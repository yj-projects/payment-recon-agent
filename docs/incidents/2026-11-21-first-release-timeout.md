# First Azure release: healthy application, cancelled workflow

## Outcome and scope

[Run 44955542503](https://github.com/yj-projects/payment-recon-agent/actions/runs/44955542503)
is **cancelled**, not successful. The release job exceeded its configured
20-minute execution limit. Azure completed the accepted deployment independently;
later operator checks verified the live application and stored request logs.
No successful workflow release receipt was retained. Later evidence does not
change that historical outcome and cannot authorize rollback.

The deployed commit is `8028b88f4a83dfe0778045bb2e47fd1d81e65caa`.
The CI-built image ID is
`sha256:572b38a00893f8b3aeed560922995bd087a5055be6049ae193b5636cf886a933`.
The published/deployed digest is
`sha256:c09484ffa4e14f01f3eb299bddfeff04531622e45ae3105c674bcf381c24648b`.
The first healthy revision is `ca-payment-recon-demo--0a2e9q2`.
Public account IDs, registry hostnames, environment domains, and raw logs are
intentionally omitted from this document.

## Evidence timeline (UTC, November 21, 2026)

| Time | Evidence |
|---|---|
| 22:12:36 | Single manual dispatch on the approved main commit |
| 22:13:00 | 318 tests and 12/12 reconciliation golden agreement passed |
| 22:13:22 | Docker image built; smoke checks subsequently passed |
| 22:13:42 | Tested image downloaded and loaded without rebuilding |
| 22:13:51 | GitHub OIDC authentication completed; publication/deployment step began |
| 22:14:20.648 | Azure activity log: Container App write accepted, HTTP 201 |
| 22:33:10 | Revision `createdTime`; creation time is not proof of readiness |
| 22:33:16.778 | Azure activity log: Container App write `Succeeded` |
| 22:33:43 | GitHub cancelled the step; annotation: maximum execution time 20m0s |
| 22:33:45 | Workflow completed with conclusion `cancelled`; receipt upload skipped |
| 22:35:43 | System-table events include `ContainerAppReady` and `RollingRevisionCompleted` |
| 22:37:16 | Operator synthetic probe returned HTTP 200 in demo mode |
| 22:45:05 | Operator confirmed three request-correlated records in `ContainerAppConsoleLogs` |

The publication/deployment step consumed **19m52s**. Azure acceptance to the
reported successful write took about **18m56s**. Image transfer and OIDC took
seconds; the remote provisioning/readiness wait dominated. ARM write completion,
revision metadata, and application readiness are separate observations.

During provisioning, a read-only revision-list call returned
`ManagedClusterSuspended` with an unknown-reason message. The environment GET
still reported `Succeeded`. Subsequent system records include replica-assignment
and image-pull events, including `AssigningReplicaFailed`; they do not establish
why the environment was suspended or why provisioning took this long. No manual
resume, environment recreation, RBAC expansion, or second dispatch was performed.

## Confirmed cause and contributing design gaps

- The immediate cause of cancellation is the GitHub job timeout, confirmed by
  its check annotation. The app continued provisioning outside the runner.
- `az containerapp create` waited synchronously. The wrapper captured output
  until completion, so the operator could not see readiness progress in that step.
- Receipt JSON was first written in `finally`; it was not checkpointed before
  remote side effects. A hard cancellation can prevent both finalization and the
  later artifact-upload step. The run retained only its short-lived tested-image
  artifact, not a successful release receipt.
- An initial operator jobs-metadata GET returned 404. The temporary monitoring
  script disabled deployment early, then read-only monitoring resumed against the
  same run. Already-evaluated job context allowed the authorized run to continue.
  This monitoring mistake is separate from Azure's provisioning delay; the
  operator did not cancel the GitHub run.

Possible platform warm-up, suspended-compute recovery, or reconciliation delays
remain hypotheses. There is no evidence that an application-code change,
long-lived credential, extra permissions, or additional infrastructure was needed.
Do not present any hypothesis as Microsoft's confirmed root cause.

## Preserved operator evidence

The original temporary acceptance, log-verification, and resource-verification
JSON files still existed, as did the live-console verification file. Sanitized
copies and source/sanitized SHA-256 hashes are saved locally under:

```text
private/release-evidence/2026-11-21-run-44955542503/
```

This directory is Git-ignored. Copies redact account identifiers, registry and app
hostnames; no raw CLI responses or question/header/body payloads are published.
An archive manifest retains provenance hashes without copying private values into
Git. A later read-only observation records health, revision timestamps, activity
and system-event metadata, exposure, scale, and reported costs. Local evidence
files are not a replacement for the missing workflow receipt.

The verified application passed HTTPS health/docs/exceptions, dashboard loading,
all 12 golden cases, demo-agent evidence, and refund refusal. A unique synthetic
marker sent in a question, header, and query was absent from stored logs while
the same request ID matched tool, agent, and HTTP-200 metadata events. This is
bounded evidence for the inspected request, not a guarantee about every possible
future payload. AE-013 remains an unchanged evaluation limitation outside CI.

## Reliability changes and limits

The existing workflow retains its PR/main CI gates, manual-main restriction,
job-scoped OIDC, image verification, and deployment enable switch. The proposed
release job budget is 45 minutes. Publication has an 8-minute step budget;
rollout/rollback have 35-minute step budgets. Normal command calls are bounded
at 60 seconds, Docker push/pull at 300 seconds. Publication is never retried.

Publication now produces a distinct `release-checkpoint` bound to the same
repository/run/attempt/commit, tested image, resource configuration, and initial
app presence. The workflow uploads it **before** an app write. Completion
revalidates that checkpoint and locked image, submits one `--no-wait` create or
update, then polls GETs against a 30-minute deadline. An in-flight CLI read can
finish after that deadline; individual calls and the enclosing step/job have
their own hard bounds. It reports progress and requires
the expected healthy revision/digest. Acceptance has three bounded attempts;
redirects are rejected. Failure or deadline expiry never triggers another write,
unlock, infrastructure recreation, or automatic rollback.

Normal receipts are written atomically before side effects and after each phase.
SIGTERM/SIGINT preserve interrupted status where execution is possible. SIGKILL,
runner loss, service failures, or the overall job deadline can still prevent final
upload; the already-uploaded publication checkpoint remains the recovery anchor.
Neither a checkpoint nor a post-incident verification is a successful release.

The local `scripts/azure_verify.py` command uses existing read-only CLI access to
check a completed source run, locked tag/digest, tested image identity, approved
configuration, healthy revision, and HTTP acceptance. It performs no login,
publication, app write, or resource creation. It works while deployment is
disabled and emits `kind=post-incident-verification`, `status=verified`, preserving
the source conclusion `cancelled`. It refuses to overwrite evidence.

The new verifier was exercised read-only against this existing deployment. Its
final local evidence is `post-incident-verification-3.json`, with status `verified`,
all seven acceptance checks, and source conclusion `cancelled`. Earlier failed
verification files were preserved. Tests cover Azure's normalized null/empty
fields, generated revision metadata, transient HTTP responses, stale ready
digests, and interruption during acceptance. This validates inspection; the
modified publication/rollout steps have not been exercised in a new cloud run.

## Exposure, costs, and safe next action

The app is public and unauthenticated, configured for 0.25 vCPU / 0.5 GiB and
0–1 replicas. A recheck observed zero replicas before an HTTP health probe; a
probe can wake the app, so that observation is not a permanent cost guarantee.
Reported month-to-date cost at review remained USD 2.151268 for ACR, with billing
lag. ACR's continuing planning baseline is about USD 5/month; traffic, running
replicas, logs, and egress can add charges. The USD 30 account budget is an alert,
not a cap, and was last verified to expire December 31, 2026.

Keep deployment disabled. Do not rerun the cancelled release or overwrite its
locked tag just to obtain a green run. Read-only verification preserves useful
evidence without a deployment. Temporarily disabling public ingress is recommended
when no demonstration is scheduled, after evidence is preserved; that requires
separate approval and was not performed. It does not remove registry charges or
guarantee all compute stops; verify replica behavior after any approved change.

Review/merge the reliability PR and confirm main CI first. A future release needs
separate authorization, the current validated main commit and its new immutable
tag, and actual successful acceptance/receipt retention. Do not redeploy solely
to rewrite incident history. Stop before Foundry integration or rollback work.

## References

- [Incident workflow and logs](https://github.com/yj-projects/payment-recon-agent/actions/runs/44955542503)
- [Azure CLI Container Apps non-blocking operations](https://learn.microsoft.com/en-us/cli/azure/containerapp)
- [GitHub step and job timeout semantics](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)
