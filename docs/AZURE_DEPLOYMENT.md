# Ephemeral Azure demo: provision, release, and remove

This is a synthetic, read-only portfolio demo. Cloud resources are temporary;
source, Docker packaging, tests, golden labels, agent cases, and lifecycle tools
remain in GitHub after teardown. No application deployment or image publication
was executed during Milestone 3B. AE-013 remains outside the mandatory CI gate.

## Current verification boundary

Milestone 3A provisioned the dedicated resource group, Basic registry, Consumption
Container Apps environment, workspace, and diagnostic routing in East US.
Consumption quota was verified at 0 used / 100 allowed cores; quota is capacity,
not an allocation or charge. Milestone 3B adds the two approved identities,
main-only GitHub federation, five scoped roles, and controlled release tooling.
Deployment is initially disabled. OIDC policy and role configuration can be
verified without deployment, but the first actual GitHub-to-Azure token exchange,
image publication, and application rollout remain unexecuted.

Public documentation intentionally contains no personal account names, literal
subscription/tenant/client IDs, infrastructure IPs, or live environment domains.
Discover those values using authenticated CLI commands; keep local configuration
and release receipts out of Git. These identifiers are parameters, not passwords.

## Resource and cost boundary

| Resource | Default name | Configuration |
|---|---|---|
| Resource group | `rg-payment-recon-demo` | Dedicated project ownership/cleanup boundary |
| Registry | `$AZURE_ACR_NAME` | Globally unique name; Basic; administrator disabled; RBAC registry mode |
| Environment | `cae-payment-recon-demo` | Workload-profiles environment with only Consumption; platform-managed network |
| Workspace | `law-payment-recon-demo` | PerGB2018; 30-day retention; 0.023 GB/day ingestion safeguard |
| Diagnostic setting | `recon-environment-logs` | Azure Monitor console/system categories to the workspace |
| Image-pull identity | `id-recon-pull` | User-assigned; registry pull only |
| Deployment identity | `id-recon-cd` | User-assigned; GitHub OIDC; scoped release permissions |
| App, after approved deployment | `ca-payment-recon-demo` | 0.25 vCPU / 0.5 GiB; 0–1 replicas; HTTPS; existing `/health` probes |

Required ownership tags are `project=payment-recon-agent`, `environment=demo`,
`cleanup=resource-group`. Setup additionally tags foundation resources
`milestone=3a`; identities and app use `milestone=3b`. Diagnostic settings are
child resources without independent tags. Avoid mixing unrelated resources into
this group. Week 2/3 resources must remain until the project is complete and must
be reviewed in the teardown inventory; untagged/unowned resources cause refusal.

The reviewed East US retail baseline is about USD 0.1666/day for ACR Basic
(USD 5.00 per 30 days; USD 5.16 per 31 days), including 10 GB storage. The empty
Consumption environment adds no app compute usage or Dedicated management fee.
Log ingestion is usage-based; the reviewed Analytics meter is USD 2.30/GB before
allowances. Exactly 0.023 GB/day for 31 days corresponds to about USD 1.64.
The ingestion safeguard can overshoot and can pause monitoring until reset;
it is not a financial cap. No Dedicated/Flex profiles, custom VNet, NAT gateway,
private endpoint, VM, storage account, paid registry build task, or extra
monitoring service is part of this setup.

A conservative later-demo example, excluding free grants: registry USD 5.16,
100 total replica-hours USD 2.70, 100,000 requests USD 0.04, 0.25 GB logs USD 0.58,
and USD 1 network allowance totals about USD 9.48 before taxes. This is a usage
target, not a spending cap. Public traffic, long-running replicas, extra images,
other learning projects, and logs can exceed it. The existing USD 30 billing-account
budget covers the selected subscription and other account spending; alerts do not
stop resources. Check scope, recipients, forecast thresholds, and expiry before
future demos. GitHub artifacts also have storage limits: image artifacts expire
after one day and release receipts after 30 days.

## Prerequisites and local parameters

Use Python 3.12, Azure CLI, GitHub CLI, Docker for local image checks, an enabled
Azure subscription, and an authenticated GitHub account with repository access.
Azure setup requires resource-management permissions; security additionally needs
permission to assign the five listed roles and manage user-assigned identities
and their federation. Subscription Owner has these permissions but is not granted
to the CI identity. GitHub variable configuration requires repository access to
manage Actions variables. No new Python package is required by lifecycle tools.

```bash
# From a fresh clone, use the project's Python 3.12 environment:
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

az login
az account list --all -o table
az account set --subscription '<your-approved-subscription-id>'
gh auth status

cp deployment/azure-demo.env.example deployment/azure-demo.env
# Edit the local file with subscription, tenant, unique registry, and repository.
# Use bash or zsh; set -a exports values to the Python tools.
set -a
source deployment/azure-demo.env
set +a

az account show --query '{subscription:id,tenant:tenantId,state:state}'
```

`deployment/*.env`, `deployment/*.local.json`, and the root release receipt are
Git-ignored. Never add API keys or real financial data. The example file is safe
to track. Local defaults match the names in the table; subscription and registry
have no hard-coded account defaults. Changing configuration does not move or
rename an existing resource. Scripts refuse incompatible existing configuration.

Inspect the provider states and East US managed-environment quota first. If needed,
register only the previously approved providers explicitly; setup never registers
providers or requests quota itself:

```bash
for provider in Microsoft.App Microsoft.ContainerRegistry Microsoft.OperationalInsights Microsoft.ManagedIdentity Microsoft.Insights; do
  az provider show --subscription "$AZURE_SUBSCRIPTION_ID" --namespace "$provider" \
    --query '{namespace:namespace,state:registrationState}'
  # Run only when registration is approved and required:
  # az provider register --subscription "$AZURE_SUBSCRIPTION_ID" --namespace "$provider" --wait
 done
az rest --method get --url "https://management.azure.com/subscriptions/$AZURE_SUBSCRIPTION_ID/providers/Microsoft.App/locations/$AZURE_LOCATION/usages?api-version=2025-07-01"
```

## Recreate the foundation explicitly

Preview first. No writes occur without `--execute` and the exact cost acknowledgment:

```bash
python scripts/azure_infra.py setup
python scripts/azure_infra.py setup --execute \
  --confirm-cost 'I understand Azure resources incur charges until deleted'
```

The script issues the same reviewed `az group create`, `az acr create --sku Basic
--admin-enabled false --role-assignment-mode rbac`, `az monitor log-analytics
workspace create --sku PerGB2018 --retention-time 30 --quota 0.023 --identity-type
None`, and `az containerapp env create --enable-workload-profiles true
--logs-destination azure-monitor` commands. It creates the environment diagnostic
setting with the two console/system categories, the workspace resource ID, and
`--export-to-resource-specific true`. Existing matching resources are reused;
unknown ownership, incompatible plans/settings, provider failures, and network
or authentication errors stop the operation rather than appearing as absence.
There is no automatic cleanup after a partial setup failure: inspect the inventory
and choose whether to retry or explicitly tear down.

No Container App, image, identity, federation, or role is created by `setup`.
Names and full resource IDs can be inspected without committing them:

```bash
az resource list --subscription "$AZURE_SUBSCRIPTION_ID" \
  --resource-group "$AZURE_RESOURCE_GROUP" -o table
az containerapp env list-usages --subscription "$AZURE_SUBSCRIPTION_ID" \
  -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_CONTAINERAPPS_ENVIRONMENT"
az containerapp env workload-profile list --subscription "$AZURE_SUBSCRIPTION_ID" \
  -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_CONTAINERAPPS_ENVIRONMENT"
```

Resource IDs have prefix `/subscriptions/$AZURE_SUBSCRIPTION_ID/resourceGroups/
$AZURE_RESOURCE_GROUP`, followed by the appropriate provider and resource name.
The environment's generated domain/IP and Consumption profile state are platform
metadata, not deployed application ingress. No separately billed network ARM
resource or infrastructure group was found in the 3A inventory. Azure may remove
an empty environment after prolonged idleness; ACR/workspace cleanup must still
be deliberate. Never deploy an app just to keep an idle environment alive.

## Security setup and the actual OIDC subject

```bash
python scripts/azure_infra.py security
python scripts/azure_infra.py security --execute \
  --confirm-security 'I approve the two identities and five scoped roles'
```

Security setup creates only `id-recon-pull` and `id-recon-cd`. It reads this
repository's OIDC policy and numeric owner/repository IDs through `gh api` and
checks the actual subject format. This repository reports `use_default: true`
and `use_immutable_subject: true`. The subject is therefore:

```text
repo:OWNER@OWNER_ID/REPOSITORY@REPOSITORY_ID:ref:refs/heads/main
```

The older name-only subject is not valid for this repository. The script checks
GitHub's returned subject prefix against the immutable IDs and refuses unknown
custom policies. It never changes GitHub's OIDC settings. Do not add a GitHub
`environment:` to the release job without reviewing federation: environment
context changes the subject and would require a different trust policy.

Federation uses issuer `https://token.actions.githubusercontent.com`, audience
`api://AzureADTokenExchange`, and the exact main-only subject. No wildcard,
client secret, certificate, or long-lived Azure credential is created.

| Identity | Built-in role | Scope |
|---|---|---|
| Pull | AcrPull | Demo ACR only |
| CD | AcrPush | Demo ACR only |
| CD | Container Apps Contributor | Dedicated demo resource group |
| CD | Managed Identity Operator | Pull identity only |
| CD | Reader | Dedicated demo resource group; foundation/configuration preflight only |

The group-scoped app role is needed for first app creation, read checks, and
joining its environment. Reader supplies the otherwise missing foundation read
permissions for registry/workspace/diagnostics and identity inspection; this fifth
assignment was separately approved. Neither role grants role-assignment authority.
The app role can manage other Container Apps in this dedicated
group, so keep the group purpose-specific. It cannot create role assignments or
provision arbitrary foundation resources. AcrPush can manage image metadata and
locks; these protect against accidental overwrites, not a compromised principal
with permission to unlock them. Existing unexpected roles/federation cause refusal.

Configure nonsecret repository variables after security setup. Repeat after
recreation because managed identity client IDs change. Keep deployment disabled:

```bash
export AZURE_CLIENT_ID=$(az identity show --subscription "$AZURE_SUBSCRIPTION_ID" \
  -g "$AZURE_RESOURCE_GROUP" -n id-recon-cd --query clientId -o tsv)
gh variable set AZURE_DEPLOYMENT_ENABLED --repo "$GITHUB_REPOSITORY" --body false
for name in AZURE_SUBSCRIPTION_ID AZURE_TENANT_ID AZURE_CLIENT_ID AZURE_ACR_NAME AZURE_RESOURCE_GROUP AZURE_LOCATION AZURE_CONTAINERAPPS_ENVIRONMENT AZURE_LOG_ANALYTICS_WORKSPACE AZURE_APP_NAME; do
  value=$(printenv "$name")
  gh variable set "$name" --repo "$GITHUB_REPOSITORY" --body "$value"
done
```

No GitHub secrets are required. Default pull/CD identity names are fixed in the
workflow. The enable switch is an operational safeguard; branch protection and
review of trusted main code remain important. Branch rules are not changed here.

## Controlled release: future approval required

The existing `.github/workflows/ci.yml` continues PR and main-push CI. Those
runs do not authenticate to Azure, upload an image, or deploy. A manual
`workflow_dispatch` on main additionally requires the enable variable and exact
confirmation. Non-main, disabled, malformed, or unconfirmed requests fail early.

Every accepted manual release reruns pytest, reconciliation golden evaluation,
and Docker build/smoke checks. The demo-agent evaluator stays outside this gate
because AE-013 intentionally remains a documented failure.

The smoke job saves its tested image to a short-lived artifact and outputs the
Docker image ID. The release job downloads/loads it and checks that ID; it never
rebuilds. After ACR push, either the registry manifest/index digest or its config
digest must match the tested ID, accommodating containerd and classic Docker
image stores. These are content-addressed identities, not a rebuild-equivalence
claim. The full commit-SHA tag and manifest are locked against writes
and deletion, then the app is deployed by registry digest. Repeated deploys of
an existing SHA tag fail closed; use a new commit or receipt-backed rollback.
A failure after publication can leave a retained image/tag; inspect the failed
receipt and registry before deciding on recovery. No failed check is suppressed.
Canceled or timed-out runs may end without a final receipt; inspect Azure and
use the last successful receipt rather than assuming the rollout was undone.

Only the release job receives `id-token: write`; CI jobs retain `contents: read`.
The release job also has read-only Actions access for receipt provenance checks.
Deploy and rollback share a concurrency group with in-progress cancellation
disabled. Foundation absence/configuration drift stops the release; CD never
calls setup or silently recreates the cloud environment.

After reviewing/merging the PR and approving the first cloud deployment:

```bash
# Do not run during Milestone 3B.
gh variable set AZURE_DEPLOYMENT_ENABLED --repo "$GITHUB_REPOSITORY" --body true
gh workflow run ci.yml --repo "$GITHUB_REPOSITORY" --ref main \
  -f operation=deploy -f confirmation='DEPLOY SYNTHETIC DEMO'
gh run list --repo "$GITHUB_REPOSITORY" --workflow ci.yml
# Then: gh run watch <run-id> --repo "$GITHUB_REPOSITORY" --exit-status
```

The first approved release creates the Container App; later releases update it.
The generated manifest uses only the pull identity, an empty `OPENAI_API_KEY`,
0.25 vCPU/0.5 GiB, 0–1 replicas, HTTPS ingress to port 8000, and HTTP startup,
readiness, and liveness probes on `/health`. No LLM SDK extras, production
credentials, real financial data, or financial writes are added.

## Release verification and rollback

The release script verifies the deployed digest and public HTTPS `/health`,
`/docs`, `/reconcile`, `/exceptions`, demo-agent summary evidence, and refund
refusal. It records a receipt only as succeeded after acceptance passes. A failed
acceptance leaves the workflow red; no automatic rollback hides the failure.

The `azure-release-<run-id>` artifact contains `release-receipt.json`: source run,
workflow/image commits, tested image ID, deployed digest, previous digest, and
acceptance status. Receipts are retained for 30 days. Download required receipts
before expiry to ignored local storage. GitHub retains source and test/evaluation
fixtures after Azure teardown; existing ignored raw evaluation runs are not
modified or automatically published. Recreating the registry requires a fresh
validated release because old image storage is intentionally deleted.

For a rollback, select a prior successful manual main run and its **receipt's
own digest** (not merely the previous_digest field). The workflow verifies that
run's repository, event, branch, conclusion, and workflow ID before downloading
its artifact. The release script verifies receipt provenance, registry/config,
locks, and pulled image ID; it smoke-tests that old image locally before updating
an existing app. No rebuild or push occurs, and a missing app is not recreated.

```bash
# Future explicit rollback only; not executed during Milestone 3B.
gh workflow run ci.yml --repo "$GITHUB_REPOSITORY" --ref main \
  -f operation=rollback -f confirmation='ROLLBACK SYNTHETIC DEMO' \
  -f rollback_run_id='<prior-successful-manual-run-id>' \
  -f rollback_digest='sha256:<digest-from-that-receipt>'
```

This rollback path still reruns current CI and smoke checks, plus old-image
acceptance. Receipt expiry, a locked-tag publication failure, a missing registry,
or insufficient access can require operator review. Never relax identity trust,
remove locks, or recreate resources automatically to bypass an error.

## Cloud logs

The environment must use `properties.appLogsConfiguration.destination =
azure-monitor`; console/system diagnostics must target the intended workspace.
Current tables are `ContainerAppConsoleLogs` and `ContainerAppSystemLogs`, not
legacy `_CL` tables. The 3A creation response acknowledged `Dedicated`, while
subsequent diagnostic GET omitted/null-normalized that field. Actual workspace
metadata confirmed the resource-specific Analytics tables with 30-day retention;
a system-table count query succeeded with zero environment records at that time.
Do not confuse log-table mode with a Dedicated compute profile or claim event
delivery from configuration alone. Verify real records after the first release.

```bash
az containerapp logs show --subscription "$AZURE_SUBSCRIPTION_ID" \
  -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_APP_NAME" --type console --follow
# Use --type system for platform events.
```

In the workspace query editor:

```kusto
ContainerAppConsoleLogs
| where ContainerAppName == "ca-payment-recon-demo"
| extend event = parse_json(Log)
| where tostring(event.request_id) == "<X-Request-ID>"
| project TimeGenerated, RevisionName, Log
| order by TimeGenerated asc
```

Read-only Logs Query API calls through `az rest` can avoid installing a query
extension. Check request IDs, HTTP status, revision, and privacy after deployment.
No question bodies, headers, provider payloads, or tool arguments/results should
appear. Public unauthenticated access allows visitors/bots to generate traffic
and costs; max-one-replica and log caps are safeguards, not spending limits.

## Disable deployment and explicitly tear down after the project

Do **not** run teardown during Week 2 or Week 3 while these resources are needed.
Export desired receipts, logs, and synthetic evaluation evidence first. Source,
Dockerfile, tests, golden fixtures, evaluation code, and deployment tools stay in
GitHub. Inspect any other project resource groups created by later learning work.

```bash
# Safe operational disable; does not delete infrastructure.
gh variable set AZURE_DEPLOYMENT_ENABLED --repo "$GITHUB_REPOSITORY" --body false

# Run in a local interactive terminal when deletion is explicitly intended:
python scripts/azure_infra.py teardown
python scripts/azure_infra.py teardown --execute
```

Teardown refuses CI/GITHUB_ACTIONS and non-interactive stdin/stdout. It checks
ownership tags, shows the full deletion inventory, and requires typing exactly
`DELETE <subscription-id>/<resource-group-name>` using the configured real values.
There is no confirmation bypass. After the typed confirmation it first disables
the GitHub deployment variable; if that fails, deletion does not start. It then
runs `az group delete --yes` only for the displayed dedicated group. Deleting the
identities also removes their federation; resource-scoped roles disappear with
the deleted scope. No workflow or schedule invokes teardown.

Verify cleanup separately:

```bash
python scripts/azure_infra.py verify-clean
az group exists --subscription "$AZURE_SUBSCRIPTION_ID" --name "$AZURE_RESOURCE_GROUP"
az resource list --subscription "$AZURE_SUBSCRIPTION_ID" \
  --tag project=payment-recon-agent -o table
az monitor log-analytics workspace list-deleted-workspaces \
  --subscription "$AZURE_SUBSCRIPTION_ID" -o table
```

Expected active inventory: group absent and no project-tagged resources. Review
later-week resources outside this group, untagged resources, and any service-owned
infrastructure separately. Log Analytics soft-deleted data may remain recoverable;
permanent purge requires separate explicit approval and is never automatic.
Cost reporting lags usage: review Cost Analysis after its next updates and the
billing statement before claiming no further project charges. The account budget,
provider registrations, GitHub repository, and source remain. Stale GitHub IDs
must be refreshed after the next explicit setup/security run; missing resources
cause CD to fail rather than incur charges by recreating them.

## References

- [GitHub immutable OIDC subjects](https://docs.github.com/en/actions/reference/security/oidc)
- [Microsoft Entra immutable-subject guidance](https://learn.microsoft.com/en-us/entra/workload-id/workload-identities-github-immutable-subjects)
- [ACR role capabilities](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-rbac-built-in-roles-directory-reference)
- [Container Apps current Azure Monitor logging](https://learn.microsoft.com/en-us/azure/container-apps/migrate-logs-azure-monitor)
- [Container Apps billing](https://learn.microsoft.com/en-us/azure/container-apps/billing)
- [Log Analytics daily-cap behavior](https://learn.microsoft.com/en-us/azure/azure-monitor/logs/daily-cap)
