"""Offline release safety tests: no Docker daemon or Azure account required."""
import json
from pathlib import Path
import subprocess

import pytest

from scripts import azure_release as release

SHA = 'a' * 40
IMAGE = 'sha256:' + 'b' * 64
DIGEST = 'sha256:' + 'c' * 64


def gate(monkeypatch):
    for key, value in {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': 'refs/heads/main',
                       'AZURE_DEPLOYMENT_ENABLED': 'true', 'DEPLOY_CONFIRMATION': 'DEPLOY SYNTHETIC DEMO',
                       'AZURE_SUBSCRIPTION_ID': 'sub', 'AZURE_ACR_NAME': 'registry',
                       'GITHUB_REPOSITORY': 'owner/repo'}.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize('key,value', [('GITHUB_EVENT_NAME', 'push'), ('GITHUB_REF', 'refs/heads/feature'),
    ('AZURE_DEPLOYMENT_ENABLED', 'false'), ('DEPLOY_CONFIRMATION', '')])
def test_gate_rejects_before_commands(monkeypatch, key, value):
    gate(monkeypatch)
    monkeypatch.setenv(key, value)
    monkeypatch.setattr(release, 'run', lambda *a, **k: pytest.fail('command before gate'))
    with pytest.raises(release.ReleaseError):
        release.deploy(IMAGE, SHA, Path('unused.json'))


def test_image_mismatch_stops_before_azure(monkeypatch, tmp_path):
    gate(monkeypatch)
    def run(args):
        if args[:3] == ['git', 'rev-parse', 'HEAD']:
            return SHA
        if args[:3] == ['docker', 'image', 'inspect']:
            return 'sha256:' + '0' * 64
        pytest.fail(f'unexpected command {args}')
    monkeypatch.setattr(release, 'run', run)
    with pytest.raises(release.ReleaseError, match='image ID'):
        release.deploy(IMAGE, SHA, tmp_path / 'receipt.json')


def test_bad_commit_rejected_before_commands(monkeypatch, tmp_path):
    gate(monkeypatch)
    monkeypatch.setattr(release, 'run', lambda *a: pytest.fail('bad SHA command'))
    with pytest.raises(release.ReleaseError, match='commit'):
        release.deploy(IMAGE, 'main', tmp_path / 'receipt.json')


def test_collision_never_pushes(monkeypatch):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    calls = []
    def az(*args):
        calls.append(args)
        if args[:3] == ('acr', 'repository', 'list'):
            return ['payment-recon-agent']
        if args[:3] == ('acr', 'repository', 'show-tags'):
            return [SHA]
        pytest.fail(str(args))
    monkeypatch.setattr(cfg, 'az', az)
    monkeypatch.setattr(release, 'run', lambda *a: pytest.fail('collision pushed'))
    with pytest.raises(release.ReleaseError, match='already exists'):
        release.publish(cfg, SHA, IMAGE)


def test_registry_api_errors_never_become_missing(monkeypatch):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    monkeypatch.setattr(cfg, 'az', lambda *a: (_ for _ in ()).throw(release.ReleaseError('Forbidden')))
    monkeypatch.setattr(release, 'run', lambda *a: pytest.fail('error pushed'))
    with pytest.raises(release.ReleaseError, match='Forbidden'):
        release.publish(cfg, SHA, IMAGE)


def test_manifest_is_bounded_and_has_no_credentials():
    cfg = release.Config('sub', 'registry', 'owner/repo')
    foundation = {'environment_id': '/environment', 'pull_id': '/pull', 'login_server': 'registry.azurecr.io'}
    body = release.manifest(cfg, foundation, DIGEST, SHA)
    props = body['properties']
    assert props['workloadProfileName'] == 'Consumption'
    assert props['environmentId'] == '/environment'
    assert 'managedEnvironmentId' not in props
    assert body['identity'] == {'type': 'UserAssigned', 'userAssignedIdentities': {'/pull': {}}}
    assert props['configuration']['registries'] == [{'server': 'registry.azurecr.io', 'identity': '/pull'}]
    assert props['configuration']['ingress']['allowInsecure'] is False
    assert props['template']['scale'] == {'minReplicas': 0, 'maxReplicas': 1}
    container = props['template']['containers'][0]
    assert container['resources'] == {'cpu': 0.25, 'memory': '0.5Gi'}
    assert container['image'] == 'registry.azurecr.io/payment-recon-agent@' + DIGEST
    assert container['env'] == [{'name': 'OPENAI_API_KEY', 'value': ''}]
    assert {p['type'] for p in container['probes']} == {'Startup', 'Readiness', 'Liveness'}
    assert all(p['httpGet']['path'] == '/health' for p in container['probes'])


def test_rollback_receipt_cannot_select_unrecorded_digest(monkeypatch, tmp_path):
    gate(monkeypatch)
    monkeypatch.setenv('DEPLOY_CONFIRMATION', 'ROLLBACK SYNTHETIC DEMO')
    receipt = tmp_path / 'release.json'
    receipt.write_text(json.dumps({'status': 'succeeded', 'repository': 'owner/repo', 'digest': DIGEST,
                                  'commit': SHA, 'previous_digest': None}))
    monkeypatch.setattr(release, 'run', lambda *a: pytest.fail('unapproved rollback command'))
    with pytest.raises(release.ReleaseError, match='receipt'):
        release.rollback(receipt, IMAGE, tmp_path / 'out.json')


def test_acceptance_checks_evidence_and_refusal(monkeypatch):
    def request(base, path, payload=None):
        if path == '/health': return {'status': 'ok'}
        if path == '/docs': return '<html>swagger</html>'
        if path == '/reconcile': return {'summary': {'total_payouts': 12, 'matched': 7, 'exceptions': 5, 'unmatched_bank': 1}}
        if path == '/exceptions': return {'payout_exceptions': [1] * 5, 'unmatched_bank': [1]}
        if payload['question'] == 'Summary':
            return {'mode': 'demo-mode', 'error': None, 'evidence': [{'tool': 'get_summary', 'result': {'total_payouts': 12, 'matched': 7, 'exceptions': 5, 'unmatched_bank': 1}}]}
        return {'mode': 'demo-mode', 'error': None, 'evidence': [], 'executed_tools': [], 'answer': 'I cannot execute refunds; no action was performed.'}
    monkeypatch.setattr(release, 'request', request)
    assert release.acceptance('https://demo.example')['demo_mode'] is True
    original = request
    def bad(*args):
        value = original(*args)
        if args[1] == '/agent/ask': value['mode'] = 'live-llm'
        return value
    monkeypatch.setattr(release, 'request', bad)
    with pytest.raises(release.ReleaseError, match='demo'):
        release.acceptance('https://demo.example')


def foundation_stub(cfg, *, ownership=True, sku='Basic', existing=False):
    tags = {'project': 'payment-recon-agent', 'environment': 'demo', 'cleanup': 'resource-group'} if ownership else {}
    calls = []
    def az(*args):
        calls.append(args)
        key = args[:3]
        if key == ('group', 'show', '--name'): return {'tags': tags, 'location': 'eastus', 'properties': {'provisioningState': 'Succeeded'}}
        if key == ('acr', 'show', '--name'):
            return {'tags': tags, 'location': 'eastus', 'provisioningState': 'Succeeded', 'sku': {'name': sku}, 'adminUserEnabled': False,
                    'roleAssignmentMode': 'LegacyRegistryPermissions', 'id': '/acr', 'loginServer': 'registry.azurecr.io'}
        if key == ('acr', 'config', 'authentication-as-arm'): return {'status': 'enabled'}
        if key == ('containerapp', 'env', 'show'):
            return {'tags': tags, 'location': 'eastus', 'id': '/env', 'properties': {'provisioningState': 'Succeeded',
                'workloadProfiles': [{'name': 'Consumption', 'workloadProfileType': 'Consumption'}],
                'appLogsConfiguration': {'destination': 'azure-monitor'}}}
        if key == ('monitor', 'log-analytics', 'workspace'): return {'tags': tags, 'location': 'eastus', 'id': '/law', 'provisioningState': 'Succeeded', 'sku': {'name': 'PerGB2018'}, 'retentionInDays': 30, 'workspaceCapping': {'dailyQuotaGb': .023}}
        if key == ('monitor', 'diagnostic-settings', 'list'):
            return {'value': [{'workspaceId': '/law', 'logs': [{'category': category, 'enabled': True} for category in ('ContainerAppConsoleLogs', 'ContainerAppSystemLogs')]}]}
        if key == ('identity', 'show', '--name'): return {'tags': tags, 'id': '/pull', 'principalId': 'principal'}
        if key == ('role', 'assignment', 'list'): return [{'roleDefinitionId': '/roles/7f951dda-4ed3-4680-a7ca-43fe172d538d', 'scope': '/acr'}]
        if key == ('containerapp', 'list', '--resource-group'):
            return [{'name': cfg.app, 'tags': {}, 'properties': {}}] if existing else []
        pytest.fail(f'Unexpected command: {args}')
    return az, calls


@pytest.mark.parametrize('changes,error', [({'ownership': False}, 'ownership'), ({'sku': 'Premium'}, 'Basic'),
                                          ({'existing': True}, 'ownership')])
def test_preflight_refuses_bad_foundation_without_mutation(monkeypatch, changes, error):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    az, calls = foundation_stub(cfg, **changes)
    monkeypatch.setattr(cfg, 'az', az)
    with pytest.raises(release.ReleaseError, match=error):
        release.preflight(cfg)
    assert all(not {'create', 'update', 'delete'} & set(args) for args in calls)


def test_preflight_accepts_existing_foundation_read_only(monkeypatch):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    az, calls = foundation_stub(cfg)
    monkeypatch.setattr(cfg, 'az', az)
    result = release.preflight(cfg)
    assert result == {'environment_id': '/env', 'pull_id': '/pull', 'login_server': 'registry.azurecr.io', 'existing': None}
    assert all(not {'create', 'update', 'delete'} & set(args) for args in calls)


@pytest.mark.parametrize('manifest_identity', [False, True])
def test_publish_locks_tag_and_manifest_before_returning(monkeypatch, manifest_identity):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    commands = []
    mutations = []
    def az(*args):
        if args[:3] == ('acr', 'repository', 'list'): return []
        if args[:3] == ('acr', 'show', '--name'): return {'loginServer': 'registry.azurecr.io'}
        if args[:3] == ('acr', 'repository', 'show'):
            return {'digest': DIGEST, 'changeableAttributes': {'writeEnabled': False, 'deleteEnabled': False}}
        if args[:3] == ('acr', 'manifest', 'show'):
            return {'manifests': []} if manifest_identity else {'config': {'digest': IMAGE}}
        if args[:3] == ('acr', 'repository', 'update'):
            mutations.append(args)
            return {}
        pytest.fail(str(args))
    monkeypatch.setattr(cfg, 'az', az)
    monkeypatch.setattr(release, 'run', lambda args: commands.append(args) or '')
    assert release.publish(cfg, SHA, DIGEST if manifest_identity else IMAGE) == DIGEST
    assert [m[m.index('--image') + 1] for m in mutations] == ['payment-recon-agent:' + SHA, 'payment-recon-agent@' + DIGEST]
    assert all(m[-4:] == ('--write-enabled', 'false', '--delete-enabled', 'false') for m in mutations)
    assert [c[1] for c in commands if c[0] == 'docker'] == ['tag', 'push']


def test_publish_refuses_failed_lock_readback(monkeypatch):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    monkeypatch.setattr(cfg, 'az', lambda *a: {'digest': DIGEST, 'changeableAttributes': {'writeEnabled': True, 'deleteEnabled': False}})
    with pytest.raises(release.ReleaseError, match='lock readback'):
        release.verify_locked(cfg, DIGEST, SHA)


def test_deployment_failure_writes_receipt_and_does_not_rollback(monkeypatch, tmp_path):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    foundation = {'existing': {'properties': {'template': {'containers': [{'image': 'registry.azurecr.io/payment-recon-agent@' + IMAGE}]}}}}
    monkeypatch.setattr(release, 'preflight', lambda c: foundation)
    monkeypatch.setattr(release, 'publish', lambda *a: DIGEST)
    monkeypatch.setattr(release, 'apply', lambda *a: (_ for _ in ()).throw(release.ReleaseError('acceptance failed')))
    monkeypatch.setattr(release, 'run', lambda *a: pytest.fail('unexpected automatic rollback'))
    path = tmp_path / 'receipt.json'
    with pytest.raises(release.ReleaseError, match='acceptance failed'):
        release.perform(cfg, 'deploy', SHA, IMAGE, path)
    result = json.loads(path.read_text())
    assert result['status'] == 'failed' and result['digest'] == DIGEST
    assert result['previous_digest'] == IMAGE
    assert 'No automatic rollback' in result['rollback_instruction']


def test_rollback_smokes_approved_digest_before_update_and_never_pushes(monkeypatch, tmp_path):
    gate(monkeypatch)
    monkeypatch.setenv('DEPLOY_CONFIRMATION', 'ROLLBACK SYNTHETIC DEMO')
    monkeypatch.setenv('ROLLBACK_SOURCE_RUN_ID', '123')
    monkeypatch.setenv('ROLLBACK_SOURCE_COMMIT', SHA)
    cfg = release.Config('sub', 'registry', 'owner/repo')
    prior = release.receipt_base(cfg, 'deploy', SHA, IMAGE)
    prior.update(status='succeeded', run_id='123', workflow_commit=SHA, digest=DIGEST,
                 acceptance={k: True for k in ('health', 'docs', 'reconcile', 'exceptions', 'demo_mode', 'evidence', 'refusal')})
    source = tmp_path / 'prior.json'
    source.write_text(json.dumps(prior))
    steps = []
    foundation = {'existing': {'name': cfg.app}, 'login_server': 'registry.azurecr.io'}
    monkeypatch.setattr(release, 'preflight', lambda c: foundation)
    monkeypatch.setattr(release, 'verify_locked', lambda *a: steps.append('verify-lock'))
    def run(args):
        steps.append(args)
        if args[:3] == ['docker', 'image', 'inspect']: return IMAGE
        if args[:2] == ['docker', 'run']: return 'container-id'
        if args[:2] == ['docker', 'port']: return '127.0.0.1:49152'
        return ''
    monkeypatch.setattr(release, 'run', run)
    monkeypatch.setattr(release, 'retry_acceptance', lambda base: steps.append('smoke'))
    def perform(*args):
        steps.append('update')
        assert args[-1] == DIGEST
        return {'status': 'succeeded'}
    monkeypatch.setattr(release, 'perform', perform)
    assert release.rollback(source, DIGEST, tmp_path / 'receipt.json')['status'] == 'succeeded'
    assert steps.index('smoke') < steps.index('update')
    assert all('push' not in command and 'tag' not in command for command in steps if isinstance(command, list))


def test_wrong_rollback_source_run_rejected_before_cloud(monkeypatch, tmp_path):
    gate(monkeypatch)
    monkeypatch.setenv('DEPLOY_CONFIRMATION', 'ROLLBACK SYNTHETIC DEMO')
    monkeypatch.setenv('ROLLBACK_SOURCE_RUN_ID', '999')
    monkeypatch.setenv('ROLLBACK_SOURCE_COMMIT', SHA)
    source = tmp_path / 'prior.json'
    source.write_text(json.dumps({'schema_version': 1, 'status': 'succeeded', 'digest': DIGEST,
        'repository': 'owner/repo', 'resource_group': 'rg-payment-recon-demo', 'app': 'ca-payment-recon-demo',
        'registry': 'registry', 'run_id': '123', 'workflow_commit': SHA}))
    monkeypatch.setattr(release, 'run', lambda *a: pytest.fail('provenance failure made a command'))
    with pytest.raises(release.ReleaseError, match='provenance'):
        release.rollback(source, DIGEST, tmp_path / 'out.json')


def test_publish_rejects_registry_manifest_with_different_config_id(monkeypatch):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    commands = []
    def az(*args):
        if args[:3] == ('acr', 'repository', 'list'): return []
        if args[:3] == ('acr', 'show', '--name'): return {'loginServer': 'registry.azurecr.io'}
        if args[:3] == ('acr', 'repository', 'show'): return {'digest': DIGEST}
        if args[:3] == ('acr', 'manifest', 'show'): return {'config': {'digest': DIGEST}}
        pytest.fail('Unexpected mutation after config mismatch: ' + str(args))
    monkeypatch.setattr(cfg, 'az', az)
    monkeypatch.setattr(release, 'run', lambda args: commands.append(args) or '')
    with pytest.raises(release.ReleaseError, match='config ID'):
        release.publish(cfg, SHA, IMAGE)


def test_rollback_does_not_recreate_app_if_it_disappears_after_smoke(monkeypatch, tmp_path):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    monkeypatch.setattr(release, 'preflight', lambda c: {'existing': None})
    monkeypatch.setattr(release, 'apply', lambda *a: pytest.fail('rollback recreated missing app'))
    with pytest.raises(release.ReleaseError, match='existing app'):
        release.perform(cfg, 'rollback', SHA, IMAGE, tmp_path / 'receipt.json', DIGEST)


@pytest.mark.parametrize('command,changes,error', [
    (('group', 'show', '--name'), {'properties': {'provisioningState': 'Failed'}}, 'provisioning'),
    (('acr', 'show', '--name'), {'provisioningState': 'Failed'}, 'provisioning'),
    (('monitor', 'log-analytics', 'workspace'), {'provisioningState': 'Failed'}, 'provisioning'),
    (('monitor', 'log-analytics', 'workspace'), {'retentionInDays': 90}, 'workspace'),
    (('monitor', 'log-analytics', 'workspace'), {'workspaceCapping': {'dailyQuotaGb': 1}}, 'workspace'),
    (('containerapp', 'env', 'show'), {'identity': {'type': 'SystemAssigned'}}, 'identity'),
])
def test_preflight_rejects_foundation_drift(monkeypatch, command, changes, error):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    base, calls = foundation_stub(cfg)
    def az(*args):
        value = base(*args)
        if args[:3] == command:
            value.update(changes)
        return value
    monkeypatch.setattr(cfg, 'az', az)
    with pytest.raises(release.ReleaseError, match=error):
        release.preflight(cfg)


@pytest.mark.parametrize('field,value,error', [
    ('workloadProfiles', [{'name': 'Consumption', 'workloadProfileType': 'Consumption'}, {'name': 'D4', 'workloadProfileType': 'D4'}], 'Consumption'),
    ('vnetConfiguration', {'infrastructureSubnetId': '/subnet'}, 'network'),
    ('vnetConfiguration', {'internal': True}, 'network'),
    ('zoneRedundant', True, 'network'),
])
def test_preflight_rejects_dedicated_or_networked_environment(monkeypatch, field, value, error):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    base, _ = foundation_stub(cfg)
    def az(*args):
        result = base(*args)
        if args[:3] == ('containerapp', 'env', 'show'):
            result['properties'][field] = value
        return result
    monkeypatch.setattr(cfg, 'az', az)
    with pytest.raises(release.ReleaseError, match=error):
        release.preflight(cfg)


def test_acceptance_uses_real_synthetic_app_contract(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    with TestClient(create_app()) as client:
        def adapter(base, path, payload=None):
            response = client.post(path, json=payload) if payload else client.get(path)
            assert response.status_code == 200
            return response.text if path == '/docs' else response.json()
        monkeypatch.setattr(release, 'request', adapter)
        assert all(release.acceptance('https://offline-test.example').values())


@pytest.mark.parametrize('shape', ['list', 'value', 'properties'])
def test_preflight_accepts_cli_diagnostic_shapes_and_avoids_graph(monkeypatch, shape):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    base, calls = foundation_stub(cfg)
    def az(*args):
        result = base(*args)
        if args[:3] == ('monitor', 'diagnostic-settings', 'list'):
            if shape == 'list': return result['value']
            if shape == 'properties': return {'value': [{'properties': result['value'][0]}]}
        return result
    monkeypatch.setattr(cfg, 'az', az)
    release.preflight(cfg)
    role = next(c for c in calls if c[:3] == ('role', 'assignment', 'list'))
    assert '--assignee-object-id' in role and '--assignee' not in role
    assert role[role.index('--fill-principal-name') + 1] == 'false'
    assert role[role.index('--fill-role-definition-name') + 1] == 'false'


@pytest.mark.parametrize('change', [
    {'logs': [{'category': 'ContainerAppConsoleLogs', 'enabled': True}]},
    {'logs': [{'categoryGroup': 'allLogs', 'enabled': True}]},
    {'workspaceId': '/different-law'},
    {'logAnalyticsDestinationType': 'AzureDiagnostics'},
    {'metrics': [{'enabled': True}]},
    {'storageAccountId': '/storage'},
])
def test_preflight_rejects_diagnostic_drift(monkeypatch, change):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    base, _ = foundation_stub(cfg)
    def az(*args):
        result = base(*args)
        if args[:3] == ('monitor', 'diagnostic-settings', 'list'):
            result['value'][0].update(change)
        return result
    monkeypatch.setattr(cfg, 'az', az)
    with pytest.raises(release.ReleaseError, match='diagnostics'):
        release.preflight(cfg)


def test_az_manifest_show_parses_cli_json_object(monkeypatch):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    commands = []
    def run(args):
        commands.append(args)
        return json.dumps({'schemaVersion': 2, 'config': {'digest': IMAGE}})
    monkeypatch.setattr(release, 'run', run)
    assert cfg.az('acr', 'manifest', 'show', '--registry', 'registry', '--name', 'payment-recon-agent@' + DIGEST)['config']['digest'] == IMAGE
    assert '--raw' not in commands[0]
    assert commands[0][-2:] == ['-o', 'json']


def test_preflight_refuses_custom_registry_network_rules(monkeypatch):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    base, _ = foundation_stub(cfg)
    def az(*args):
        value = base(*args)
        if args[:3] == ('acr', 'show', '--name'):
            value['networkRuleSet'] = {'defaultAction': 'Deny', 'ipRules': []}
        return value
    monkeypatch.setattr(cfg, 'az', az)
    with pytest.raises(release.ReleaseError, match='network'):
        release.preflight(cfg)


@pytest.mark.parametrize('references', [
    {'environmentId': '/env'},
    {'managedEnvironmentId': '/env'},
    {'environmentId': '/env', 'managedEnvironmentId': '/ENV'},
])
def test_existing_app_accepts_current_and_legacy_environment_reference(monkeypatch, references):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    base, _ = foundation_stub(cfg)
    def az(*args):
        if args[:3] == ('containerapp', 'list', '--resource-group'):
            return [{'name': cfg.app, 'tags': {'project': 'payment-recon-agent', 'environment': 'demo', 'cleanup': 'resource-group'},
                     'properties': {**references, 'template': {'containers': [{'image': 'registry.azurecr.io/payment-recon-agent@' + DIGEST}]}}}]
        return base(*args)
    monkeypatch.setattr(cfg, 'az', az)
    assert release.preflight(cfg)['existing']['name'] == cfg.app


@pytest.mark.parametrize('references', [
    {}, {'environmentId': ''}, {'environmentId': None}, {'environmentId': 42},
    {'environmentId': '/env', 'managedEnvironmentId': ''},
    {'environmentId': '/env', 'managedEnvironmentId': '/other'},
    {'environmentId': '/other', 'managedEnvironmentId': '/env'},
])
def test_existing_app_rejects_invalid_or_conflicting_environment_reference(monkeypatch, references):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    base, _ = foundation_stub(cfg)
    def az(*args):
        if args[:3] == ('containerapp', 'list', '--resource-group'):
            return [{'name': cfg.app, 'tags': {'project': 'payment-recon-agent', 'environment': 'demo', 'cleanup': 'resource-group'},
                     'properties': {**references, 'template': {'containers': [{'image': 'registry.azurecr.io/payment-recon-agent@' + DIGEST}]}}}]
        return base(*args)
    monkeypatch.setattr(cfg, 'az', az)
    with pytest.raises(release.ReleaseError, match='environment'):
        release.preflight(cfg)


@pytest.mark.parametrize('destination_type', [None, 'Dedicated'])
def test_preflight_accepts_supported_azure_monitor_destination_types(monkeypatch, destination_type):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    base, _ = foundation_stub(cfg)
    def az(*args):
        result = base(*args)
        if args[:3] == ('monitor', 'diagnostic-settings', 'list'):
            result['value'][0]['logAnalyticsDestinationType'] = destination_type
        return result
    monkeypatch.setattr(cfg, 'az', az)
    release.preflight(cfg)
