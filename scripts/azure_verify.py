#!/usr/bin/env python3
"""Local read-only post-incident checks; never authorizes a release or rollback."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

if __package__:
    from . import azure_release as release
else:
    import azure_release as release


def configuration():
    required = ('AZURE_SUBSCRIPTION_ID', 'AZURE_ACR_NAME', 'GITHUB_REPOSITORY')
    release.require(all(os.environ.get(key) for key in required), 'verification configuration is missing')
    cfg = release.Config(*(os.environ[key] for key in required))
    release.require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', cfg.repository), 'invalid repository')
    for field, key in [('group', 'RESOURCE_GROUP'), ('app', 'APP_NAME'), ('environment', 'CONTAINERAPPS_ENVIRONMENT'),
                       ('workspace', 'LOG_ANALYTICS_WORKSPACE'), ('pull', 'PULL_IDENTITY_NAME'), ('cd', 'CD_IDENTITY_NAME')]:
        setattr(cfg, field, os.environ.get('AZURE_' + key) or getattr(cfg, field))
    return cfg


def write_evidence(path, evidence, *, initial=False):
    """Publish complete JSON atomically; initial publication must never replace a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    evidence['timestamp'] = datetime.now(timezone.utc).isoformat()
    fd, temporary = tempfile.mkstemp(prefix='.verification-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(json.dumps(evidence, indent=2) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        if initial:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def source_run(cfg, run_id, commit, evidence):
    source = json.loads(release.run(['gh', 'api', f'repos/{cfg.repository}/actions/runs/{run_id}']))
    conclusion = source.get('conclusion')
    allowed = {'success', 'failure', 'neutral', 'cancelled', 'skipped', 'timed_out', 'action_required', 'stale', 'startup_failure'}
    if conclusion in allowed:
        evidence['source_run_conclusion'] = conclusion
    release.require(str(source.get('id')) == run_id and source.get('status') == 'completed' and conclusion in allowed
                    and source.get('event') == 'workflow_dispatch' and source.get('head_branch') == 'main'
                    and source.get('head_sha') == commit and source.get('path') == '.github/workflows/ci.yml'
                    and source.get('repository', {}).get('full_name') == cfg.repository, 'source run provenance differs')


def runtime_template(template, foundation, digest):
    """Check runtime bounds on each ARM projection, ignoring generated defaults."""
    containers = template.get('containers', [])
    scale = template.get('scale', {})
    release.require(len(containers) == 1 and not template.get('initContainers') and not template.get('volumes')
                    and scale.get('minReplicas') == 0 and scale.get('maxReplicas') == 1 and not scale.get('rules'),
                    'runtime workload differs')
    container = containers[0]
    release.require(container.get('name') == release.PROJECT
                    and container.get('image') == foundation['login_server'] + '/' + release.PROJECT + '@' + digest
                    and container.get('resources', {}).get('cpu') == .25 and container['resources'].get('memory') == '0.5Gi'
                    and container.get('env') == [{'name': 'OPENAI_API_KEY', 'value': ''}]
                    and not container.get('command') and not container.get('args') and not container.get('volumeMounts'),
                    'runtime container differs')
    probes = container.get('probes', [])
    release.require(len(probes) == 3 and {p.get('type') for p in probes} == {'Startup', 'Readiness', 'Liveness'}
                    and all(p.get('httpGet', {}).get('path') == '/health' and p['httpGet'].get('port') == 8000
                            and p['httpGet'].get('scheme') == 'HTTP' and not p['httpGet'].get('httpHeaders')
                            and not p.get('tcpSocket') and not p.get('exec')
                            and p.get('initialDelaySeconds') == 5 and p.get('periodSeconds') == 10
                            and p.get('timeoutSeconds') == 3
                            and p.get('failureThreshold') == (30 if p['type'] == 'Startup' else 3) for p in probes), 'runtime probes differ')


def app_state(cfg, foundation, digest, commit):
    release.require(foundation.get('existing'), 'existing app is required')
    app = cfg.az('containerapp', 'show', '--name', cfg.app, '--resource-group', cfg.group)
    release.owned(app)
    props = app['properties']
    refs = [props[key] for key in ('environmentId', 'managedEnvironmentId') if key in props]
    release.require(app.get('name') == cfg.app and props.get('provisioningState') == 'Succeeded'
                    and app['tags'].get('commit') == commit and refs
                    and all(isinstance(ref, str) and ref.lower() == foundation['environment_id'].lower() for ref in refs),
                    'app provenance/configuration differs')
    identity = app.get('identity', {})
    release.require(identity.get('type') == 'UserAssigned'
                    and set(identity.get('userAssignedIdentities', {})) == {foundation['pull_id']}, 'app identity differs')
    configuration = props['configuration']
    ingress = configuration['ingress']
    registries = configuration.get('registries', [])
    release.require('secrets' in configuration and configuration['secrets'] in (None, []) and configuration.get('activeRevisionsMode') == 'Single'
                    and len(registries) == 1 and registries[0].get('server') == foundation['login_server']
                    and registries[0].get('identity') == foundation['pull_id']
                    and not registries[0].get('username') and not registries[0].get('passwordSecretRef')
                    and ingress.get('external') is True and ingress.get('allowInsecure') is False and ingress.get('targetPort') == 8000
                    and isinstance(ingress.get('transport'), str) and ingress['transport'].lower() == 'auto'
                    and ingress.get('exposedPort') in (None, 0)
                    and all(ingress.get(key) in (None, []) for key in ('additionalPortMappings', 'customDomains', 'ipSecurityRestrictions'))
                    and ingress.get('corsPolicy') in (None, [], {}),
                    'app ingress, registry or secrets differ')
    release.require(props.get('workloadProfileName') == 'Consumption', 'app workload profile differs')
    runtime_template(props['template'], foundation, digest)
    hostname = ingress.get('fqdn', '')
    release.require(isinstance(hostname, str) and re.fullmatch(r'[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?', hostname)
                    and hostname.startswith(cfg.app + '.') and hostname.endswith('.azurecontainerapps.io')
                    and '..' not in hostname, 'invalid approved app hostname')
    revision_name = props.get('latestReadyRevisionName')
    release.require(isinstance(revision_name, str) and re.fullmatch(re.escape(cfg.app) + r'--[a-z0-9][a-z0-9-]*', revision_name)
                    and props.get('latestRevisionName') == revision_name, 'latest revision is not ready')
    traffic = ingress.get('traffic')
    release.require(isinstance(traffic, list) and len(traffic) == 1 and traffic[0].get('weight') == 100
                    and (traffic[0].get('latestRevision') is True or traffic[0].get('revisionName') == revision_name)
                    and traffic[0].get('revisionName') in (None, '', revision_name),
                    'app traffic differs from ready revision')
    revision = cfg.az('containerapp', 'revision', 'show', '--name', cfg.app, '--resource-group', cfg.group, '--revision', revision_name)
    state = revision['properties']
    release.require(revision.get('name') == revision_name and state.get('provisioningState') == 'Provisioned'
                    and state.get('healthState') == 'Healthy' and state.get('active') is True, 'ready revision is unhealthy or differs')
    runtime_template(state['template'], foundation, digest)
    return hostname, revision_name


def verify(args):
    evidence = {'schema_version': 1, 'kind': 'post-incident-verification', 'status': 'pending',
                'source_run_id': args.source_run_id if re.fullmatch(r'[1-9][0-9]*', args.source_run_id) else None,
                'source_run_conclusion': None, 'commit': args.commit if release.COMMIT_RE.fullmatch(args.commit) else None,
                'digest': args.digest if release.DIGEST_RE.fullmatch(args.digest) else None,
                'image_id': args.image_id if release.DIGEST_RE.fullmatch(args.image_id) else None,
                'revision': None, 'acceptance': None, 'configuration_fingerprint': None}
    write_evidence(args.output, evidence, initial=True)
    stage = 'input validation'
    try:
        release.require(all(evidence[k] for k in ('source_run_id', 'commit', 'digest', 'image_id')), 'invalid verification input')
        cfg = configuration()
        scope = {**asdict(cfg), 'location': os.environ.get('AZURE_LOCATION', 'eastus')}
        evidence['configuration_fingerprint'] = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
        stage = 'source run validation'
        source_run(cfg, args.source_run_id, args.commit, evidence)
        stage = 'foundation verification'
        foundation = release.preflight(cfg)
        stage = 'published image verification'
        release.verify_published(cfg, args.digest, args.commit, args.image_id)
        stage = 'app and revision verification'
        hostname, evidence['revision'] = app_state(cfg, foundation, args.digest, args.commit)
        stage = 'HTTP acceptance'
        evidence['acceptance'] = release.retry_acceptance('https://' + hostname)
        evidence['status'] = 'verified'
    except (KeyboardInterrupt, SystemExit):
        evidence.update(status='interrupted', error='verification interrupted during ' + stage)
        raise
    except Exception:
        evidence.update(status='failed', error='verification failed during ' + stage)
        raise
    finally:
        write_evidence(args.output, evidence)
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('source-run-id', 'commit', 'digest', 'image-id'):
        parser.add_argument('--' + flag, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        verify(args)
    except (KeyboardInterrupt, SystemExit):
        print('Post-incident verification interrupted; no release authorized.', file=sys.stderr)
        return 130
    except Exception:
        print('Post-incident verification failed; no release authorized.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
