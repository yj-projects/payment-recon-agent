#!/usr/bin/env python3
"""Explicit, fail-closed Azure demo lifecycle commands (standard library only).

All commands preview mutations by default. Read-only az/gh authentication is
required even for previews. Never invoked by CI or deployment tooling.
"""
import argparse
import datetime
import json
import os
import re
import shlex
import subprocess
import sys


class SafetyError(RuntimeError):
    """A safety check or external command failed; stop without guessing."""


TAGS = {'project': 'payment-recon-agent', 'environment': 'demo', 'cleanup': 'resource-group'}
ROLES = {
    'AcrPull': '7f951dda-4ed3-4680-a7ca-43fe172d538d',
    'AcrPush': '8311e382-0749-4cb8-b61a-304f252e45ec',
    'Container Apps Contributor': '358470bc-b998-42bd-ab17-a7e34c199c0f',
    'Managed Identity Operator': 'f1a07417-d97a-45cb-824c-7a7467783830',
    'Reader': 'acdd72a7-3385-48ef-bd42-f606fba81ae7',
}
COST_CONFIRMATION = 'I understand Azure resources incur charges until deleted'
SECURITY_CONFIRMATION = 'I approve the two identities and five scoped roles'
DIAGNOSTIC_NAME = 'recon-environment-logs'
LOG_CATEGORIES = {'ContainerAppConsoleLogs', 'ContainerAppSystemLogs'}


class Config:
    @classmethod
    def from_env(cls, env):
        obj = cls()
        for key in ('AZURE_SUBSCRIPTION_ID', 'AZURE_ACR_NAME'):
            if not env.get(key, '').strip():
                raise SafetyError(f'{key} is required')
        obj.subscription = env['AZURE_SUBSCRIPTION_ID'].strip()
        if not re.fullmatch(r'[0-9a-fA-F-]{36}', obj.subscription):
            raise SafetyError('AZURE_SUBSCRIPTION_ID must be a subscription UUID')
        obj.tenant = env.get('AZURE_TENANT_ID', '').strip()
        obj.acr = env['AZURE_ACR_NAME'].strip()
        if not re.fullmatch(r'[a-zA-Z0-9]{5,50}', obj.acr):
            raise SafetyError('AZURE_ACR_NAME must be 5-50 alphanumeric characters')
        obj.group = env.get('AZURE_RESOURCE_GROUP', 'rg-payment-recon-demo')
        obj.location = env.get('AZURE_LOCATION', 'eastus')
        obj.environment = env.get('AZURE_CONTAINERAPPS_ENVIRONMENT', 'cae-payment-recon-demo')
        obj.workspace = env.get('AZURE_LOG_ANALYTICS_WORKSPACE', 'law-payment-recon-demo')
        obj.repository = env.get('GITHUB_REPOSITORY', '')
        obj.pull = env.get('AZURE_PULL_IDENTITY_NAME', 'id-recon-pull')
        obj.cd = env.get('AZURE_CD_IDENTITY_NAME', 'id-recon-cd')
        if (obj.pull, obj.cd) != ('id-recon-pull', 'id-recon-cd'):
            raise SafetyError('Only the approved id-recon-pull and id-recon-cd identities are allowed')
        for value in (obj.group, obj.location, obj.environment, obj.workspace):
            if not re.fullmatch(r'[a-zA-Z0-9_.()-]+', value):
                raise SafetyError('Invalid Azure resource name/location')
        return obj

    @property
    def group_id(self):
        return f'/subscriptions/{self.subscription}/resourceGroups/{self.group}'

    def resource_id(self, provider, name):
        return f'{self.group_id}/providers/{provider}/{name}'

    def az(self, *parts):
        return ['az', *parts, '--subscription', self.subscription]

    def require_repository(self):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', self.repository):
            raise SafetyError('GITHUB_REPOSITORY must be explicitly set to owner/repository')


class CLI:
    def __init__(self, execute=False):
        self.execute = execute

    def read(self, command, missing_ok=False):
        args = list(command)
        if args[0] == 'az':
            args += ['--output', 'json', '--only-show-errors']
        try:
            result = subprocess.run(args, text=True, capture_output=True, check=False)
        except OSError as exc:
            raise SafetyError(str(exc)) from exc
        if result.returncode:
            # Auth, network and subscription failures must never look like absence.
            if missing_ok and re.search(r'\((ResourceNotFound|ResourceGroupNotFound|NotFound)\)', result.stderr):
                return None
            raise SafetyError(f'{shlex.join(command)} failed: {result.stderr.strip()}')
        try:
            return json.loads(result.stdout) if result.stdout.strip() else None
        except json.JSONDecodeError as exc:
            raise SafetyError(f'Invalid JSON from {shlex.join(command)}') from exc

    def write(self, command):
        print(('EXECUTE ' if self.execute else 'PREVIEW ') + shlex.join(command))
        if self.execute:
            return self.read(command)
        return None


def owned(value):
    tags = value.get('tags') or {}
    if any(tags.get(key) != expected for key, expected in TAGS.items()):
        raise SafetyError(f'Resource ownership tags do not match: {value.get("id", "resource")}')


def normalized(value):
    return str(value).replace(' ', '').lower()


def validate_foundation(kind, value, config):
    owned(value)
    if normalized(value.get('location')) != normalized(config.location):
        raise SafetyError(f'Incompatible {kind} location')
    p = value.get('properties', value)
    identity = value.get('identity') or {}
    if identity.get('type', 'None') != 'None':
        raise SafetyError(f'Incompatible {kind} identity')
    if p.get('provisioningState', 'Succeeded') != 'Succeeded':
        raise SafetyError(f'{kind} provisioning is not Succeeded')
    if kind == 'acr':
        valid = (value.get('sku', {}).get('name') == 'Basic'
                 and p.get('adminUserEnabled') is False
                 and p.get('roleAssignmentMode') in ('LegacyRegistryPermissions', 'rbac'))
    elif kind == 'law':
        valid = (p.get('sku', {}).get('name') == 'PerGB2018'
                 and p.get('retentionInDays') == 30
                 and p.get('workspaceCapping', {}).get('dailyQuotaGb') == .023)
    else:
        profiles = p.get('workloadProfiles', [])
        vnet = p.get('vnetConfiguration') or {}
        valid = (len(profiles) == 1 and profiles[0].get('name') == 'Consumption'
                 and profiles[0].get('workloadProfileType') == 'Consumption'
                 and p.get('appLogsConfiguration', {}).get('destination') == 'azure-monitor'
                 and not vnet.get('infrastructureSubnetId') and not vnet.get('internal')
                 and not p.get('zoneRedundant'))
    if not valid:
        raise SafetyError(f'Incompatible existing {kind}; refusing to modify it')


def foundation_commands(c):
    return {
        'acr': c.az('acr', 'show', '--resource-group', c.group, '--name', c.acr),
        'law': c.az('monitor', 'log-analytics', 'workspace', 'show', '--resource-group', c.group, '--workspace-name', c.workspace),
        'env': c.az('containerapp', 'env', 'show', '--resource-group', c.group, '--name', c.environment),
    }


def read_foundation(c, cli, require=False):
    group = cli.read(c.az('group', 'show', '--name', c.group), missing_ok=True)
    if group is not None:
        owned(group)
        if normalized(group.get('location')) != normalized(c.location):
            raise SafetyError('Incompatible resource group location')
    values = {}
    for kind, command in foundation_commands(c).items():
        value = cli.read(command, missing_ok=True) if group is not None else None
        if value is not None:
            validate_foundation(kind, value, c)
        values[kind] = value
    if require and (group is None or any(v is None for v in values.values())):
        raise SafetyError('Foundation is missing; run setup explicitly first')
    return group, values


def diagnostic_command(c):
    return c.az('monitor', 'diagnostic-settings', 'show', '--name', DIAGNOSTIC_NAME,
                '--resource', c.resource_id('Microsoft.App/managedEnvironments', c.environment))


def validate_diagnostic(value, c, environment=None):
    p = value.get('properties', value)
    expected_workspace = c.resource_id('Microsoft.OperationalInsights/workspaces', c.workspace)
    logs = p.get('logs', [])
    # This environment API can omit/null the table-mode field on GET even
    # after accepting Dedicated on create. Its verified azure-monitor routing
    # and these exact categories still target the resource-specific tables.
    env_properties = (environment or {}).get('properties', environment or {})
    azure_monitor = env_properties.get('appLogsConfiguration', {}).get('destination') == 'azure-monitor'
    table_mode = p.get('logAnalyticsDestinationType')
    valid_mode = table_mode == 'Dedicated' or (table_mode is None and azure_monitor)
    if (normalized(p.get('workspaceId')) != normalized(expected_workspace)
            or not valid_mode
            or {x.get('category') for x in logs if x.get('enabled')} != LOG_CATEGORIES
            or any(x.get('enabled') for x in p.get('metrics', []))
            or any(p.get(key) for key in ('storageAccountId', 'eventHubAuthorizationRuleId',
                                         'marketplacePartnerId', 'serviceBusRuleId'))):
        raise SafetyError('Incompatible existing diagnostic setting')


def check_providers(c, cli):
    for namespace in ('Microsoft.App', 'Microsoft.ContainerRegistry',
                      'Microsoft.OperationalInsights', 'Microsoft.Insights'):
        value = cli.read(c.az('provider', 'show', '--namespace', namespace))
        if not isinstance(value, dict) or value.get('registrationState') != 'Registered':
            raise SafetyError(f'Required provider {namespace} is not Registered; register it explicitly outside this script before setup')


def setup(c, cli):
    group, existing = read_foundation(c, cli)
    diag = cli.read(diagnostic_command(c), missing_ok=True) if existing['env'] else None
    if diag is not None:
        validate_diagnostic(diag, c, environment=existing['env'])
    check_providers(c, cli)
    tags = [f'{k}={v}' for k, v in (TAGS | {'milestone': '3a'}).items()]
    common = ['--resource-group', c.group, '--location', c.location, '--tags', *tags]
    # Complete read-only compatibility checks above before the first write.
    if group is None:
        cli.write(c.az('group', 'create', '--name', c.group, '--location', c.location, '--tags', *tags))
    if existing['acr'] is None:
        cli.write(c.az('acr', 'create', '--name', c.acr, '--sku', 'Basic', '--admin-enabled', 'false', '--role-assignment-mode', 'rbac', *common))
    if existing['law'] is None:
        cli.write(c.az('monitor', 'log-analytics', 'workspace', 'create', '--workspace-name', c.workspace,
                       '--sku', 'PerGB2018', '--retention-time', '30', '--quota', '0.023', '--identity-type', 'None', *common))
    if existing['env'] is None:
        cli.write(c.az('containerapp', 'env', 'create', '--name', c.environment,
                       '--enable-workload-profiles', 'true', '--logs-destination', 'azure-monitor', *common))
    if diag is None:
        cli.write(c.az('monitor', 'diagnostic-settings', 'create', '--name', DIAGNOSTIC_NAME,
                       '--resource', c.resource_id('Microsoft.App/managedEnvironments', c.environment),
                       '--workspace', c.resource_id('Microsoft.OperationalInsights/workspaces', c.workspace),
                       '--export-to-resource-specific', 'true', '--logs',
                       json.dumps([{'category': x, 'enabled': True} for x in sorted(LOG_CATEGORIES)])))
    if getattr(cli, 'execute', False):
        _, verified = read_foundation(c, cli, require=True)
        validate_diagnostic(cli.read(diagnostic_command(c)), c, environment=verified['env'])


def oidc_subject(settings, repository):
    """Resolve the actual repository policy; never change GitHub OIDC settings.

    GitHub's default immutable format embeds numeric IDs in sub_claim_prefix.
    Unknown custom templates fail closed. A legacy default is accepted only
    when the API explicitly disables immutable subjects on a pre-cutover repo.
    """
    values = {'repository_id': repository.get('id'),
              'repository_owner_id': repository.get('owner', {}).get('id'),
              'ref': 'refs/heads/main'}
    if any(not str(values[k]).isdigit() for k in ('repository_id', 'repository_owner_id')):
        raise SafetyError('GitHub repository and owner numeric IDs are required')
    if settings.get('use_default') is True:
        owner = repository.get('owner', {}).get('login', '')
        name = repository.get('name', '')
        full_name = f'{owner}/{name}'
        if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', full_name)
                or repository.get('full_name') != full_name):
            raise SafetyError('GitHub repository names are missing or inconsistent')
        if settings.get('use_immutable_subject') is True:
            expected = f"repo:{owner}@{values['repository_owner_id']}/{name}@{values['repository_id']}"
        elif settings.get('use_immutable_subject') is False:
            try:
                created = datetime.datetime.fromisoformat(repository['created_at'].replace('Z', '+00:00'))
                cutoff = datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc)
                if created >= cutoff:
                    raise ValueError('post-cutover repository')
            except (KeyError, ValueError, TypeError, AttributeError) as exc:
                raise SafetyError('Legacy OIDC requires an explicitly pre-2026-07-15 repository') from exc
            expected = f'repo:{full_name}'
        else:
            raise SafetyError('GitHub OIDC immutable-subject setting is unknown')
        if settings.get('sub_claim_prefix') != expected:
            raise SafetyError('GitHub OIDC subject prefix does not match repository names and numeric IDs')
        return expected + ':ref:refs/heads/main'
    keys = settings.get('include_claim_keys', [])
    if (settings.get('use_default') is not False
            or set(keys) != {'repository_id', 'repository_owner_id', 'ref'} or len(keys) != 3):
        raise SafetyError('Unknown custom GitHub OIDC subject template; refusing to guess')
    return ':'.join(f'{key}:{values[key]}' for key in keys)


def validate_roles(assignments, principal, approved):
    allowed = {(ROLES[role], scope.lower()) for role, scope in approved}
    seen = set()
    for assignment in assignments:
        if str(assignment.get('principalId', '')).lower() != principal.lower():
            raise SafetyError('Unexpected principal in role assignment response')
        role_id = str(assignment.get('roleDefinitionId', '')).lower().rsplit('/', 1)[-1]
        pair = (role_id, str(assignment.get('scope', '')).lower())
        if pair in seen:
            raise SafetyError('Duplicate existing role assignment')
        seen.add(pair)
        if pair not in allowed:
            raise SafetyError('Unexpected existing role or scope; refusing to broaden or silently preserve access')


def ensure_role(cli, principal, role, scope, c):
    assignments = cli.read(c.az('role', 'assignment', 'list', '--assignee-object-id', principal, '--scope', scope,
                                  '--fill-principal-name', 'false', '--fill-role-definition-name', 'false'))
    role_id = ROLES[role]
    if any(str(x.get('principalId', '')).lower() == principal.lower()
           and str(x.get('scope', '')).lower() == scope.lower()
           and str(x.get('roleDefinitionId', '')).lower().endswith('/' + role_id)
           for x in assignments):
        print(f'Existing exact assignment: {role} at {scope}')
        return
    cli.write(c.az('role', 'assignment', 'create', '--assignee-object-id', principal,
                   '--assignee-principal-type', 'ServicePrincipal', '--role', role_id, '--scope', scope))


def security(c, cli):
    c.require_repository()
    if not c.tenant:
        raise SafetyError('AZURE_TENANT_ID is required for security')
    _, verified = read_foundation(c, cli, require=True)
    validate_diagnostic(cli.read(diagnostic_command(c)), c, environment=verified['env'])
    repository = cli.read(['gh', 'api', f'repos/{c.repository}'])
    settings = cli.read(['gh', 'api', f'repos/{c.repository}/actions/oidc/customization/sub'])
    subject = oidc_subject(settings, repository)
    print(f'GitHub main-only subject verified from repository OIDC policy: {subject}')
    identities = {}
    for name in (c.pull, c.cd):
        value = cli.read(c.az('identity', 'show', '--resource-group', c.group, '--name', name), missing_ok=True)
        if value is not None:
            owned(value)
            if (normalized(value.get('location')) != normalized(c.location)
                    or value.get('tenantId', '').lower() != c.tenant.lower()):
                raise SafetyError(f'Incompatible existing identity {name}')
        identities[name] = value
    registry = c.resource_id('Microsoft.ContainerRegistry/registries', c.acr)
    pull_id = c.resource_id('Microsoft.ManagedIdentity/userAssignedIdentities', c.pull)
    approved = {c.pull: [('AcrPull', registry)], c.cd: [
        ('AcrPush', registry), ('Container Apps Contributor', c.group_id),
        ('Managed Identity Operator', pull_id), ('Reader', c.group_id)]}
    # Refuse drift before creating either identity.
    credentials = []
    for name, identity in identities.items():
        if identity is not None:
            principal = identity.get('principalId')
            if not principal:
                raise SafetyError(f'Identity {name} has no principalId')
            listing = ['role', 'assignment', 'list', '--assignee-object-id', principal,
                       '--fill-principal-name', 'false', '--fill-role-definition-name', 'false']
            validate_roles(cli.read(c.az(*listing, '--all')), principal, approved[name])
            # --all cannot be combined with --scope in az. Check ancestors
            # separately so subscription/management-group inheritance is visible.
            validate_roles(cli.read(c.az(*listing, '--scope', c.group_id, '--include-inherited')),
                           principal, approved[name])
            current = cli.read(c.az('identity', 'federated-credential', 'list', '--identity-name', name, '--resource-group', c.group))
            if name == c.pull and current:
                raise SafetyError('Pull identity must have no federated credentials')
            if name == c.cd:
                credentials = current
                for credential in current:
                    if not valid_credential(credential, subject):
                        raise SafetyError('Unexpected existing CD federated credential; refusing to broaden trust')
                if len(current) > 1:
                    raise SafetyError('Expected at most one CD federated credential')
    for name, value in identities.items():
        if value is None:
            identities[name] = cli.write(c.az('identity', 'create', '--resource-group', c.group,
                                             '--name', name, '--location', c.location, '--tags',
                                             *[f'{k}={v}' for k, v in (TAGS | {'milestone': '3b'}).items()]))
    if not credentials:
        cli.write(c.az('identity', 'federated-credential', 'create', '--name', 'github-main', '--identity-name', c.cd,
                       '--resource-group', c.group, '--issuer', 'https://token.actions.githubusercontent.com',
                       '--subject', subject, '--audiences', 'api://AzureADTokenExchange'))
    for name, role, scope in [(c.pull, 'AcrPull', registry), (c.cd, 'AcrPush', registry),
                              (c.cd, 'Container Apps Contributor', c.group_id), (c.cd, 'Managed Identity Operator', pull_id),
                              (c.cd, 'Reader', c.group_id)]:
        identity = identities[name]
        if identity is None:  # A preview cannot know the new principal's ID.
            print(f'PREVIEW assign {role} to newly created {name} at {scope}')
        else:
            if not identity.get('principalId'):
                raise SafetyError(f'Identity {name} has no principalId')
            ensure_role(cli, identity['principalId'], role, scope, c)
    if getattr(cli, 'execute', False):
        for name in (c.pull, c.cd):
            identity = cli.read(c.az('identity', 'show', '--resource-group', c.group, '--name', name))
            print(json.dumps({'name': name, 'clientId': identity['clientId'], 'id': identity['id']}))


def valid_credential(value, subject):
    p = value.get('properties', value)
    return (p.get('issuer') == 'https://token.actions.githubusercontent.com'
            and p.get('subject') == subject and p.get('audiences') == ['api://AzureADTokenExchange'])


def teardown(c, cli, execute=False):
    if os.environ.get('CI') or os.environ.get('GITHUB_ACTIONS'):
        raise SafetyError('Teardown is local-only and refuses CI/GITHUB_ACTIONS')
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SafetyError('Teardown requires a local interactive TTY')
    c.require_repository()
    group = cli.read(c.az('group', 'show', '--name', c.group), missing_ok=True)
    if group is None:
        print('Dedicated resource group already absent; use verify-clean for remaining tagged resources.')
        return
    owned(group)
    resources = cli.read(c.az('resource', 'list', '--resource-group', c.group))
    print(f'Subscription: {c.subscription}\nResource group: {c.group}\nDeletion inventory:')
    for resource in resources:
        print(resource.get('id', '<missing resource ID>'))
        owned(resource)
        if not resource.get('id', '').lower().startswith(c.group_id.lower() + '/providers/'):
            raise SafetyError('Inventory contains a resource outside the dedicated group')
    if not execute:
        print('Preview only. Re-run teardown --execute locally for typed confirmation.')
        return
    expected = f'DELETE {c.subscription}/{c.group}'
    answer = input(f'Type exactly {expected}: ')
    if answer != expected:
        raise SafetyError('Deletion confirmation did not match; no changes made')
    # A failed GitHub switch write prevents Azure deletion.
    cli.write(['gh', 'variable', 'set', 'AZURE_DEPLOYMENT_ENABLED', '--repo', c.repository, '--body', 'false'])
    cli.write(c.az('group', 'delete', '--name', c.group, '--yes'))
    print('Deletion requested. Run verify-clean; billing and soft-deleted data may lag.')


def verify_clean(c, cli):
    group = cli.read(c.az('group', 'show', '--name', c.group), missing_ok=True)
    resources = cli.read(c.az('resource', 'list', '--tag', 'project=payment-recon-agent'))
    members = cli.read(c.az('resource', 'list', '--resource-group', c.group)) if group else []
    print(json.dumps({'dedicated_group_exists': group is not None, 'group_resources': members,
                      'project_tagged_resources': resources}, indent=2))
    print('ARM inventory cannot prove zero charges: soft-deleted workspaces, retained data, usage/billing reporting lag, and resources without project tags require separate review.')
    return group is None and not resources and not members


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('setup', 'security', 'teardown'):
        sub = commands.add_parser(name)
        sub.add_argument('--execute', action='store_true', help='Perform the previewed writes')
        if name == 'setup':
            sub.add_argument('--confirm-cost', help=COST_CONFIRMATION)
        if name == 'security':
            sub.add_argument('--confirm-security', help=SECURITY_CONFIRMATION)
    commands.add_parser('verify-clean')
    args = parser.parse_args(argv)
    try:
        c = Config.from_env(os.environ)
        if args.command == 'setup' and args.execute and args.confirm_cost != COST_CONFIRMATION:
            raise SafetyError(f'--execute requires --confirm-cost {COST_CONFIRMATION!r}; ACR and logs incur ongoing charges; quota is not a spending cap')
        if args.command == 'security' and args.execute and args.confirm_security != SECURITY_CONFIRMATION:
            raise SafetyError(f'--execute requires --confirm-security {SECURITY_CONFIRMATION!r}')
        cli = CLI(execute=getattr(args, 'execute', False))
        if args.command in ('setup', 'security'):
            if args.command == 'setup':
                print('Cost: Basic ACR persists until deleted; logs/storage and retained data can incur charges. The ingestion quota is not a spending cap.')
            globals()[args.command](c, cli)
        elif args.command == 'teardown':
            teardown(c, cli, execute=args.execute)
        else:
            return 0 if verify_clean(c, cli) else 1
        return 0
    except (SafetyError, KeyboardInterrupt, EOFError) as exc:
        print(f'STOP: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
