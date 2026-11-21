"""Release interruption and recovery safeguards; external services stay offline."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError

import pytest

from scripts import azure_release as release

SHA = 'a' * 40
IMAGE = 'sha256:' + 'b' * 64
DIGEST = 'sha256:' + 'c' * 64


@pytest.mark.parametrize('expected', [IMAGE, DIGEST])
def test_read_only_published_verification_accepts_both_docker_identity_forms(monkeypatch, expected):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    def az(*args):
        if args[:3] == ('acr', 'repository', 'show'):
            return {'digest': DIGEST, 'changeableAttributes': {'writeEnabled': False, 'deleteEnabled': False}}
        if args[:3] == ('acr', 'manifest', 'show'):
            return {'config': {'digest': IMAGE}}
        pytest.fail('Read-only verifier attempted another operation: ' + str(args))
    monkeypatch.setattr(cfg, 'az', az)
    release.verify_published(cfg, DIGEST, SHA, expected)


def test_read_only_published_verification_rejects_wrong_image_identity(monkeypatch):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    def az(*args):
        if args[:3] == ('acr', 'repository', 'show'):
            return {'digest': DIGEST, 'changeableAttributes': {'writeEnabled': False, 'deleteEnabled': False}}
        if args[:3] == ('acr', 'manifest', 'show'):
            return {'config': {'digest': IMAGE}}
        pytest.fail(str(args))
    monkeypatch.setattr(cfg, 'az', az)
    with pytest.raises(release.ReleaseError, match='identity'):
        release.verify_published(cfg, DIGEST, SHA, 'sha256:' + 'd' * 64)


def test_command_timeout_reports_uncertain_outcome_without_retry_or_payload(monkeypatch):
    calls = []
    def stalled(args, **kwargs):
        calls.append(args)
        assert 0 < kwargs['timeout'] <= 300
        raise subprocess.TimeoutExpired(args, kwargs['timeout'], output='private-account-info', stderr='secret-token')
    monkeypatch.setattr(release.subprocess, 'run', stalled)
    with pytest.raises(release.ReleaseError, match='timed out') as error:
        release.run(['az', 'containerapp', 'create'])
    assert len(calls) == 1
    assert 'private-account-info' not in str(error.value) and 'secret-token' not in str(error.value)


def test_acceptance_retries_are_bounded_and_remain_failure(monkeypatch):
    attempts = []
    def fail(base):
        attempts.append(base)
        raise release.ReleaseError('not ready')
    monkeypatch.setattr(release, 'acceptance', fail)
    monkeypatch.setattr(release.time, 'sleep', lambda _: None)
    with pytest.raises(release.ReleaseError, match='acceptance'):
        release.retry_acceptance('https://demo.example')
    assert len(attempts) == 3


def test_acceptance_http_does_not_follow_redirects_to_another_route():
    destinations = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/health':
                self.send_response(302)
                self.send_header('Location', '/destination')
                self.end_headers()
            else:
                destinations.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with pytest.raises(HTTPError) as error:
            release.request('http://127.0.0.1:' + str(server.server_port), '/health')
        assert error.value.code == 302
        assert destinations == []
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


class Clock:
    def __init__(self):
        self.now = 0
    def monotonic(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


def ready_app(image):
    return {'properties': {'provisioningState': 'Succeeded', 'latestRevisionName': 'app--new',
        'latestReadyRevisionName': 'app--new', 'template': {'containers': [{'image': image}]},
        'configuration': {'ingress': {'fqdn': 'ca-payment-recon-demo.example.azurecontainerapps.io'}}}}


def test_rollout_poll_waits_for_ready_revision_without_reissuing_write(monkeypatch):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    wanted = 'registry.azurecr.io/payment-recon-agent@' + DIGEST
    clock = Clock(); states = iter(['InProgress', 'Succeeded']); calls = []
    def az(*args):
        calls.append(args)
        if args[:3] == ('containerapp', 'show', '--name'):
            value = ready_app(wanted);value['properties']['provisioningState'] = next(states)
            return value
        if args[:3] == ('containerapp', 'revision', 'show'):
            return {'name': 'app--new', 'properties': {'healthState': 'Healthy', 'provisioningState': 'Provisioned',
                'active': True, 'template': {'containers': [{'image': wanted}]}}}
        pytest.fail('Unexpected write during polling: ' + str(args))
    monkeypatch.setattr(cfg, 'az', az)
    monkeypatch.setattr(release.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(release.time, 'sleep', clock.sleep)
    result = release.wait_for_app(cfg, wanted, timeout_seconds=30, poll_seconds=15)
    assert result['properties']['latestReadyRevisionName'] == 'app--new' and clock.now == 15
    assert not any(set(a) & {'create', 'update', 'delete'} for a in calls)


def test_rollout_tolerates_stale_ready_previous_digest_after_accepted_update(monkeypatch):
    cfg = release.Config('sub', 'registry', 'owner/repo');clock = Clock()
    wanted = 'registry.azurecr.io/payment-recon-agent@' + DIGEST
    states = iter([ready_app('registry.azurecr.io/payment-recon-agent@' + IMAGE), ready_app(wanted)])
    def az(*args):
        if args[:3] == ('containerapp', 'show', '--name'): return next(states)
        assert args[:3] == ('containerapp', 'revision', 'show')
        return {'name': 'app--new', 'properties': {'healthState': 'Healthy', 'provisioningState': 'Provisioned',
            'active': True, 'template': {'containers': [{'image': wanted}]}}}
    monkeypatch.setattr(cfg, 'az', az)
    monkeypatch.setattr(release.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(release.time, 'sleep', clock.sleep)
    result = release.wait_for_app(cfg, wanted, timeout_seconds=30, poll_seconds=15)
    assert result['properties']['template']['containers'][0]['image'] == wanted and clock.now == 15


def test_ready_previous_digest_never_counts_as_success(monkeypatch):
    cfg = release.Config('sub', 'registry', 'owner/repo');clock = Clock()
    def az(*args):
        assert args[:3] == ('containerapp', 'show', '--name')
        return ready_app('registry.azurecr.io/payment-recon-agent@' + IMAGE)
    monkeypatch.setattr(cfg, 'az', az)
    monkeypatch.setattr(release.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(release.time, 'sleep', clock.sleep)
    with pytest.raises(release.ReleaseError, match='deadline'):
        release.wait_for_app(cfg, 'registry.azurecr.io/payment-recon-agent@' + DIGEST,
                             timeout_seconds=30, poll_seconds=15)
    assert clock.now == 30


def test_rollout_deadline_preserves_uncertain_partial_deployment_without_retry(monkeypatch):
    cfg = release.Config('sub', 'registry', 'owner/repo'); clock = Clock(); reads = []
    def az(*args):
        assert args[:3] == ('containerapp', 'show', '--name')
        reads.append(args)
        return {'properties': {'provisioningState': 'InProgress'}}
    monkeypatch.setattr(cfg, 'az', az)
    monkeypatch.setattr(release.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(release.time, 'sleep', clock.sleep)
    with pytest.raises(release.ReleaseError, match='deadline'):
        release.wait_for_app(cfg, 'registry.azurecr.io/payment-recon-agent@' + DIGEST,
                             timeout_seconds=30, poll_seconds=15)
    assert clock.now == 30 and len(reads) <= 3


@pytest.mark.parametrize('state', ['Failed', 'Canceled', 'Deleting'])
def test_terminal_rollout_state_stops_immediately(monkeypatch, state):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    monkeypatch.setattr(cfg, 'az', lambda *args: {'properties': {'provisioningState': state}})
    monkeypatch.setattr(release.time, 'sleep', lambda _: pytest.fail('terminal state was retried'))
    with pytest.raises(release.ReleaseError, match='terminal'):
        release.wait_for_app(cfg, 'registry.azurecr.io/payment-recon-agent@' + DIGEST)


def test_apply_submits_once_with_no_wait_then_polls_read_only(monkeypatch):
    cfg = release.Config('sub', 'registry', 'owner/repo'); writes = []
    foundation = {'existing': None, 'login_server': 'registry.azurecr.io', 'environment_id': '/env', 'pull_id': '/pull'}
    def az(*args):
        assert args[:2] == ('containerapp', 'create')
        assert '--no-wait' in args
        writes.append(args)
    monkeypatch.setattr(cfg, 'az', az)
    monkeypatch.setattr(release, 'wait_for_app', lambda *a, **kw: ready_app('registry.azurecr.io/payment-recon-agent@' + DIGEST))
    monkeypatch.setattr(release, 'retry_acceptance', lambda _: {'health': True})
    assert release.apply(cfg, foundation, DIGEST, SHA) == {'health': True}
    assert len(writes) == 1


def test_progress_evidence_exists_before_publication_and_survives_interrupt(monkeypatch, tmp_path):
    cfg = release.Config('sub', 'registry', 'owner/repo'); path = tmp_path / 'receipt.json'
    monkeypatch.setattr(release, 'preflight', lambda c: {'existing': None})
    def interrupted(*args):
        checkpoint = json.loads(path.read_text())
        assert checkpoint['status'] == 'in_progress' and checkpoint['phase'] == 'publishing'
        raise KeyboardInterrupt()
    monkeypatch.setattr(release, 'publish', interrupted)
    monkeypatch.setattr(release, 'apply', lambda *args: pytest.fail('write after interruption'))
    with pytest.raises(KeyboardInterrupt):
        release.perform(cfg, 'deploy', SHA, IMAGE, path)
    checkpoint = json.loads(path.read_text())
    assert checkpoint['status'] == 'interrupted' and checkpoint['digest'] is None
    assert checkpoint['kind'] == 'workflow-release-receipt'


def test_known_digest_is_persisted_before_azure_write(monkeypatch, tmp_path):
    cfg = release.Config('sub', 'registry', 'owner/repo');path = tmp_path / 'receipt.json'
    monkeypatch.setattr(release, 'preflight', lambda c: {'existing': None})
    monkeypatch.setattr(release, 'publish', lambda *args: DIGEST)
    def fail(*args):
        checkpoint = json.loads(path.read_text())
        assert checkpoint['digest'] == DIGEST and checkpoint['status'] == 'in_progress'
        raise release.ReleaseError('partial Azure operation')
    monkeypatch.setattr(release, 'apply', fail)
    with pytest.raises(release.ReleaseError):release.perform(cfg, 'deploy', SHA, IMAGE, path)
    assert json.loads(path.read_text())['status'] == 'failed'


def deploy_context(monkeypatch):
    for key, value in {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': 'refs/heads/main',
        'AZURE_DEPLOYMENT_ENABLED': 'true', 'DEPLOY_CONFIRMATION': 'DEPLOY SYNTHETIC DEMO',
        'AZURE_SUBSCRIPTION_ID': 'sub', 'AZURE_ACR_NAME': 'registry', 'GITHUB_REPOSITORY': 'owner/repo',
        'GITHUB_RUN_ID': '42', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': SHA}.items():
        monkeypatch.setenv(key, value)
    def run(args):
        if args[:2] == ['git', 'rev-parse']: return SHA
        if args[:3] == ['docker', 'image', 'inspect']: return IMAGE
        pytest.fail('Unexpected external command: ' + str(args))
    monkeypatch.setattr(release, 'run', run)
    monkeypatch.setattr(release, 'preflight', lambda cfg: {'existing': None})


def test_prepare_checkpoints_locked_image_before_rollout_and_complete_never_pushes(monkeypatch, tmp_path):
    deploy_context(monkeypatch)
    checkpoint = tmp_path / 'prepared.json'; receipt = tmp_path / 'receipt.json'
    def publish(*args):
        pending = json.loads(checkpoint.read_text())
        assert pending['kind'] == 'release-checkpoint' and pending['phase'] == 'publishing'
        return DIGEST
    monkeypatch.setattr(release, 'publish', publish)
    monkeypatch.setattr(release, 'apply', lambda *args: pytest.fail('prepare attempted Azure write'))
    release.prepare(IMAGE, SHA, checkpoint)
    prepared = json.loads(checkpoint.read_text())
    assert prepared['status'] == 'prepared' and prepared['digest'] == DIGEST
    assert 'acceptance' not in prepared
    monkeypatch.setattr(release, 'publish', lambda *args: pytest.fail('complete republished image'))
    monkeypatch.setattr(release, 'verify_published', lambda *args: None)
    monkeypatch.setattr(release, 'apply', lambda *args: {k: True for k in
        ('health', 'docs', 'reconcile', 'exceptions', 'demo_mode', 'evidence', 'refusal')})
    release.complete(IMAGE, SHA, checkpoint, receipt)
    result = json.loads(receipt.read_text())
    assert result['kind'] == 'workflow-release-receipt' and result['status'] == 'succeeded'
    assert result['digest'] == DIGEST and result['run_id'] == '42'
    assert json.loads(checkpoint.read_text()) == prepared


@pytest.mark.parametrize('change', [
    {'run_id': '99'}, {'run_attempt': '2'}, {'configuration_fingerprint': 'wrong'},
    {'kind': 'post-incident-verification'}, {'status': 'failed'}, {'commit': 'd' * 40},
    {'digest': 'not-a-digest'}, {'image_id': DIGEST}, {'app_existed': None},
])
def test_complete_rejects_stale_or_wrong_checkpoint_before_any_cloud_check(monkeypatch, tmp_path, change):
    deploy_context(monkeypatch);path = tmp_path / 'prepared.json'
    monkeypatch.setattr(release, 'publish', lambda *args: DIGEST)
    release.prepare(IMAGE, SHA, path)
    value = json.loads(path.read_text());value.update(change);path.write_text(json.dumps(value))
    monkeypatch.setattr(release, 'verify_published', lambda *args: pytest.fail('invalid checkpoint reached registry'))
    monkeypatch.setattr(release, 'preflight', lambda *args: pytest.fail('invalid checkpoint reached Azure'))
    with pytest.raises(release.ReleaseError, match='checkpoint'):
        release.complete(IMAGE, SHA, path, tmp_path / 'receipt.json')


def test_preparation_preserves_interrupted_publication_without_retry(monkeypatch, tmp_path):
    deploy_context(monkeypatch);path = tmp_path / 'prepared.json';calls = []
    def publish(*args):
        calls.append(args)
        raise release.ReleaseInterrupted('signal')
    monkeypatch.setattr(release, 'publish', publish)
    with pytest.raises(release.ReleaseInterrupted):release.prepare(IMAGE, SHA, path)
    result = json.loads(path.read_text())
    assert len(calls) == 1 and result['status'] == 'interrupted' and result['phase'] == 'publishing'
    assert result['digest'] is None and result['kind'] == 'release-checkpoint'


def test_operator_evidence_never_qualifies_as_normal_rollback_receipt(monkeypatch, tmp_path):
    deploy_context(monkeypatch)
    monkeypatch.setenv('DEPLOY_CONFIRMATION', 'ROLLBACK SYNTHETIC DEMO')
    monkeypatch.setenv('ROLLBACK_SOURCE_RUN_ID', '42')
    monkeypatch.setenv('ROLLBACK_SOURCE_COMMIT', SHA)
    path = tmp_path / 'operator.json'
    path.write_text(json.dumps({'schema_version': 1, 'kind': 'post-incident-verification',
        'status': 'succeeded', 'digest': DIGEST, 'repository': 'owner/repo', 'run_id': '42',
        'workflow_commit': SHA, 'commit': SHA, 'image_id': IMAGE, 'registry': 'registry',
        'app': 'ca-payment-recon-demo', 'resource_group': 'rg-payment-recon-demo',
        'acceptance': {k: True for k in ('health', 'docs', 'reconcile', 'exceptions', 'demo_mode', 'evidence', 'refusal')}}))
    monkeypatch.setattr(release, 'preflight', lambda *args: pytest.fail('operator evidence triggered Azure access'))
    with pytest.raises(release.ReleaseError, match='workflow release receipt'):
        release.rollback(path, DIGEST, tmp_path / 'receipt.json')


def test_complete_refuses_to_recreate_app_that_disappeared_after_prepare(monkeypatch, tmp_path):
    deploy_context(monkeypatch); path = tmp_path / 'prepared.json'
    monkeypatch.setattr(release, 'preflight', lambda cfg: {'existing': {'name': cfg.app}})
    monkeypatch.setattr(release, 'publish', lambda *args: DIGEST)
    release.prepare(IMAGE, SHA, path)
    monkeypatch.setattr(release, 'preflight', lambda cfg: {'existing': None})
    monkeypatch.setattr(release, 'verify_published', lambda *args: None)
    monkeypatch.setattr(release, 'apply', lambda *args: pytest.fail('recreated disappeared app'))
    with pytest.raises(release.ReleaseError, match='presence'):
        release.complete(IMAGE, SHA, path, tmp_path / 'receipt.json')


@pytest.mark.parametrize('termination,status', [(signal.SIGTERM, 'interrupted'), (signal.SIGKILL, 'in_progress')])
def test_real_process_interruption_preserves_non_success_checkpoint(tmp_path, termination, status):
    path = tmp_path / 'receipt.json'
    code = '''
import signal,sys,time
from pathlib import Path
from scripts import azure_release as r
signal.signal(signal.SIGTERM,r.interrupt_release)
r.preflight=lambda cfg:{'existing':None}
r.publish=lambda *args:time.sleep(60)
r.perform(r.Config('sub','registry','owner/repo'),'deploy','a'*40,'sha256:'+'b'*64,Path(sys.argv[1]))
'''
    child = subprocess.Popen([sys.executable, '-c', code, str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.exists() and json.loads(path.read_text()).get('phase') == 'publishing': break
            time.sleep(.01)
        else: pytest.fail('child did not reach checkpoint before write')
        os.kill(child.pid, termination)
        child.communicate(timeout=5)
        value = json.loads(path.read_text())
        assert child.returncode != 0 and value['status'] == status and value['phase'] == 'publishing'
        assert value['kind'] == 'workflow-release-receipt' and value['acceptance'] is None
    finally:
        if child.poll() is None:
            child.kill();child.communicate()


def test_existing_evidence_is_never_overwritten_by_a_new_release(monkeypatch, tmp_path):
    cfg = release.Config('sub', 'registry', 'owner/repo');path = tmp_path / 'receipt.json'
    original = '{"status":"succeeded","original":true}\n';path.write_text(original)
    monkeypatch.setattr(release, 'preflight', lambda *args: pytest.fail('cloud read before refusing overwrite'))
    with pytest.raises(release.ReleaseError, match='already exists'):
        release.perform(cfg, 'deploy', SHA, IMAGE, path)
    assert path.read_text() == original


@pytest.mark.parametrize('operation', ['prepare', 'complete'])
@pytest.mark.parametrize('key,value', [('AZURE_DEPLOYMENT_ENABLED', 'false'), ('GITHUB_REF', 'refs/heads/feature'),
    ('GITHUB_EVENT_NAME', 'push'), ('DEPLOY_CONFIRMATION', '')])
def test_both_workflow_phases_require_original_main_only_release_gates(monkeypatch, tmp_path, operation, key, value):
    deploy_context(monkeypatch);monkeypatch.setenv(key, value)
    monkeypatch.setattr(release, 'run', lambda *args: pytest.fail('command before gate'))
    with pytest.raises(release.ReleaseError):
        if operation == 'prepare':release.prepare(IMAGE, SHA, tmp_path / 'checkpoint.json')
        else:release.complete(IMAGE, SHA, tmp_path / 'checkpoint.json', tmp_path / 'receipt.json')


def test_unknown_azure_state_is_not_printed_in_progress_logs(monkeypatch, capsys):
    cfg = release.Config('sub', 'registry', 'owner/repo')
    monkeypatch.setattr(cfg, 'az', lambda *args: {'properties': {'provisioningState': 'private-account-info'}})
    with pytest.raises(release.ReleaseError, match='unknown'):
        release.wait_for_app(cfg, 'registry.azurecr.io/payment-recon-agent@' + DIGEST)
    assert 'private-account-info' not in capsys.readouterr().out


def test_partial_acceptance_cannot_mark_workflow_receipt_successful(monkeypatch, tmp_path):
    cfg = release.Config('sub', 'registry', 'owner/repo');path = tmp_path / 'receipt.json'
    monkeypatch.setattr(release, 'preflight', lambda cfg: {'existing': None})
    monkeypatch.setattr(release, 'publish', lambda *args: DIGEST)
    monkeypatch.setattr(release, 'apply', lambda *args: {'health': True})
    with pytest.raises(release.ReleaseError, match='acceptance'):
        release.perform(cfg, 'deploy', SHA, IMAGE, path)
    assert json.loads(path.read_text())['status'] == 'failed'


def test_acceptance_does_not_swallow_release_interruption(monkeypatch):
    attempts = []
    def interrupt(base):
        attempts.append(base)
        raise release.ReleaseInterrupted('signal')
    monkeypatch.setattr(release, 'acceptance', interrupt)
    monkeypatch.setattr(release.time, 'sleep', lambda _: pytest.fail('signal was treated as retryable startup'))
    with pytest.raises(release.ReleaseInterrupted):release.retry_acceptance('https://demo.example')
    assert len(attempts) == 1


def test_signal_during_http_acceptance_keeps_normal_receipt_interrupted(monkeypatch, tmp_path):
    cfg = release.Config('sub', 'registry', 'owner/repo');path = tmp_path / 'receipt.json'
    monkeypatch.setattr(release, 'preflight', lambda cfg: {'existing': None})
    monkeypatch.setattr(release, 'publish', lambda *args: DIGEST)
    def interrupt(base):raise release.ReleaseInterrupted('signal')
    monkeypatch.setattr(release, 'acceptance', interrupt)
    monkeypatch.setattr(release.time, 'sleep', lambda _: pytest.fail('signal was swallowed'))
    monkeypatch.setattr(release, 'apply', lambda *args: release.retry_acceptance('https://demo.example'))
    with pytest.raises(release.ReleaseInterrupted):release.perform(cfg, 'deploy', SHA, IMAGE, path)
    evidence = json.loads(path.read_text())
    assert evidence['status'] == 'interrupted' and evidence['digest'] == DIGEST
