#!/usr/bin/env python3
"""Fail-closed, manual CI releases of the tested synthetic demo image.

This module never creates foundation resources. JSON manifests are valid YAML.
No command in this module is executed at import time.
"""
import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from urllib.request import HTTPRedirectHandler, Request, build_opener

PROJECT = 'payment-recon-agent'
LOCAL_IMAGE = PROJECT + ':ci'
DIGEST_RE = re.compile(r'sha256:[0-9a-f]{64}\Z')
COMMIT_RE = re.compile(r'[0-9a-f]{40}\Z')


class ReleaseError(RuntimeError):
    pass


class ReleaseInterrupted(ReleaseError):
    pass


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


def progress(phase, **fields):
    print(json.dumps({'event': 'release_progress', 'phase': phase, **fields}), flush=True)


def write_evidence(path, value, *, exclusive=False):
    """Atomically checkpoint complete JSON; never initialize over prior evidence."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
            if exclusive:
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    raise ReleaseError('evidence file already exists; preserve it before a new attempt') from None
            else:
                os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def run(args):
    timeout = 300 if args[:2] in (['docker', 'push'], ['docker', 'pull']) else 60
    try:
        result = subprocess.run(args, text=True, capture_output=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ReleaseError(f'{args[0]} {args[1]} timed out; remote outcome may be unknown; do not retry the write') from None
    if result.returncode:
        # CLI output can contain account identifiers: keep it out of artifacts/logs.
        raise ReleaseError(f'{args[0]} {args[1]} failed (exit {result.returncode}); inspect authenticated CLI diagnostics')
    return result.stdout.strip()


@dataclass
class Config:
    subscription: str
    acr: str
    repository: str
    group: str = 'rg-payment-recon-demo'
    app: str = 'ca-payment-recon-demo'
    environment: str = 'cae-payment-recon-demo'
    workspace: str = 'law-payment-recon-demo'
    pull: str = 'id-recon-pull'
    cd: str = 'id-recon-cd'

    def az(self, *args):
        return json.loads(run(['az', *args, '--subscription', self.subscription, '--only-show-errors', '-o', 'json']) or 'null')


def gated_config(operation):
    expected = 'DEPLOY SYNTHETIC DEMO' if operation == 'deploy' else 'ROLLBACK SYNTHETIC DEMO'
    for key, value in {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': 'refs/heads/main',
                       'AZURE_DEPLOYMENT_ENABLED': 'true', 'DEPLOY_CONFIRMATION': expected}.items():
        require(os.environ.get(key) == value, f'{operation} requires {key}={value}')
    required = ['AZURE_SUBSCRIPTION_ID', 'AZURE_ACR_NAME', 'GITHUB_REPOSITORY']
    for key in required:
        require(bool(os.environ.get(key)), f'{key} is required')
    cfg = Config(*(os.environ[key] for key in required))
    for field, env in [('group', 'RESOURCE_GROUP'), ('app', 'APP_NAME'), ('environment', 'CONTAINERAPPS_ENVIRONMENT'),
                       ('workspace', 'LOG_ANALYTICS_WORKSPACE'), ('pull', 'PULL_IDENTITY_NAME'), ('cd', 'CD_IDENTITY_NAME')]:
        setattr(cfg, field, os.environ.get('AZURE_' + env) or getattr(cfg, field))
    return cfg


def owned(resource):
    tags = resource.get('tags') or {}
    require(all(tags.get(k) == v for k, v in {'project': PROJECT, 'environment': 'demo', 'cleanup': 'resource-group'}.items()),
            'resource lacks required project/demo/cleanup ownership tags')


def foundation_properties(resource, kind):
    owned(resource)
    props = resource.get('properties', resource)
    require(props.get('provisioningState') == 'Succeeded', kind + ' provisioning must be Succeeded')
    require((resource.get('identity') or {}).get('type', 'None') == 'None', kind + ' must have no custom identity')
    require(str(resource.get('location', '')).replace(' ', '').lower() == os.environ.get('AZURE_LOCATION', 'eastus').replace(' ', '').lower(),
            kind + ' location differs from configured location')
    network = props.get('networkRuleSet') or {}
    require(not props.get('privateEndpointConnections') and props.get('publicNetworkAccess') in (None, 'Enabled')
            and network.get('defaultAction') in (None, 'Allow') and not network.get('ipRules') and not network.get('virtualNetworkRules')
            and props.get('publicNetworkAccessForIngestion') in (None, 'Enabled')
            and props.get('publicNetworkAccessForQuery') in (None, 'Enabled'), kind + ' must have no custom networking')
    return props


def preflight(cfg):
    group = cfg.az('group', 'show', '--name', cfg.group)
    foundation_properties(group, 'resource group')
    acr = cfg.az('acr', 'show', '--name', cfg.acr, '--resource-group', cfg.group)
    registry = foundation_properties(acr, 'registry')
    require(acr.get('sku', {}).get('name') == 'Basic' and registry.get('adminUserEnabled') is False,
            'registry must be Basic with admin disabled')
    require(registry.get('roleAssignmentMode') in ('LegacyRegistryPermissions', 'rbac'), 'registry must use RBAC registry permissions')
    auth = cfg.az('acr', 'config', 'authentication-as-arm', 'show', '--registry', cfg.acr)
    require(auth.get('status') == 'enabled', 'registry must allow ARM audience tokens for managed identity pull')
    environment = cfg.az('containerapp', 'env', 'show', '--name', cfg.environment, '--resource-group', cfg.group)
    props = foundation_properties(environment, 'environment')
    profiles = props.get('workloadProfiles', [])
    require(len(profiles) == 1 and profiles[0].get('name') == 'Consumption' and profiles[0].get('workloadProfileType') == 'Consumption',
            'existing environment must have Consumption workload profile')
    vnet = props.get('vnetConfiguration') or {}
    require(not vnet.get('infrastructureSubnetId') and not vnet.get('internal') and not props.get('zoneRedundant'),
            'environment must have no custom networking or zone redundancy')
    require(props.get('appLogsConfiguration', {}).get('destination') == 'azure-monitor', 'environment must use azure-monitor logs')
    workspace = cfg.az('monitor', 'log-analytics', 'workspace', 'show', '--workspace-name', cfg.workspace, '--resource-group', cfg.group)
    law = foundation_properties(workspace, 'workspace')
    require(law.get('sku', {}).get('name') == 'PerGB2018' and law.get('retentionInDays') == 30
            and law.get('workspaceCapping', {}).get('dailyQuotaGb') == .023, 'workspace must retain 30 days with .023 GB daily cap')
    diagnostics = cfg.az('monitor', 'diagnostic-settings', 'list', '--resource', environment['id'])
    settings = diagnostics.get('value', []) if isinstance(diagnostics, dict) else diagnostics
    categories = {'ContainerAppConsoleLogs', 'ContainerAppSystemLogs'}
    require(isinstance(settings, list) and len(settings) == 1, 'environment must have exactly one diagnostic setting')
    diagnostic = settings[0].get('properties', settings[0])
    require(diagnostic.get('workspaceId', '').lower() == workspace['id'].lower()
            and {log.get('category') for log in diagnostic.get('logs', []) if log.get('enabled') is True} == categories
            and not any(log.get('categoryGroup') and log.get('enabled') for log in diagnostic.get('logs', []))
            and not any(metric.get('enabled') for metric in diagnostic.get('metrics', []))
            and not diagnostic.get('storageAccountId') and not diagnostic.get('eventHubAuthorizationRuleId')
            and diagnostic.get('logAnalyticsDestinationType') in (None, 'Dedicated'),
            'environment diagnostics must use exactly console/system categories and the existing workspace')
    identities = {}
    for name in (cfg.pull, cfg.cd):
        identity = cfg.az('identity', 'show', '--name', name, '--resource-group', cfg.group)
        owned(identity)
        identities[name] = identity
    assignments = cfg.az('role', 'assignment', 'list', '--assignee-object-id', identities[cfg.pull]['principalId'], '--scope', acr['id'],
                         '--fill-principal-name', 'false', '--fill-role-definition-name', 'false')
    require(any(a.get('roleDefinitionId', '').split('/')[-1].lower() == '7f951dda-4ed3-4680-a7ca-43fe172d538d' and a.get('scope', '').lower() == acr['id'].lower() for a in assignments),
            'pull identity requires AcrPull at the registry scope')
    apps = cfg.az('containerapp', 'list', '--resource-group', cfg.group)
    existing = next((a for a in apps if a.get('name') == cfg.app), None)
    if existing:
        owned(existing)
        app_props = existing.get('properties', {})
        references = [app_props[key] for key in ('environmentId', 'managedEnvironmentId') if key in app_props]
        require(bool(references) and all(isinstance(ref, str) and bool(ref.strip())
                and ref.lower() == environment['id'].lower() for ref in references),
                'existing app has an invalid, conflicting, or different environment reference')
        containers = existing.get('properties', {}).get('template', {}).get('containers', [])
        require(len(containers) == 1, 'existing app must have exactly one project container')
        image = containers[0].get('image', '')
        prefix = acr['loginServer'] + '/' + PROJECT + '@'
        require(image.startswith(prefix) and DIGEST_RE.fullmatch(image[len(prefix):]), 'existing app must reference a project digest')
    return {'environment_id': environment['id'], 'pull_id': identities[cfg.pull]['id'],
            'login_server': acr['loginServer'], 'existing': existing}


def publish(cfg, commit, expected_image_id):
    # Listing avoids interpreting authorization, network, or generic CLI errors as absence.
    repositories = cfg.az('acr', 'repository', 'list', '--name', cfg.acr)
    if PROJECT in repositories:
        tags = cfg.az('acr', 'repository', 'show-tags', '--name', cfg.acr, '--repository', PROJECT)
        require(commit not in tags, 'commit tag already exists; verify a partial release read-only or choose approved rollback; never overwrite')
    login = cfg.az('acr', 'show', '--name', cfg.acr, '--resource-group', cfg.group)['loginServer']
    run(['az', 'acr', 'login', '--name', cfg.acr, '--subscription', cfg.subscription, '--only-show-errors'])
    target = login + '/' + PROJECT + ':' + commit
    run(['docker', 'tag', LOCAL_IMAGE, target])
    run(['docker', 'push', target])
    image = PROJECT + ':' + commit
    metadata = cfg.az('acr', 'repository', 'show', '--name', cfg.acr, '--image', image)
    digest = metadata.get('digest', '')
    require(DIGEST_RE.fullmatch(digest), 'registry returned an invalid digest')
    remote = cfg.az('acr', 'manifest', 'show', '--registry', cfg.acr, '--name', PROJECT + '@' + digest)
    # Classic Docker stores identify images by config; containerd stores may
    # identify them by manifest/index. Both are content-addressed identities.
    require(expected_image_id in (digest, remote.get('config', {}).get('digest')),
            'published manifest/config ID differs from tested image')
    for reference in (image, PROJECT + '@' + digest):
        cfg.az('acr', 'repository', 'update', '--name', cfg.acr, '--image', reference,
               '--write-enabled', 'false', '--delete-enabled', 'false')
    verify_locked(cfg, digest, commit)
    return digest


def verify_locked(cfg, digest, commit):
    for reference in (PROJECT + ':' + commit, PROJECT + '@' + digest):
        metadata = cfg.az('acr', 'repository', 'show', '--name', cfg.acr, '--image', reference)
        require(metadata.get('digest') == digest and metadata.get('changeableAttributes', {}).get('writeEnabled') is False
                and metadata.get('changeableAttributes', {}).get('deleteEnabled') is False, 'immutable tag/manifest lock readback failed')


def verify_published(cfg, digest, commit, expected_image_id):
    """Verify an existing immutable image without login, push, or metadata writes."""
    require(COMMIT_RE.fullmatch(commit) and DIGEST_RE.fullmatch(digest)
            and DIGEST_RE.fullmatch(expected_image_id), 'invalid published image identity')
    verify_locked(cfg, digest, commit)
    remote = cfg.az('acr', 'manifest', 'show', '--registry', cfg.acr, '--name', PROJECT + '@' + digest)
    require(expected_image_id in (digest, remote.get('config', {}).get('digest')),
            'published image identity differs from expected tested image')


def manifest(cfg, foundation, digest, commit):
    return {'name': cfg.app, 'location': os.environ.get('AZURE_LOCATION', 'eastus'),
            'type': 'Microsoft.App/containerApps',
            'tags': {'project': PROJECT, 'environment': 'demo', 'cleanup': 'resource-group', 'milestone': '3b', 'commit': commit},
            'identity': {'type': 'UserAssigned', 'userAssignedIdentities': {foundation['pull_id']: {}}},
            'properties': {'environmentId': foundation['environment_id'], 'workloadProfileName': 'Consumption',
                'configuration': {'activeRevisionsMode': 'Single',
                    'ingress': {'external': True, 'targetPort': 8000, 'transport': 'auto', 'allowInsecure': False},
                    'secrets': [], 'registries': [{'server': foundation['login_server'], 'identity': foundation['pull_id']}]},
                'template': {'containers': [{'name': PROJECT, 'image': foundation['login_server'] + '/' + PROJECT + '@' + digest,
                    'resources': {'cpu': 0.25, 'memory': '0.5Gi'}, 'env': [{'name': 'OPENAI_API_KEY', 'value': ''}],
                    'probes': [{'type': kind, 'httpGet': {'path': '/health', 'port': 8000, 'scheme': 'HTTP'},
                                'initialDelaySeconds': 5, 'periodSeconds': 10, 'timeoutSeconds': 3,
                                'failureThreshold': 30 if kind == 'Startup' else 3} for kind in ('Startup', 'Readiness', 'Liveness')]}],
                             'scale': {'minReplicas': 0, 'maxReplicas': 1}}}}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(base, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    with build_opener(NoRedirect()).open(Request(base + path, data=data, headers={'Content-Type': 'application/json'}), timeout=10) as response:
        require(response.status == 200, 'acceptance HTTP status failed')
        body = response.read()
        return body.decode() if path == '/docs' else json.loads(body)


def acceptance(base):
    require(request(base, '/health') == {'status': 'ok'}, 'health check failed')
    require('swagger' in request(base, '/docs').lower(), 'docs check failed')
    summary = request(base, '/reconcile')['summary']
    require(summary == {'total_payouts': 12, 'matched': 7, 'exceptions': 5, 'unmatched_bank': 1}, 'synthetic reconciliation changed')
    exceptions = request(base, '/exceptions')
    require(len(exceptions['payout_exceptions']) == 5 and len(exceptions['unmatched_bank']) == 1, 'exception check failed')
    agent = request(base, '/agent/ask', {'question': 'Summary'})
    require(agent.get('mode') == 'demo-mode' and agent.get('error') is None, 'agent must remain in demo mode')
    require(any(e.get('tool') == 'get_summary' and e.get('result') == summary for e in agent.get('evidence', [])), 'agent evidence mismatch')
    refusal = request(base, '/agent/ask', {'question': 'Execute a refund for P-003.'})
    require(refusal.get('mode') == 'demo-mode' and refusal.get('error') is None and refusal.get('executed_tools') == []
            and refusal.get('evidence') == [] and 'cannot' in refusal.get('answer', '').lower()
            and 'no action was performed' in refusal.get('answer', '').lower(), 'read-only refusal failed')
    return {'health': True, 'docs': True, 'reconcile': True, 'exceptions': True, 'demo_mode': True, 'evidence': True, 'refusal': True}


def retry_acceptance(base):
    for attempt in range(3):
        try:
            return acceptance(base)
        except ReleaseInterrupted:
            raise
        except Exception:
            progress('acceptance', attempt=attempt + 1, outcome='not_ready')
            if attempt == 2:
                raise ReleaseError('acceptance failed after bounded startup retries') from None
            time.sleep(5)


def wait_for_app(cfg, wanted, *, timeout_seconds=1800, poll_seconds=15):
    """Poll read-only readiness after ONE submitted write; never resubmit it."""
    require(timeout_seconds > 0 and poll_seconds > 0, 'polling limits must be positive')
    started = time.monotonic()
    deadline = started + timeout_seconds
    previous = None
    last_report = -30
    while time.monotonic() < deadline:
        app = cfg.az('containerapp', 'show', '--name', cfg.app, '--resource-group', cfg.group)
        props = app['properties']
        state = props.get('provisioningState')
        require(state not in ('Failed', 'Canceled', 'Deleting'), 'terminal app provisioning state; inspect partial deployment')
        require(state in ('InProgress', 'Succeeded'), 'unknown app provisioning state; inspect partial deployment')
        elapsed = int(time.monotonic() - started)
        if state != previous or elapsed - last_report >= 30:
            progress('provisioning', app_state=state, elapsed_seconds=elapsed)
            previous, last_report = state, elapsed
        revision_name = props.get('latestRevisionName')
        if state == 'Succeeded' and revision_name and props.get('latestReadyRevisionName') == revision_name:
            containers = props.get('template', {}).get('containers', [])
            require(len(containers) == 1, 'deployed container configuration differs')
            # An accepted update can initially return the previous ready image.
            # Wait for the selected digest; never accept the old revision or resubmit.
            if containers[0].get('image') == wanted:
                revision = cfg.az('containerapp', 'revision', 'show', '--name', cfg.app,
                                  '--resource-group', cfg.group, '--revision', revision_name)
                rp = revision['properties']
                require(rp.get('provisioningState') not in ('Failed', 'Deprovisioned')
                        and rp.get('healthState') != 'Unhealthy', 'terminal revision state; inspect partial deployment')
                if rp.get('active') is True and rp.get('provisioningState') == 'Provisioned' and rp.get('healthState') == 'Healthy':
                    rc = rp.get('template', {}).get('containers', [])
                    require(revision.get('name') == revision_name and len(rc) == 1 and rc[0].get('image') == wanted,
                            'ready revision digest differs from selected image')
                    require(time.monotonic() < deadline, 'provisioning deadline exceeded; Azure operation may continue; verify without retrying')
                    return app
        time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))
    raise ReleaseError('provisioning deadline exceeded; Azure operation may continue; verify without retrying')


def apply(cfg, foundation, digest, commit):
    with tempfile.TemporaryDirectory(prefix='recon-release-') as directory:
        path = Path(directory) / 'containerapp.json'
        path.write_text(json.dumps(manifest(cfg, foundation, digest, commit)))
        cfg.az('containerapp', 'update' if foundation['existing'] else 'create', '--name', cfg.app,
               '--resource-group', cfg.group, '--yaml', str(path), '--no-wait')
    wanted = foundation['login_server'] + '/' + PROJECT + '@' + digest
    app = wait_for_app(cfg, wanted)
    fqdn = app['properties']['configuration']['ingress']['fqdn']
    require(isinstance(fqdn, str) and fqdn.startswith(cfg.app + '.') and fqdn.endswith('.azurecontainerapps.io'), 'invalid Azure app FQDN')
    progress('acceptance', outcome='starting')
    return retry_acceptance('https://' + fqdn)


def receipt_base(cfg, operation, commit, image_id):
    return {'schema_version': 1, 'kind': 'workflow-release-receipt', 'operation': operation,
            'status': 'in_progress', 'phase': 'preflight', 'repository': cfg.repository,
            'run_id': os.environ.get('GITHUB_RUN_ID'), 'workflow_commit': os.environ.get('GITHUB_SHA'),
            'source_run_id': os.environ.get('ROLLBACK_SOURCE_RUN_ID') if operation == 'rollback' else None,
            'commit': commit, 'image_id': image_id, 'digest': None, 'previous_digest': None,
            'resource_group': cfg.group, 'app': cfg.app, 'registry': cfg.acr, 'acceptance': None}


def perform(cfg, operation, commit, image_id, receipt_path, digest=None, *, expected_existing=None):
    receipt = receipt_base(cfg, operation, commit, image_id)
    write_evidence(receipt_path, receipt, exclusive=True)
    try:
        foundation = preflight(cfg)
        require(expected_existing is None or bool(foundation['existing']) == expected_existing,
                'app presence changed since preparation; inspect without recreating or overwriting')
        require(operation != 'rollback' or foundation['existing'], 'rollback requires an existing app at update time')
        if foundation['existing']:
            receipt['previous_digest'] = foundation['existing']['properties']['template']['containers'][0]['image'].split('@')[1]
        receipt['phase'] = 'publishing' if digest is None else 'verifying-published-image'
        write_evidence(receipt_path, receipt)
        receipt['digest'] = digest or publish(cfg, commit, image_id)
        receipt['phase'] = 'rollout-and-acceptance'
        write_evidence(receipt_path, receipt)
        receipt['acceptance'] = apply(cfg, foundation, receipt['digest'], commit)
        require(isinstance(receipt['acceptance'], dict) and all(receipt['acceptance'].get(key) is True for key in
                ('health', 'docs', 'reconcile', 'exceptions', 'demo_mode', 'evidence', 'refusal')),
                'release acceptance evidence is incomplete')
        receipt['status'] = 'succeeded'
        receipt['phase'] = 'accepted'
    except BaseException as exc:
        receipt['status'] = 'interrupted' if not isinstance(exc, Exception) or isinstance(exc, ReleaseInterrupted) else 'failed'
        receipt['error'] = str(exc) if isinstance(exc, ReleaseError) else type(exc).__name__
        receipt['rollback_instruction'] = 'No automatic rollback. Azure operations may continue; inspect with the read-only verifier before a separately approved recovery.'
        raise
    finally:
        write_evidence(receipt_path, receipt)
    return receipt


def deploy(expected_image_id, commit, receipt_path):
    cfg = deploy_inputs(expected_image_id, commit)
    return perform(cfg, 'deploy', commit, expected_image_id, receipt_path)


def deploy_inputs(expected_image_id, commit):
    cfg = gated_config('deploy')
    require(COMMIT_RE.fullmatch(commit), 'commit must be a full lowercase 40-hex SHA')
    require(DIGEST_RE.fullmatch(expected_image_id), 'expected image ID must be sha256 plus 64 hex digits')
    require(run(['git', 'rev-parse', 'HEAD']) == commit, 'commit does not match checkout HEAD')
    actual = run(['docker', 'image', 'inspect', LOCAL_IMAGE, '--format', '{{.Id}}'])
    require(actual == expected_image_id, 'loaded image ID differs from smoke-tested artifact')
    return cfg


def checkpoint_context(cfg, commit, image_id):
    run_id, attempt = os.environ.get('GITHUB_RUN_ID', ''), os.environ.get('GITHUB_RUN_ATTEMPT', '')
    require(re.fullmatch(r'[0-9]+', run_id) and re.fullmatch(r'[0-9]+', attempt)
            and os.environ.get('GITHUB_SHA') == commit, 'checkpoint requires the current GitHub run, attempt and commit')
    binding = {**asdict(cfg), 'location': os.environ.get('AZURE_LOCATION', 'eastus')}
    return {'repository': cfg.repository, 'run_id': run_id, 'run_attempt': attempt,
            'workflow_commit': commit, 'commit': commit, 'image_id': image_id,
            'configuration_fingerprint': hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()}


def prepare(expected_image_id, commit, checkpoint_path):
    """Publish once and preserve a checkpoint BEFORE any Container App write."""
    cfg = deploy_inputs(expected_image_id, commit)
    checkpoint = {'schema_version': 1, 'kind': 'release-checkpoint', 'status': 'in_progress',
                  'phase': 'preflight', 'digest': None, **checkpoint_context(cfg, commit, expected_image_id)}
    write_evidence(checkpoint_path, checkpoint, exclusive=True)
    try:
        foundation = preflight(cfg)
        checkpoint['app_existed'] = bool(foundation['existing'])
        checkpoint['phase'] = 'publishing'
        write_evidence(checkpoint_path, checkpoint)
        progress('publishing', outcome='starting')
        checkpoint['digest'] = publish(cfg, commit, expected_image_id)
        checkpoint.update(status='prepared', phase='published')
        progress('published', outcome='locked-and-verified')
    except BaseException as exc:
        checkpoint['status'] = 'interrupted' if not isinstance(exc, Exception) or isinstance(exc, ReleaseInterrupted) else 'failed'
        checkpoint['error'] = str(exc) if isinstance(exc, ReleaseError) else type(exc).__name__
        raise
    finally:
        write_evidence(checkpoint_path, checkpoint)
    return checkpoint


def complete(expected_image_id, commit, checkpoint_path, receipt_path):
    """Consume ONLY this run's checkpoint; no publication, unlock, or re-tag."""
    cfg = deploy_inputs(expected_image_id, commit)
    try:
        checkpoint = json.loads(Path(checkpoint_path).read_text())
    except (OSError, ValueError):
        raise ReleaseError('prepared checkpoint cannot be read') from None
    require(isinstance(checkpoint, dict) and checkpoint.get('schema_version') == 1
            and checkpoint.get('kind') == 'release-checkpoint' and checkpoint.get('status') == 'prepared'
            and checkpoint.get('phase') == 'published' and isinstance(checkpoint.get('app_existed'), bool),
            'checkpoint is not a prepared publication')
    for key, expected in checkpoint_context(cfg, commit, expected_image_id).items():
        require(checkpoint.get(key) == expected, 'checkpoint provenance/configuration mismatch: ' + key)
    digest = checkpoint.get('digest', '')
    require(isinstance(digest, str) and DIGEST_RE.fullmatch(digest), 'checkpoint contains invalid digest')
    verify_published(cfg, digest, commit, expected_image_id)
    return perform(cfg, 'deploy', commit, expected_image_id, receipt_path, digest,
                   expected_existing=checkpoint['app_existed'])


def interrupt_release(signum, frame):
    raise ReleaseInterrupted('release interrupted; Azure operation may continue; verify without retrying')


def rollback(receipt_path, digest, output_path):
    cfg = gated_config('rollback')
    require(DIGEST_RE.fullmatch(digest), 'rollback digest is invalid')
    prior = json.loads(receipt_path.read_text())
    require(prior.get('kind', 'workflow-release-receipt') == 'workflow-release-receipt',
            'rollback requires a workflow release receipt, never operator evidence')
    require(prior.get('schema_version') == 1 and prior.get('status') == 'succeeded' and prior.get('digest') == digest,
            'rollback requires a successful approved receipt for exactly this digest')
    for key, value in {'repository': cfg.repository, 'resource_group': cfg.group, 'app': cfg.app, 'registry': cfg.acr,
                       'run_id': os.environ.get('ROLLBACK_SOURCE_RUN_ID'), 'workflow_commit': os.environ.get('ROLLBACK_SOURCE_COMMIT')}.items():
        require(bool(value) and prior.get(key) == value, 'rollback receipt provenance/configuration mismatch: ' + key)
    commit = prior.get('commit', '')
    require(COMMIT_RE.fullmatch(commit) and DIGEST_RE.fullmatch(prior.get('image_id', '')), 'receipt image evidence is invalid')
    require(prior.get('acceptance') and all(prior['acceptance'].get(k) is True for k in ('health', 'docs', 'reconcile', 'exceptions', 'demo_mode', 'evidence', 'refusal')),
            'receipt lacks successful acceptance evidence')
    foundation = preflight(cfg)
    require(foundation['existing'], 'rollback requires an existing project app')
    verify_locked(cfg, digest, commit)
    target = foundation['login_server'] + '/' + PROJECT + '@' + digest
    run(['az', 'acr', 'login', '--name', cfg.acr, '--subscription', cfg.subscription, '--only-show-errors'])
    run(['docker', 'pull', target])
    require(run(['docker', 'image', 'inspect', target, '--format', '{{.Id}}']) == prior['image_id'], 'rollback image ID differs from receipt')
    container = run(['docker', 'run', '--detach', '--publish', '127.0.0.1::8000', '--env', 'OPENAI_API_KEY=', target])
    try:
        port = run(['docker', 'port', container, '8000/tcp'])
        require(re.fullmatch(r'127\.0\.0\.1:[0-9]+', port), 'invalid local smoke port')
        retry_acceptance('http://' + port)
    finally:
        run(['docker', 'rm', '--force', container])
    return perform(cfg, 'rollback', commit, prior['image_id'], output_path, digest)


def main(argv=None):
    signal.signal(signal.SIGTERM, interrupt_release)
    signal.signal(signal.SIGINT, interrupt_release)
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='operation', required=True)
    deploy_parser = subs.add_parser('deploy')
    deploy_parser.add_argument('--expected-image-id', required=True)
    deploy_parser.add_argument('--commit', required=True)
    deploy_parser.add_argument('--receipt', type=Path, default=Path('release-receipt.json'))
    for operation in ('prepare', 'complete'):
        command = subs.add_parser(operation)
        command.add_argument('--expected-image-id', required=True)
        command.add_argument('--commit', required=True)
        command.add_argument('--checkpoint', type=Path, default=Path('release-progress.json'))
        if operation == 'complete':
            command.add_argument('--receipt', type=Path, default=Path('release-receipt.json'))
    rollback_parser = subs.add_parser('rollback')
    rollback_parser.add_argument('--receipt', required=True, type=Path)
    rollback_parser.add_argument('--digest', required=True)
    rollback_parser.add_argument('--output', type=Path, default=Path('release-receipt.json'))
    args = parser.parse_args(argv)
    try:
        if args.operation == 'deploy':
            deploy(args.expected_image_id, args.commit, args.receipt)
        elif args.operation == 'prepare':
            prepare(args.expected_image_id, args.commit, args.checkpoint)
        elif args.operation == 'complete':
            complete(args.expected_image_id, args.commit, args.checkpoint, args.receipt)
        else:
            rollback(args.receipt, args.digest, args.output)
    except (ReleaseError, OSError, ValueError, KeyError, TypeError) as exc:
        print('Release refused/failed: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
