"""Safety contract tests; Azure/GitHub calls are always substituted."""
import importlib.util
import io
from pathlib import Path
from unittest.mock import patch

import pytest

SPEC = importlib.util.spec_from_file_location('azure_infra', Path(__file__).parents[1] / 'scripts/azure_infra.py')
infra = importlib.util.module_from_spec(SPEC)
# Missing implementation is the initial red stage.
if SPEC.loader and Path(SPEC.origin).exists():
    SPEC.loader.exec_module(infra)

ENV = {'AZURE_SUBSCRIPTION_ID': '11111111-1111-1111-1111-111111111111',
       'AZURE_TENANT_ID': '22222222-2222-2222-2222-222222222222',
       'AZURE_ACR_NAME': 'acrdemotest', 'GITHUB_REPOSITORY': 'owner/recon'}
TAGS = {'project': 'payment-recon-agent', 'environment': 'demo', 'cleanup': 'resource-group', 'milestone': '3a'}


def config(**changes):
    return infra.Config.from_env(ENV | changes)


def test_required_subscription_and_registry_fail_before_cli():
    for missing in ['AZURE_SUBSCRIPTION_ID', 'AZURE_ACR_NAME']:
        with pytest.raises(infra.SafetyError, match=missing):
            infra.Config.from_env({k: v for k, v in ENV.items() if k != missing})


def test_immutable_oidc_subject_uses_numeric_ids_in_configured_order():
    result = infra.oidc_subject({'use_default': False, 'include_claim_keys': ['repository_id', 'repository_owner_id', 'ref']},
                               {'id': 42, 'owner': {'id': 99}})
    assert result == 'repository_id:42:repository_owner_id:99:ref:refs/heads/main'


@pytest.mark.parametrize('settings', [
    {'use_default': True},
    {'use_default': False, 'include_claim_keys': ['repo', 'ref']},
    {'use_default': False, 'include_claim_keys': ['repository_id', 'repository_owner_id', 'environment']},
])
def test_mutable_or_context_dependent_oidc_subject_is_rejected(settings):
    with pytest.raises(infra.SafetyError):
        infra.oidc_subject(settings, {'id': 42, 'owner': {'id': 99}})


def test_dry_run_never_starts_mutation():
    cli = infra.CLI(execute=False)
    with patch.object(infra.subprocess, 'run', side_effect=AssertionError('unexpected external command')):
        assert cli.write(['az', 'group', 'create', '--name', 'example']) is None


def test_cli_only_treats_explicit_not_found_as_missing():
    failure = infra.subprocess.CompletedProcess([], 1, '', 'AuthorizationFailed')
    with patch.object(infra.subprocess, 'run', return_value=failure):
        with pytest.raises(infra.SafetyError, match='AuthorizationFailed'):
            infra.CLI().read(['az', 'group', 'show'], missing_ok=True)
    absent = infra.subprocess.CompletedProcess([], 1, '', '(ResourceGroupNotFound) missing')
    with patch.object(infra.subprocess, 'run', return_value=absent):
        assert infra.CLI().read(['az', 'group', 'show'], missing_ok=True) is None


def test_setup_rejects_unowned_existing_group_before_mutation():
    class Fake:
        def read(self, command, **kwargs):
            return {'tags': {'project': 'someone-else'}, 'location': 'eastus'}
        def write(self, command):
            pytest.fail('must validate ownership before mutation')
    with pytest.raises(infra.SafetyError, match='ownership'):
        infra.setup(config(), Fake())


@pytest.mark.parametrize('kind,patch_value', [
    ('acr', {'sku': {'name': 'Premium'}}),
    ('acr', {'adminUserEnabled': True}),
    ('law', {'retentionInDays': 90}),
    ('law', {'workspaceCapping': {'dailyQuotaGb': 1}}),
    ('env', {'workloadProfiles': [{'name': 'Dedicated', 'workloadProfileType': 'D4'}]}),
    ('env', {'appLogsConfiguration': {'destination': 'log-analytics'}}),
])
def test_existing_incompatible_foundation_is_refused(kind, patch_value):
    value = foundation(kind) | patch_value
    with pytest.raises(infra.SafetyError):
        infra.validate_foundation(kind, value, config())


def foundation(kind):
    return {'tags': TAGS, 'location': 'East US', 'identity': {'type': 'None'}, **{
        'acr': {'sku': {'name': 'Basic'}, 'adminUserEnabled': False, 'roleAssignmentMode': 'LegacyRegistryPermissions'},
        'law': {'sku': {'name': 'PerGB2018'}, 'retentionInDays': 30, 'workspaceCapping': {'dailyQuotaGb': .023}},
        'env': {'workloadProfiles': [{'name': 'Consumption', 'workloadProfileType': 'Consumption'}],
                'appLogsConfiguration': {'destination': 'azure-monitor'}, 'vnetConfiguration': None},
    }[kind]}


@pytest.mark.parametrize('ci_env', [{'CI': 'true'}, {'GITHUB_ACTIONS': 'true'}, {}])
def test_teardown_requires_local_tty_before_any_external_call(ci_env):
    class Fake:
        def read(self, *args, **kwargs):
            pytest.fail('refuse before inspecting account')
    with patch.dict(infra.os.environ, ci_env, clear=True), patch.object(infra.sys, 'stdin', io.StringIO()):
        with pytest.raises(infra.SafetyError):
            infra.teardown(config(), Fake(), execute=True)


def test_role_assignment_is_idempotent_and_scope_exact():
    class Fake:
        writes = []
        def read(self, command, **kwargs):
            return [{'principalId': 'p', 'roleDefinitionId': '/providers/Microsoft.Authorization/roleDefinitions/7f951dda-4ed3-4680-a7ca-43fe172d538d', 'scope': '/registry'}]
        def write(self, command):
            self.writes.append(command)
    cli = Fake()
    infra.ensure_role(cli, 'p', 'AcrPull', '/registry', config())
    assert cli.writes == []


def test_wrong_teardown_confirmation_never_disables_or_deletes():
    class TTY(io.StringIO):
        def isatty(self): return True
    class Fake:
        def read(self, command, **kwargs):
            if 'group' in command: return {'tags': TAGS}
            return [{'id': '/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-payment-recon-demo/providers/Microsoft.App/containerApps/demo', 'tags': TAGS}]
        def write(self, command): pytest.fail('wrong confirmation must cause zero writes')
    with patch.dict(infra.os.environ, {}, clear=True), patch.object(infra.sys, 'stdin', TTY()), patch.object(infra.sys, 'stdout', TTY()), patch('builtins.input', return_value='DELETE wrong'):
        with pytest.raises(infra.SafetyError, match='confirmation'):
            infra.teardown(config(), Fake(), execute=True)


def test_existing_role_with_broader_scope_is_refused():
    assignments = [{'principalId': 'p', 'scope': '/subscriptions/sub', 'roleDefinitionId': '/roles/8311e382-0749-4cb8-b61a-304f252e45ec'}]
    with pytest.raises(infra.SafetyError, match='Unexpected'):
        infra.validate_roles(assignments, 'p', [('AcrPush', '/registry')])


def test_existing_unapproved_role_is_refused():
    assignments = [{'principalId': 'p', 'scope': '/registry', 'roleDefinitionId': '/roles/owner-role'}]
    with pytest.raises(infra.SafetyError, match='Unexpected'):
        infra.validate_roles(assignments, 'p', [('AcrPush', '/registry')])


def test_existing_exact_approved_roles_are_accepted():
    assignments = [{'principalId': 'p', 'scope': '/registry', 'roleDefinitionId': '/roles/8311e382-0749-4cb8-b61a-304f252e45ec'}]
    infra.validate_roles(assignments, 'p', [('AcrPush', '/registry')])


def test_setup_missing_foundation_previews_only_approved_resources(capsys):
    cli = infra.CLI()
    with patch.object(cli, 'read', side_effect=lambda command, **kwargs: {'registrationState': 'Registered'} if 'provider' in command else None):
        infra.setup(config(), cli)
    out = capsys.readouterr().out
    assert out.count('PREVIEW ') == 5
    assert '--sku Basic' in out and '--admin-enabled false' in out
    assert '--quota 0.023' in out and '--retention-time 30' in out
    assert '--enable-workload-profiles true' in out
    assert '--export-to-resource-specific true' in out
    assert 'identity create' not in out and 'role assignment create' not in out


def test_setup_existing_foundation_is_read_only():
    class Fake:
        def read(self, command, **kwargs):
            if command[1:3] == ['provider', 'show']: return {'registrationState': 'Registered'}
            if command[1:3] == ['group', 'show']:
                return {'tags': TAGS, 'location': 'eastus'}
            if command[1:3] == ['acr', 'show']: return foundation('acr')
            if 'log-analytics' in command: return foundation('law')
            if 'containerapp' in command: return foundation('env')
            return diagnostic()
        def write(self, command): pytest.fail('compatible infrastructure should not be mutated')
    infra.setup(config(), Fake())


def diagnostic():
    return {'workspaceId': '/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-payment-recon-demo/providers/Microsoft.OperationalInsights/workspaces/law-payment-recon-demo',
            'logAnalyticsDestinationType': 'Dedicated',
            'logs': [{'category': 'ContainerAppConsoleLogs', 'enabled': True}, {'category': 'ContainerAppSystemLogs', 'enabled': True}], 'metrics': []}


@pytest.mark.parametrize('change', [
    {'logAnalyticsDestinationType': 'AzureDiagnostics'},
    {'workspaceId': '/other/workspace'},
    {'logs': [{'category': 'ContainerAppConsoleLogs', 'enabled': True}]},
    {'metrics': [{'category': 'AllMetrics', 'enabled': True}]},
])
def test_diagnostic_drift_is_refused(change):
    with pytest.raises(infra.SafetyError):
        infra.validate_diagnostic(diagnostic() | change, config())


def test_execute_requires_explicit_cost_confirmation_before_external_calls(capsys):
    with patch.dict(infra.os.environ, ENV, clear=True), patch.object(infra.subprocess, 'run', side_effect=AssertionError('must reject before CLI')):
        assert infra.main(['setup', '--execute']) == 2
    assert '--confirm-cost' in capsys.readouterr().err


def test_execute_requires_explicit_security_confirmation_before_external_calls(capsys):
    with patch.dict(infra.os.environ, ENV, clear=True), patch.object(infra.subprocess, 'run', side_effect=AssertionError('must reject before CLI')):
        assert infra.main(['security', '--execute']) == 2
    assert '--confirm-security' in capsys.readouterr().err


def test_verified_cleanup_reports_lag_and_tagged_orphans(capsys):
    class Fake:
        def read(self, command, **kwargs):
            if 'group' in command: return None
            return [{'id': '/orphan'}]
    assert infra.verify_clean(config(), Fake()) is False
    assert 'billing reporting lag' in capsys.readouterr().out


def test_teardown_failed_github_switch_prevents_azure_deletion():
    class TTY(io.StringIO):
        def isatty(self): return True
    class Fake:
        writes = []
        def read(self, command, **kwargs):
            if 'group' in command: return {'tags': TAGS}
            return []
        def write(self, command):
            self.writes.append(command)
            raise infra.SafetyError('GitHub unavailable')
    cli = Fake()
    with patch.dict(infra.os.environ, {}, clear=True), patch.object(infra.sys, 'stdin', TTY()), patch.object(infra.sys, 'stdout', TTY()), patch('builtins.input', return_value='DELETE 11111111-1111-1111-1111-111111111111/rg-payment-recon-demo'):
        with pytest.raises(infra.SafetyError, match='GitHub unavailable'):
            infra.teardown(config(), cli, execute=True)
    assert len(cli.writes) == 1
    assert cli.writes[0][:4] == ['gh', 'variable', 'set', 'AZURE_DEPLOYMENT_ENABLED']


def test_teardown_disables_deployment_before_exact_group_deletion():
    class TTY(io.StringIO):
        def isatty(self): return True
    class Fake:
        writes = []
        def read(self, command, **kwargs):
            if 'group' in command: return {'tags': TAGS}
            return []
        def write(self, command): self.writes.append(command)
    cli = Fake()
    with patch.dict(infra.os.environ, {}, clear=True), patch.object(infra.sys, 'stdin', TTY()), patch.object(infra.sys, 'stdout', TTY()), patch('builtins.input', return_value='DELETE 11111111-1111-1111-1111-111111111111/rg-payment-recon-demo'):
        infra.teardown(config(), cli, execute=True)
    assert cli.writes[0] == ['gh', 'variable', 'set', 'AZURE_DEPLOYMENT_ENABLED', '--repo', 'owner/recon', '--body', 'false']
    assert cli.writes[1] == ['az', 'group', 'delete', '--name', 'rg-payment-recon-demo', '--yes', '--subscription', '11111111-1111-1111-1111-111111111111']


class SecurityCloud:
    """In-memory CLI boundary. No network or subprocess calls."""
    def __init__(self, existing=False, broad=False):
        self.writes = []
        self.existing = existing
        self.broad = broad

    def identity(self, name):
        return {'name': name, 'id': '/identity/' + name, 'tags': TAGS, 'location': 'eastus',
                'tenantId': ENV['AZURE_TENANT_ID'], 'principalId': name + '-principal', 'clientId': name + '-client'}

    def read(self, command, **kwargs):
        if command[1:3] == ['provider', 'show']: return {'registrationState': 'Registered'}
        if command[0] == 'gh':
            if command[-1].endswith('/sub'):
                return {'use_default': True, 'use_immutable_subject': True, 'sub_claim_prefix': 'repo:owner@99/recon@42'}
            return {'id': 42, 'name': 'recon', 'full_name': 'owner/recon', 'created_at': '2026-10-25T10:00:00Z', 'owner': {'id': 99, 'login': 'owner'}}
        if command[1:3] == ['group', 'show']: return {'tags': TAGS, 'location': 'eastus'}
        if command[1:3] == ['acr', 'show']: return foundation('acr')
        if 'log-analytics' in command: return foundation('law')
        if 'containerapp' in command: return foundation('env')
        if 'diagnostic-settings' in command: return diagnostic()
        if command[1:3] == ['identity', 'show']:
            return self.identity(command[command.index('--name') + 1]) if self.existing else None
        if 'federated-credential' in command: return []
        if command[1:4] == ['role', 'assignment', 'list']:
            if self.broad:
                return [{'principalId': command[command.index('--assignee-object-id') + 1], 'scope': '/subscriptions/all', 'roleDefinitionId': '/roles/owner'}]
            return []
        raise AssertionError(command)

    def write(self, command):
        self.writes.append(command)
        if command[1:3] == ['identity', 'create']:
            return self.identity(command[command.index('--name') + 1])
        return {}


def test_security_creates_only_two_identities_main_credential_and_five_scoped_roles():
    cloud = SecurityCloud()
    infra.security(config(), cloud)
    identities = [x for x in cloud.writes if x[1:3] == ['identity', 'create']]
    assert [x[x.index('--name') + 1] for x in identities] == ['id-recon-pull', 'id-recon-cd']
    credentials = [x for x in cloud.writes if 'federated-credential' in x]
    assert len(credentials) == 1
    credential = credentials[0]
    assert credential[credential.index('--subject') + 1] == 'repo:owner@99/recon@42:ref:refs/heads/main'
    assert credential[credential.index('--identity-name') + 1] == 'id-recon-cd'
    roles = [x for x in cloud.writes if x[1:4] == ['role', 'assignment', 'create']]
    assert len(roles) == 5
    prefix = '/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-payment-recon-demo'
    assert [(x[x.index('--role') + 1], x[x.index('--scope') + 1]) for x in roles] == [
        ('7f951dda-4ed3-4680-a7ca-43fe172d538d', prefix + '/providers/Microsoft.ContainerRegistry/registries/acrdemotest'),
        ('8311e382-0749-4cb8-b61a-304f252e45ec', prefix + '/providers/Microsoft.ContainerRegistry/registries/acrdemotest'),
        ('358470bc-b998-42bd-ab17-a7e34c199c0f', prefix),
        ('f1a07417-d97a-45cb-824c-7a7467783830', prefix + '/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-recon-pull'),
        ('acdd72a7-3385-48ef-bd42-f606fba81ae7', prefix),
    ]
    assert len(cloud.writes) == 8


def test_security_role_drift_blocks_all_mutations():
    cloud = SecurityCloud(existing=True, broad=True)
    with pytest.raises(infra.SafetyError, match='Unexpected'):
        infra.security(config(), cloud)
    assert cloud.writes == []


def test_security_never_bootstraps_missing_foundation():
    cloud = SecurityCloud()
    original = cloud.read
    cloud.read = lambda command, **kwargs: None if command[1:3] == ['group', 'show'] else original(command, **kwargs)
    with pytest.raises(infra.SafetyError, match='Foundation is missing'):
        infra.security(config(), cloud)
    assert cloud.writes == []


def test_github_immutable_default_subject_validates_api_prefix_against_numeric_ids():
    settings = {'use_default': True, 'use_immutable_subject': True, 'sub_claim_prefix': 'repo:owner@99/recon@42'}
    repository = {'id': 42, 'name': 'recon', 'full_name': 'owner/recon', 'created_at': '2026-10-25T10:00:00Z', 'owner': {'id': 99, 'login': 'owner'}}
    assert infra.oidc_subject(settings, repository) == 'repo:owner@99/recon@42:ref:refs/heads/main'


@pytest.mark.parametrize('prefix', ['repo:owner@98/recon@42', 'repo:owner@99/recon@43', 'repo:other@99/recon@42', 'repo:owner/recon'])
def test_github_immutable_prefix_drift_fails_closed(prefix):
    with pytest.raises(infra.SafetyError):
        infra.oidc_subject({'use_default': True, 'use_immutable_subject': True, 'sub_claim_prefix': prefix},
                           {'id': 42, 'name': 'recon', 'full_name': 'owner/recon', 'owner': {'id': 99, 'login': 'owner'}})


def test_legacy_default_requires_explicit_false_and_old_creation_date():
    settings = {'use_default': True, 'use_immutable_subject': False, 'sub_claim_prefix': 'repo:owner/recon'}
    repository = {'id': 42, 'name': 'recon', 'full_name': 'owner/recon', 'created_at': '2026-07-14T23:59:59Z', 'owner': {'id': 99, 'login': 'owner'}}
    assert infra.oidc_subject(settings, repository) == 'repo:owner/recon:ref:refs/heads/main'
    for changed in [{'created_at': '2026-07-15T00:00:00Z'}, {'created_at': '2026-10-25T00:00:00Z'}, {'created_at': None}]:
        with pytest.raises(infra.SafetyError):
            infra.oidc_subject(settings, repository | changed)


def test_security_detects_inherited_roles_before_any_mutation():
    class InheritedCloud(SecurityCloud):
        def read(self, command, **kwargs):
            if command[1:4] == ['role', 'assignment', 'list']:
                if '--include-inherited' in command:
                    return [{'principalId': command[command.index('--assignee-object-id') + 1], 'scope': '/providers/Microsoft.Management/managementGroups/shared', 'roleDefinitionId': '/roles/owner'}]
                return []
            return super().read(command, **kwargs)
    cloud = InheritedCloud(existing=True)
    with pytest.raises(infra.SafetyError, match='Unexpected'):
        infra.security(config(), cloud)
    assert cloud.writes == []


def test_duplicate_approved_roles_are_refused():
    assignment = {'principalId': 'p', 'scope': '/registry', 'roleDefinitionId': '/roles/8311e382-0749-4cb8-b61a-304f252e45ec'}
    with pytest.raises(infra.SafetyError, match='Duplicate'):
        infra.validate_roles([assignment, assignment], 'p', [('AcrPush', '/registry')])


def test_role_queries_use_supported_scope_flags_and_skip_graph_lookup():
    class StrictCloud:
        def read(self, command, **kwargs):
            assert not ('--all' in command and '--scope' in command), 'Azure CLI rejects scope plus all'
            assert '--assignee-object-id' in command, 'Object IDs must not require Graph resolution'
            return []
        def write(self, command): return {}
    infra.ensure_role(StrictCloud(), 'principal', 'AcrPull', '/registry', config())


def test_malformed_existing_identity_blocks_credential_creation():
    class EmptyIdentity(SecurityCloud):
        def read(self, command, **kwargs):
            if command[1:3] == ['identity', 'show']: return {}
            return super().read(command, **kwargs)
    cloud = EmptyIdentity()
    with pytest.raises(infra.SafetyError):
        infra.security(config(), cloud)
    assert cloud.writes == []


def test_empty_existing_diagnostic_is_not_treated_as_valid_or_absent():
    cloud = SecurityCloud()
    original = cloud.read
    cloud.read = lambda command, **kwargs: {} if 'diagnostic-settings' in command else original(command, **kwargs)
    with pytest.raises(infra.SafetyError):
        infra.setup(config(), cloud)
    assert cloud.writes == []


@pytest.mark.parametrize('shape', ['omitted', 'null'])
def test_actual_diagnostic_get_without_table_mode_accepts_verified_azure_monitor_environment(shape):
    value = diagnostic()
    if shape == 'omitted':
        value.pop('logAnalyticsDestinationType')
    else:
        value['logAnalyticsDestinationType'] = None
    infra.validate_diagnostic(value, config(), environment=foundation('env'))


def test_null_diagnostic_table_mode_requires_azure_monitor_environment_evidence():
    value = diagnostic() | {'logAnalyticsDestinationType': None}
    for environment in [None, foundation('env') | {'appLogsConfiguration': {'destination': 'log-analytics'}}]:
        with pytest.raises(infra.SafetyError):
            infra.validate_diagnostic(value, config(), environment=environment)


def test_setup_refuses_unregistered_provider_before_any_creation():
    class UnregisteredCloud(SecurityCloud):
        def read(self, command, **kwargs):
            if command[1:3] == ['provider', 'show']:
                return {'registrationState': 'NotRegistered', 'namespace': command[command.index('--namespace') + 1]}
            return super().read(command, **kwargs)
    cloud = UnregisteredCloud()
    with pytest.raises(infra.SafetyError, match='provider.*Registered'):
        infra.setup(config(), cloud)
    assert cloud.writes == []


def test_setup_checks_all_foundation_provider_prerequisites_read_only():
    class ProviderCloud(SecurityCloud):
        namespaces = []
        def read(self, command, **kwargs):
            if command[1:3] == ['provider', 'show']:
                self.namespaces.append(command[command.index('--namespace') + 1])
                return {'registrationState': 'Registered'}
            if 'diagnostic-settings' in command:
                return diagnostic() | {'logAnalyticsDestinationType': None}
            return super().read(command, **kwargs)
    cloud = ProviderCloud()
    infra.setup(config(), cloud)
    assert set(cloud.namespaces) == {'Microsoft.App', 'Microsoft.ContainerRegistry', 'Microsoft.OperationalInsights', 'Microsoft.Insights'}
    assert cloud.writes == []


def test_security_accepts_actual_null_diagnostic_get_with_verified_environment():
    class ActualDiagnosticCloud(SecurityCloud):
        def read(self, command, **kwargs):
            if 'diagnostic-settings' in command:
                return diagnostic() | {'logAnalyticsDestinationType': None}
            return super().read(command, **kwargs)
    cloud = ActualDiagnosticCloud()
    infra.security(config(), cloud)
    assert len(cloud.writes) == 8


@pytest.mark.parametrize('destination', ['storageAccountId', 'eventHubAuthorizationRuleId', 'marketplacePartnerId', 'serviceBusRuleId'])
def test_diagnostic_refuses_additional_export_destinations(destination):
    with pytest.raises(infra.SafetyError):
        infra.validate_diagnostic(diagnostic() | {destination: '/another/export/target'}, config())


def test_reader_at_subscription_scope_is_refused():
    assignments = [{'principalId': 'p', 'scope': '/subscriptions/sub', 'roleDefinitionId': '/roles/acdd72a7-3385-48ef-bd42-f606fba81ae7'}]
    with pytest.raises(infra.SafetyError, match='Unexpected'):
        infra.validate_roles(assignments, 'p', [('Reader', '/subscriptions/sub/resourceGroups/dedicated')])


def test_security_with_existing_exact_five_roles_and_credential_makes_no_writes():
    class ApprovedCloud(SecurityCloud):
        def __init__(self):
            super().__init__(existing=True)
        def read(self, command, **kwargs):
            if 'federated-credential' in command:
                name = command[command.index('--identity-name') + 1]
                return [] if name == 'id-recon-pull' else [{
                    'issuer': 'https://token.actions.githubusercontent.com',
                    'subject': 'repo:owner@99/recon@42:ref:refs/heads/main',
                    'audiences': ['api://AzureADTokenExchange']}]
            if command[1:4] == ['role', 'assignment', 'list']:
                principal = command[command.index('--assignee-object-id') + 1]
                group = '/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-payment-recon-demo'
                registry = group + '/providers/Microsoft.ContainerRegistry/registries/acrdemotest'
                pull = group + '/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-recon-pull'
                expected = [('7f951dda-4ed3-4680-a7ca-43fe172d538d', registry)] if principal == 'id-recon-pull-principal' else [
                    ('8311e382-0749-4cb8-b61a-304f252e45ec', registry),
                    ('358470bc-b998-42bd-ab17-a7e34c199c0f', group),
                    ('f1a07417-d97a-45cb-824c-7a7467783830', pull),
                    ('acdd72a7-3385-48ef-bd42-f606fba81ae7', group)]
                if '--scope' in command:
                    scope = command[command.index('--scope') + 1]
                    expected = [(role, target) for role, target in expected if target == scope]
                return [{'principalId': principal, 'roleDefinitionId': '/roles/' + role, 'scope': scope} for role, scope in expected]
            return super().read(command, **kwargs)
    cloud = ApprovedCloud()
    infra.security(config(), cloud)
    assert cloud.writes == []


def test_old_four_role_confirmation_is_rejected_before_external_commands(capsys):
    with patch.dict(infra.os.environ, ENV, clear=True), patch.object(infra.subprocess, 'run', side_effect=AssertionError('must reject old authorization')):
        assert infra.main(['security', '--execute', '--confirm-security', 'I approve the two identities and four scoped roles']) == 2
    assert 'five scoped roles' in capsys.readouterr().err
