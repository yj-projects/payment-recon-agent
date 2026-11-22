# Engineering sprint closure and teardown preparation

Snapshot: November 21, 2026, America/New_York. **Preparation only: nothing deleted.**
Deployment flag verified `false`. Foundry's single authorized inference is complete;
there is no remaining inference authorization. No new deployment was attempted.

## Portfolio evidence

- [README](../README.md): architecture, milestones, measured results, limitations.
- [Azure lifecycle guide](AZURE_DEPLOYMENT.md): repeatable setup/security/release,
  resource-specific log queries, rollback procedure, and cleanup controls.
- [Original release incident](incidents/2026-11-21-first-release-timeout.md):
  cancelled run, successful later operator verification, hypotheses vs. facts.
- [Foundry lab](../experiments/foundry/README.md): reusable stdlib Python,
  five synthetic cases, offline tests, pricing, and sanitized one-call evidence.
- [Foundry result](../experiments/foundry/results/2026-11-21-one-request.json):
  5/5 decisions, 4/4 substantive factual cases, no unsupported structured facts,
  one refusal passed, 556 tokens, 6.420-second end-to-end latency.
- [Main CI before this branch](https://github.com/yj-projects/payment-recon-agent/actions/runs/44963961561):
  481 tests, golden evaluation and Docker checks. This branch adds ten offline
  lab tests; final local suite is 491 passing with one dependency warning.

The original release run remains cancelled. Improved CD has no second live
release validation; actual rollback remains untested. Foundry is standalone,
not application integration. AE-013 remains an unchanged failing demo-agent case
outside mandatory CI. Neither small fixture measures real-world accuracy.

## Read-only Azure inventory

All eight ARM resources in the selected subscription are in
`rg-payment-recon-demo`. This was its only resource group. No deletion locks,
separate infrastructure resource group, customer VNet, VM, storage, search,
private endpoint, or extra monitoring service was found. The environment reports
`infrastructureResourceGroup=null`; platform-internal compute is not an additional
customer-owned ARM resource in this inventory.

| Resource | Name / discovery | Configuration |
|---|---|---|
| ACR | `$AZURE_ACR_NAME` | Basic; administrator disabled; one repository `payment-recon-agent`, one manifest |
| Container App | `ca-payment-recon-demo` | External HTTPS, port 8000; 0.25 vCPU / 0.5 GiB; min 0, max 1; observed zero replicas |
| Container Apps environment | `cae-payment-recon-demo` | East US, Consumption only, Azure Monitor log destination |
| Log Analytics | `law-payment-recon-demo` | PerGB2018, 30-day retention, 0.023 GB/day ingestion safeguard |
| Pull identity | `id-recon-pull` | ACR pull only |
| CD identity | `id-recon-cd` | Four scoped roles; one `github-main` OIDC federation child |
| Foundry account | `$AZURE_FOUNDRY_ACCOUNT` | AIServices S0, West US 3, system identity, project management enabled |
| Foundry project | `proj-recon-mini-lab` | Child of account, system identity, no model deployments |

Child configurations: environment diagnostic setting `recon-environment-logs`
routes enabled console/system categories to the workspace; the registry holds
the original SHA-tagged/digest image. Five deployment role assignments plus one
human Foundry User assignment are all scoped to the demo group or its children.
The existing subscription Owner assignment is outside this group and must remain.
System-assigned identities belong to their parent resources; they are not
separate user-assigned identity resources or separate compute reservations.

The app still serves its original digest; no redeployment occurred during closure.
No dedicated project ARM resource was found outside the group. GitHub source,
Actions history/variables, registered providers, and the existing billing-account
USD 30 budget are outside the group's lifecycle. Preserve them; the budget is
an alert, not a spending cap. No subscription or tenant deletion is proposed.

Private authenticated snapshots are retained locally in `private/closure/`;
resource IDs and live endpoints are deliberately excluded from public docs.

## Continuing cost and exposure

Cost Management currently reports **USD 2.1859767594 month-to-date**, all attributed
to Container Registry in this group. This is delayed actual-cost reporting, not
proof of zero Container Apps, logging, or Foundry charges. Foundry's measured
token estimate is **USD 0.000356** before tax and may appear later.

ACR Basic continues its roughly USD 5/month baseline while retained. The public
Container App can scale from zero on requests and incur compute/request/egress
and log ingestion costs. Min zero and the deployment flag do not close ingress.
The log cap may overshoot or pause observability; it is not a financial cap.
Foundry has no reserved model deployment/hosting commitment, but retained access
could permit future usage. No inference is scheduled or approved.

## Exact future teardown procedure — separate approval required

Do not run deletion commands until the owner explicitly approves teardown.
First preserve the PR, sanitized artifacts, incident summary and private operator
evidence locally. Deletion removes the public app, ACR image, cloud logs, and
Foundry endpoint; it does not remove GitHub or local source/evidence.

Use an interactive local **Bash** terminal with Azure CLI and GitHub CLI signed in.
Owner permissions on the selected subscription are already available; do not
assign any additional roles. Set parameters from the reviewed local configuration:

```bash
export AZURE_SUBSCRIPTION_ID='<approved-subscription-id>'
export AZURE_RESOURCE_GROUP='rg-payment-recon-demo'
export GITHUB_REPOSITORY='yj-projects/payment-recon-agent'
az account show --query '{name:name,id:id,state:state}' -o json
gh variable get AZURE_DEPLOYMENT_ENABLED --repo "$GITHUB_REPOSITORY"
az group show --subscription "$AZURE_SUBSCRIPTION_ID" --name "$AZURE_RESOURCE_GROUP"
az resource list --subscription "$AZURE_SUBSCRIPTION_ID" \
  --resource-group "$AZURE_RESOURCE_GROUP" -o table
az lock list --subscription "$AZURE_SUBSCRIPTION_ID" -o table
az cognitiveservices account deployment list \
  --subscription "$AZURE_SUBSCRIPTION_ID" -g "$AZURE_RESOURCE_GROUP" \
  -n '<verified-foundry-account-name>' -o json
```

Expect the reviewed eight resources, flag `false`, no locks and no model
deployments. Stop on a subscription mismatch, unexpected resource, deployment,
lock, or permission error. Do not reinterpret an authentication error as absence.

The existing `azure_infra.py teardown` refuses the untagged Foundry child project.
This is expected fail-closed behavior. Do not weaken its ownership check, add tags
silently, or force it through. The reviewed whole-group command below includes
that child by containment; it is the explicit manual teardown plan for this final
inventory, not an automatic infrastructure workaround.

After approval and inventory review, this function requires a typed scope match
and then Azure CLI's own deletion confirmation. It performs no recreation/purge:

```bash
delete_reviewed_demo_group() {
  local selected enabled answer
  selected=$(az account show --query id -o tsv) || return 1
  [[ "$selected" == "$AZURE_SUBSCRIPTION_ID" ]] || return 1
  [[ "$AZURE_RESOURCE_GROUP" == 'rg-payment-recon-demo' ]] || return 1
  enabled=$(gh variable get AZURE_DEPLOYMENT_ENABLED --repo "$GITHUB_REPOSITORY") || return 1
  [[ "$enabled" == false ]] || return 1
  printf 'Type DELETE %s/%s: ' "$AZURE_SUBSCRIPTION_ID" "$AZURE_RESOURCE_GROUP"
  read -r answer || return 1
  [[ "$answer" == "DELETE $AZURE_SUBSCRIPTION_ID/$AZURE_RESOURCE_GROUP" ]] || return 1
  az group delete --subscription "$AZURE_SUBSCRIPTION_ID" --name "$AZURE_RESOURCE_GROUP"
}
# DESTRUCTIVE: invoke only after separate owner approval.
delete_reviewed_demo_group
```

All inventoried dedicated resources and their child settings/federation fit this
group boundary. Group deletion must still be verified; a request acknowledgment
is not completion, and retained soft-deleted services need separate review.

## Read-only post-deletion verification

```bash
az group exists --subscription "$AZURE_SUBSCRIPTION_ID" --name "$AZURE_RESOURCE_GROUP"
# Expected false; inspect the entire subscription, not only tagged resources:
az resource list --subscription "$AZURE_SUBSCRIPTION_ID" -o table
az resource list --subscription "$AZURE_SUBSCRIPTION_ID" --tag project=payment-recon-agent -o table
az group list --subscription "$AZURE_SUBSCRIPTION_ID" -o table
az role assignment list --subscription "$AZURE_SUBSCRIPTION_ID" --all -o json
az cognitiveservices account list-deleted --subscription "$AZURE_SUBSCRIPTION_ID" -o json
az monitor log-analytics workspace list-deleted-workspaces \
  --subscription "$AZURE_SUBSCRIPTION_ID" -o json
gh variable get AZURE_DEPLOYMENT_ENABLED --repo "$GITHUB_REPOSITORY"
```

Expect no active project resources, no project-scoped roles, and flag `false`;
the human subscription Owner assignment remains. Use preserved principal IDs to
inspect any orphaned project assignment, without deleting unrelated permissions.
Confirm the former app hostname no longer serves the demo; DNS caching can lag.

Log Analytics normally retains soft-deleted configuration/data for 14 days;
Foundry names can remain reserved for 48 hours. Listing these is read-only;
**purging requires separate approval** and is not part of the above command.
Foundry provisioned deployments can continue charges after soft deletion; none
exists here, so recheck deployment count before deletion rather than assuming.
See [workspace deletion](https://learn.microsoft.com/en-us/azure/azure-monitor/logs/delete-workspace)
and [Foundry recovery/purge](https://learn.microsoft.com/en-us/azure/ai-services/recover-purge-resources).

Review Cost Management by resource/group after billing catches up (including
late-arriving inference and logs). Keep the budget alerts. Report active-resource
absence separately from soft-delete retention and final billed charges; do not
claim zero project-specific billable resources until those checks support it.
