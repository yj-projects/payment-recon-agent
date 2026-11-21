"""Offline post-incident verification: all CLI/HTTP boundaries are mocked."""
import copy
import json
from pathlib import Path

import pytest

from scripts import azure_release as release

SHA = 'a' * 40
DIGEST = 'sha256:' + 'c' * 64
IMAGE = 'sha256:' + 'b' * 64
HOST = 'ca-payment-recon-demo.example.eastus.azurecontainerapps.io'


@pytest.fixture
def harness(monkeypatch, tmp_path):
    from scripts import azure_verify as verify
    for key, value in {'AZURE_SUBSCRIPTION_ID': 'private-subscription', 'AZURE_ACR_NAME': 'registry',
                       'GITHUB_REPOSITORY': 'owner/repo', 'AZURE_DEPLOYMENT_ENABLED': 'false'}.items():
        monkeypatch.setenv(key, value)
    for key in ('GITHUB_EVENT_NAME', 'GITHUB_REF', 'DEPLOY_CONFIRMATION'):
        monkeypatch.delenv(key, raising=False)
    cfg = release.Config('private-subscription', 'registry', 'owner/repo')
    foundation = {'environment_id': '/env', 'pull_id': '/pull', 'login_server': 'registry.azurecr.io'}
    app = release.manifest(cfg, foundation, DIGEST, SHA)
    app['properties'].update(provisioningState='Succeeded', latestRevisionName=cfg.app + '--abc', latestReadyRevisionName=cfg.app + '--abc')
    app['properties']['configuration']['ingress'].update(fqdn=HOST, traffic=[{'latestRevision': True, 'weight': 100}])
    foundation['existing'] = app
    revision = {'name': cfg.app + '--abc', 'properties': {'provisioningState': 'Provisioned', 'healthState': 'Healthy', 'active': True,
                'runningState': 'ScaleTo0', 'template': copy.deepcopy(app['properties']['template'])}}
    source = {'id': 123, 'status': 'completed', 'conclusion': 'cancelled', 'event': 'workflow_dispatch',
              'head_branch': 'main', 'head_sha': SHA, 'path': '.github/workflows/ci.yml', 'repository': {'full_name': 'owner/repo'}}
    calls = []
    output = tmp_path / 'evidence.json'
    def run(args):
        calls.append(args)
        assert json.loads(Path(argv[-1]).read_text())['status'] == 'pending'
        assert args == ['gh', 'api', 'repos/owner/repo/actions/runs/123']
        return json.dumps(source)
    def az(self, *args):
        calls.append(list(args))
        assert not set(args) & {'create', 'update', 'delete', 'login', 'push', 'pull', 'build'}
        if args[:3] == ('containerapp', 'show', '--name'): return app
        if args[:3] == ('containerapp', 'revision', 'show'): return revision
        if args[:3] == ('acr', 'repository', 'show'):
            return {'digest': DIGEST, 'changeableAttributes': {'writeEnabled': False, 'deleteEnabled': False}}
        if args[:3] == ('acr', 'manifest', 'show'): return {'config': {'digest': IMAGE}}
        pytest.fail(str(args))
    monkeypatch.setattr(release, 'run', run)
    monkeypatch.setattr(release.Config, 'az', az)
    monkeypatch.setattr(release, 'preflight', lambda cfg: foundation)
    def acceptance(base):
        calls.append(['HTTP', base])
        assert base == 'https://' + HOST
        return {'health': True, 'docs': True, 'reconcile': True, 'exceptions': True, 'demo_mode': True, 'evidence': True, 'refusal': True}
    monkeypatch.setattr(release, 'acceptance', acceptance)
    argv = ['--source-run-id', '123', '--commit', SHA, '--digest', DIGEST, '--image-id', IMAGE, '--output', str(output)]
    return verify, argv, output, source, app, revision, foundation, calls


def test_success_disabled_deployment_preserves_cancelled_source_and_scope(harness, monkeypatch):
    verify, argv, output, source, app, revision, foundation, calls = harness
    assert verify.main(argv) == 0
    evidence = json.loads(output.read_text())
    assert evidence['kind'] == 'post-incident-verification'
    assert evidence['status'] == 'verified'
    assert evidence['source_run_id'] == '123' and evidence['source_run_conclusion'] == 'cancelled'
    assert evidence['commit'] == SHA and evidence['digest'] == DIGEST and evidence['image_id'] == IMAGE
    assert evidence['revision'] == revision['name'] and all(evidence['acceptance'].values())
    assert len(evidence['configuration_fingerprint']) == 64 and evidence['timestamp']
    assert 'private-subscription' not in output.read_text() and HOST not in output.read_text()
    assert source['conclusion'] == 'cancelled'
    first = evidence['configuration_fingerprint']
    monkeypatch.setenv('AZURE_SUBSCRIPTION_ID', 'another-subscription')
    second = output.with_name('second.json')
    argv[-1] = str(second)
    assert verify.main(argv) == 0
    assert json.loads(second.read_text())['configuration_fingerprint'] != first


@pytest.mark.parametrize('field,value', [('status', 'in_progress'), ('event', 'push'), ('head_branch', 'other'),
    ('head_sha', 'd' * 40), ('path', '.github/workflows/other.yml'), ('repository', {'full_name': 'other/repo'}), ('id', 999)])
def test_source_mismatch_fails_before_http(harness, field, value):
    verify, argv, output, source, app, revision, foundation, calls = harness
    source[field] = value
    assert verify.main(argv) == 1
    evidence = json.loads(output.read_text())
    assert evidence['status'] == 'failed' and evidence['kind'] == 'post-incident-verification'
    assert not any(c[0] == 'HTTP' for c in calls)


@pytest.mark.parametrize('flag,value', [('--digest', 'latest'), ('--image-id', 'latest'), ('--commit', 'main'), ('--source-run-id', '../1')])
def test_invalid_inputs_fail_before_commands(harness, flag, value):
    verify, argv, output, source, app, revision, foundation, calls = harness
    argv[argv.index(flag) + 1] = value
    assert verify.main(argv) == 1
    assert calls == [] and json.loads(output.read_text())['status'] == 'failed'


@pytest.mark.parametrize('change', ['missing', 'ownership', 'environment', 'digest', 'commit', 'provisioning', 'cpu', 'memory',
    'scale', 'insecure', 'external', 'port', 'identity', 'secrets', 'api-key', 'secret-ref', 'fqdn', 'ready', 'revision-template', 'revision-health', 'traffic', 'scaler', 'other-host', 'missing-secrets'])
def test_app_drift_refused_before_http(harness, change):
    verify, argv, output, source, app, revision, foundation, calls = harness
    props = app['properties']
    container = props['template']['containers'][0]
    if change == 'missing': foundation['existing'] = None
    elif change == 'ownership': app['tags'] = {}
    elif change == 'environment': props['environmentId'] = '/other'
    elif change == 'digest': container['image'] = container['image'].replace(DIGEST, IMAGE)
    elif change == 'commit': app['tags']['commit'] = 'd' * 40
    elif change == 'provisioning': props['provisioningState'] = 'Failed'
    elif change == 'cpu': container['resources']['cpu'] = 1
    elif change == 'memory': container['resources']['memory'] = '2Gi'
    elif change == 'scale': props['template']['scale']['maxReplicas'] = 2
    elif change == 'insecure': props['configuration']['ingress']['allowInsecure'] = True
    elif change == 'external': props['configuration']['ingress']['external'] = False
    elif change == 'port': props['configuration']['ingress']['targetPort'] = 80
    elif change == 'identity': app['identity']['userAssignedIdentities']['/extra'] = {}
    elif change == 'secrets': props['configuration']['secrets'] = [{'name': 'key'}]
    elif change == 'api-key': container['env'][0]['value'] = 'private-token'
    elif change == 'secret-ref': container['env'][0] = {'name': 'OPENAI_API_KEY', 'secretRef': 'key'}
    elif change == 'fqdn': props['configuration']['ingress']['fqdn'] = HOST + '.evil.test'
    elif change == 'ready': props['latestReadyRevisionName'] = 'other'
    elif change == 'revision-template': revision['properties']['template']['containers'][0]['image'] = 'wrong'
    elif change == 'revision-health': revision['properties']['healthState'] = 'Unhealthy'
    elif change == 'traffic': props['configuration']['ingress']['traffic'] = [{'revisionName': 'other', 'weight': 100}]
    elif change == 'scaler': props['template']['scale']['rules'] = [{'name': 'custom'}]
    elif change == 'other-host': props['configuration']['ingress']['fqdn'] = 'other.example.azurecontainerapps.io'
    elif change == 'missing-secrets': del props['configuration']['secrets']
    assert verify.main(argv) == 1
    assert not any(c[0] == 'HTTP' for c in calls)
    assert json.loads(output.read_text())['source_run_conclusion'] == 'cancelled'
    assert json.loads(output.read_text())['error'] == 'verification failed during app and revision verification'


@pytest.mark.parametrize('issue', ['lock', 'identity', 'digest'])
def test_registry_mismatch_fails_without_mutation(harness, monkeypatch, issue):
    verify, argv, output, source, app, revision, foundation, calls = harness
    original = release.Config.az
    def az(self, *args):
        result = original(self, *args)
        if issue == 'lock' and args[:3] == ('acr', 'repository', 'show'): result['changeableAttributes']['writeEnabled'] = True
        if issue == 'identity' and args[:3] == ('acr', 'manifest', 'show'): result['config']['digest'] = DIGEST
        if issue == 'digest' and args[:3] == ('acr', 'repository', 'show'): result['digest'] = IMAGE
        return result
    monkeypatch.setattr(release.Config, 'az', az)
    assert verify.main(argv) == 1
    assert not any(c[0] == 'HTTP' for c in calls)
    assert json.loads(output.read_text())['error'] == 'verification failed during published image verification'


@pytest.mark.parametrize('error,status,code', [(RuntimeError('token private-subscription ' + HOST), 'failed', 1), (KeyboardInterrupt(), 'interrupted', 130)])
def test_exception_and_interruption_evidence_sanitized(harness, monkeypatch, capsys, error, status, code):
    verify, argv, output, source, app, revision, foundation, calls = harness
    def fail(cfg): raise error
    monkeypatch.setattr(release, 'preflight', fail)
    assert verify.main(argv) == code
    evidence = json.loads(output.read_text())
    assert evidence['status'] == status and evidence['kind'] == 'post-incident-verification'
    assert evidence['source_run_conclusion'] == 'cancelled'
    combined = output.read_text() + capsys.readouterr().err
    assert 'token' not in combined and HOST not in combined and 'private-subscription' not in combined


def test_existing_evidence_is_never_overwritten(harness):
    verify, argv, output, *rest = harness
    output.write_text('original')
    assert verify.main(argv) == 1
    assert output.read_text() == 'original'
    assert rest[-1] == []


def test_post_incident_evidence_cannot_authorize_normal_rollback(harness, monkeypatch):
    verify, argv, output, *rest = harness
    assert verify.main(argv) == 0
    monkeypatch.setattr(release, 'gated_config', lambda operation: release.Config('sub', 'registry', 'owner/repo'))
    with pytest.raises(release.ReleaseError, match='workflow release receipt'):
        release.rollback(output, DIGEST, output.with_name('rollback.json'))


def test_registry_cli_empty_credential_fields_are_safe(harness):
    verify, argv, output, source, app, revision, foundation, calls = harness
    app['properties']['configuration']['registries'][0].update(username=None, passwordSecretRef=None)
    cfg = release.Config('private-subscription', 'registry', 'owner/repo')
    assert verify.app_state(cfg, foundation, DIGEST, SHA) == (HOST, revision['name'])


@pytest.mark.parametrize('field,value', [('username', 'user'), ('passwordSecretRef', 'key')])
def test_registry_credentials_are_refused(harness, field, value):
    verify, argv, output, source, app, revision, foundation, calls = harness
    app['properties']['configuration']['registries'][0][field] = value
    assert verify.main(argv) == 1
    assert not any(c[0] == 'HTTP' for c in calls)


def test_conflicting_traffic_target_is_refused(harness):
    verify, argv, output, source, app, revision, foundation, calls = harness
    app['properties']['configuration']['ingress']['traffic'] = [{'latestRevision': True, 'revisionName': 'other', 'weight': 100}]
    assert verify.main(argv) == 1
    assert not any(c[0] == 'HTTP' for c in calls)


@pytest.mark.parametrize('normalization', ['null-secrets', 'template-defaults', 'both'])
def test_azure_normalization_preserves_approved_runtime(harness, normalization):
    verify, argv, output, source, app, revision, foundation, calls = harness
    if normalization in ('null-secrets', 'both'):
        app['properties']['configuration']['secrets'] = None
    if normalization in ('template-defaults', 'both'):
        template = revision['properties']['template']
        template['revisionSuffix'] = 'azure-generated'
        template['scale'].update(cooldownPeriod=300, pollingInterval=30)
        container = template['containers'][0]
        container.update(command=None, args=None, volumeMounts=None)
        for probe in container['probes']:
            probe.update(successThreshold=1)
            probe['httpGet']['httpHeaders'] = None
    assert verify.main(argv) == 0
    assert json.loads(output.read_text())['status'] == 'verified'


@pytest.mark.parametrize('target', ['app', 'revision'])
@pytest.mark.parametrize('change', ['command', 'args', 'api-key', 'secret-ref', 'extra-env', 'cpu', 'memory', 'image',
                                    'scale-min', 'scale-max', 'scale-rules', 'init', 'volumes', 'mounts', 'probe-path', 'probe-port', 'probe-types'])
def test_each_template_runtime_is_independently_bounded(harness, target, change):
    verify, argv, output, source, app, revision, foundation, calls = harness
    template = (app if target == 'app' else revision)['properties']['template']
    container = template['containers'][0]
    if change == 'command': container['command'] = ['/bin/sh']
    elif change == 'args': container['args'] = ['--unsafe']
    elif change == 'api-key': container['env'][0]['value'] = 'key'
    elif change == 'secret-ref': container['env'][0]['secretRef'] = 'key'
    elif change == 'extra-env': container['env'].append({'name': 'EXTRA', 'value': 'value'})
    elif change == 'cpu': container['resources']['cpu'] = 1
    elif change == 'memory': container['resources']['memory'] = '2Gi'
    elif change == 'image': container['image'] = 'other'
    elif change == 'scale-min': template['scale']['minReplicas'] = 1
    elif change == 'scale-max': template['scale']['maxReplicas'] = 2
    elif change == 'scale-rules': template['scale']['rules'] = [{'name': 'custom'}]
    elif change == 'init': template['initContainers'] = [{'name': 'init'}]
    elif change == 'volumes': template['volumes'] = [{'name': 'data'}]
    elif change == 'mounts': container['volumeMounts'] = [{'volumeName': 'data', 'mountPath': '/data'}]
    elif change == 'probe-path': container['probes'][0]['httpGet']['path'] = '/other'
    elif change == 'probe-port': container['probes'][0]['httpGet']['port'] = 80
    elif change == 'probe-types': container['probes'][0]['type'] = 'Readiness'
    assert verify.main(argv) == 1
    assert json.loads(output.read_text())['error'] == 'verification failed during app and revision verification'
    assert not any(c[0] == 'HTTP' for c in calls)


@pytest.mark.parametrize('empty', [None, []])
def test_normalized_empty_ingress_extensions_are_safe(harness, empty):
    verify, argv, output, source, app, revision, foundation, calls = harness
    ingress = app['properties']['configuration']['ingress']
    ingress.update(additionalPortMappings=empty, customDomains=empty, ipSecurityRestrictions=empty,
                   corsPolicy=empty, exposedPort=None if empty is None else 0, transport='Auto')
    assert verify.main(argv) == 0


@pytest.mark.parametrize('field,value', [('additionalPortMappings', [{'external': True, 'exposedPort': 9000, 'targetPort': 9000}]),
    ('customDomains', [{'name': 'custom.example'}]), ('transport', 'tcp'), ('transport', None), ('exposedPort', 8001),
    ('ipSecurityRestrictions', [{'name': 'custom', 'action': 'Allow', 'ipAddressRange': '10.0.0.0/8'}]),
    ('corsPolicy', {'allowedOrigins': ['https://custom.example']})])
def test_unapproved_ingress_extensions_are_refused(harness, field, value):
    verify, argv, output, source, app, revision, foundation, calls = harness
    app['properties']['configuration']['ingress'][field] = value
    assert verify.main(argv) == 1
    assert not any(c[0] == 'HTTP' for c in calls)


@pytest.mark.parametrize('target', ['app', 'revision'])
@pytest.mark.parametrize('kind', ['Startup', 'Readiness', 'Liveness'])
@pytest.mark.parametrize('field', ['initialDelaySeconds', 'periodSeconds', 'timeoutSeconds', 'failureThreshold'])
def test_declared_probe_timing_drift_is_refused(harness, target, kind, field):
    verify, argv, output, source, app, revision, foundation, calls = harness
    container = (app if target == 'app' else revision)['properties']['template']['containers'][0]
    probe = next(probe for probe in container['probes'] if probe['type'] == kind)
    probe[field] += 1
    assert verify.main(argv) == 1
    assert not any(c[0] == 'HTTP' for c in calls)


@pytest.mark.parametrize('recover', [True, False])
def test_http_acceptance_retries_are_bounded_and_preserve_source(harness, monkeypatch, recover):
    verify, argv, output, source, app, revision, foundation, calls = harness
    attempts = []
    sleeps = []
    original = release.acceptance
    def acceptance(base):
        attempts.append(base)
        if not recover or len(attempts) == 1:
            raise release.ReleaseError('temporary HTTP failure')
        return original(base)
    monkeypatch.setattr(release, 'acceptance', acceptance)
    monkeypatch.setattr(release.time, 'sleep', sleeps.append)
    assert verify.main(argv) == (0 if recover else 1)
    assert len(attempts) == (2 if recover else 3)
    assert sleeps == ([5] if recover else [5, 5])
    evidence = json.loads(output.read_text())
    assert evidence['source_run_conclusion'] == 'cancelled'
    assert evidence['status'] == ('verified' if recover else 'failed')
    assert all(base == 'https://' + HOST for base in attempts)
