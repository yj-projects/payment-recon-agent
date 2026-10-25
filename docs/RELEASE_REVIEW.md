# Portfolio release review

Reviewed on 2026-10-25. This is a local release-preparation assessment, not a
production security certification. No application behavior changed in this
milestone, and no publication commands below have been executed.

## Assessment

The source and documentation are ready for a final human review before GitHub
publication. The original location needs an explicit repository-boundary
decision: publish a curated copy outside the parent repository rather than
initializing a nested child repository in place.

## Verification

- `pytest -q`: **126 passed**, one existing upstream Starlette/HTTPX TestClient
  deprecation warning; verified using Python 3.12.
- `python scripts/evaluate.py`: exit 0, **12/12** exact payout statuses and
  candidate-ID sets, 7 matched, 5 payout exceptions, 1 unmatched bank row,
  **B-999 only**, no discrepancies.
- Deterministic HTTP/browser behavior was exercised in earlier milestones.
  This documentation milestone reruns automated checks; it does not claim new
  browser, recording, or paid-provider verification.
- Actual OpenAI SDK exchanges against mocked HTTP transport verify dispatch,
  outputs, reasoning/history preservation, and bounded failure handling.
  **No real provider call has been tested.** Model access and narrative quality
  remain unverified; a paid smoke test needs separate approval.

## Repository boundary findings

Read-only Git inspection found:

- `payment-recon-agent` has **no `.git` directory**.
- Git commands in this folder resolve to the existing parent **AI_Native** repo.
- The parent has an unborn HEAD: **no commits exist**.
- The parent's index contains **zero paths** for `payment-recon-agent`.
- Parent status reports `?? payment-recon-agent/`: the project is currently
  untracked, not a tracked subdirectory or registered submodule.

An in-place child `git init` would not overwrite the parent's `.git` or cause an
immediate merge conflict. It would introduce a nested repository. A later
parent `git add` could record the child as an embedded repository/gitlink rather
than ordinary project files, creating confusing ownership and clone behavior.
No parent ignore rule or index change is authorized here.

**Recommended boundary:** copy only the release file list to a new directory
outside AI_Native, verify that destination is outside any other repository, and
initialize there only after approval. Leave the original and parent `.git`
untouched. Another choice is a deliberate monorepo/submodule workflow, but that
requires a separate decision and changes outside this project's authorized scope.

## Security and privacy findings

- Read-only `git check-ignore --no-index` verifies exclusion of virtual
  environments, `.env`/`.env.*`, Python/test caches, logs, common private-key and
  certificate files, credential JSON files, cloud/SSH config, private notes,
  local agent settings, and internal working plans. `.env.example` is explicitly
  included and contains **an empty key** and a nonsecret model name.
- Scanning release-source text found **no matches** for OpenAI/GitHub/AWS key
  patterns, private-key headers, credential-bearing URLs, or nonempty example
  API-key assignments. Values were not printed by the scanner.
- Release text contains **no personal absolute filesystem paths**. Runtime
  virtual-environment metadata does contain machine paths and is excluded.
- No source symlinks, unexpected binaries, real credential files, or private
  documents were found among the intended release files. Tests use labeled
  fake keys; CSVs contain synthetic merchant/transaction data.
- A copy-command dry run exposed macOS-generated AppleDouble archive metadata.
  The recommended command disables that metadata and excludes `._*` files;
  the corrected archive was inspected in memory without extracting files.
- There are no existing project index entries that would bypass ignore rules.
  Ignore patterns do not sanitize already tracked files or secrets embedded in
  otherwise allowed source. This pattern scan cannot guarantee the absence of
  every possible secret or identify all future private files.
- No dependency vulnerability audit, penetration test, or license audit is
  claimed. The app remains a local synthetic-data demo without authentication.

## Intended release files

Include `.gitignore`, `.env.example`, `README.md`, `PROJECT_SPEC.md`,
`pyproject.toml`, `app/`, `data/`, `scripts/`, `static/`, `tests/`,
`docs/DEMO.md`, and this review. Exclude generated files inside these directories.

Do not copy the parent repository, virtual environment, caches, logs, local
credentials, internal plans, or personal media. `AGENTS.md` and
`README_START_HERE.md` remain in the working folder but are omitted from the
curated public release: they are project-generation instructions, not the
portfolio walkthrough. No files are deleted from the original folder.

## Recommended publication commands — not executed

Run only after explicit approval. These commands require Git and, for the final
step, an authenticated GitHub CLI. They are not setup instructions for the app.

### 1. Prepare an independent copy and inspect the staged release

Start in the existing `payment-recon-agent` folder. Stop if the destination
already exists or its parent is itself inside a Git repository. The destination
is a new directory under your home Projects folder, not a child of AI_Native.

```bash
set -eu
source_dir="$(pwd -P)"
test -f "$source_dir/pyproject.toml"
test -f "$source_dir/app/main.py"
release_parent="$HOME/Projects"
release_dir="$release_parent/payment-recon-agent"
mkdir -p "$release_parent"
if git -C "$release_parent" rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "Choose a destination outside any existing Git repository."
  exit 1
fi
test ! -e "$release_dir"
mkdir "$release_dir"
COPYFILE_DISABLE=1 tar --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='.DS_Store' --exclude='._*' \
  -C "$source_dir" -cf - \
  .gitignore .env.example README.md PROJECT_SPEC.md pyproject.toml \
  app data scripts static tests docs/DEMO.md docs/RELEASE_REVIEW.md \
  | tar -C "$release_dir" -xf -
cd "$release_dir"
git init -b main
git add .gitignore .env.example README.md PROJECT_SPEC.md pyproject.toml \
  app data scripts static tests docs/DEMO.md docs/RELEASE_REVIEW.md
git diff --cached --check
git diff --cached --stat
git diff --cached
```

**Stop and review every staged file before the next block.** Confirm the
destination root with `git rev-parse --show-toplevel`, inspect `.env.example`,
and confirm no environment, secret, private file, or generated output is staged.
Rerun setup/tests/evaluation in the new checkout before publishing if anything
changes. Avoid `git add .` from the parent AI_Native directory.

### 2. Commit and publish only the approved public snapshot

From the new independent checkout, after staged-file review and explicit public
release approval, replace `YOUR_GITHUB_USERNAME` with the intended account:

```bash
GITHUB_OWNER="YOUR_GITHUB_USERNAME"
git commit -m "Initial portfolio release: deterministic payment reconciliation agent"
gh repo create "$GITHUB_OWNER/payment-recon-agent" \
  --public --source=. --remote=origin --push
```

This final command **creates a public repository and pushes the commit**. Do not
run it without approval of the account, repository name, visibility, and staged
contents. If the repository name already exists, stop and inspect that repository
rather than overwriting a remote or force-pushing.

## Remaining decisions and nonblockers

- **Required before publication:** approval of the independent destination,
  intended GitHub account/name/public visibility, and final staged-file contents.
- **Not a publication blocker if accurately disclosed:** untested paid provider,
  the upstream TestClient warning, no CI/Docker/hosting, and no demo media yet.
- Screenshots/video can be supplied manually later; no recording permissions
  were requested. Add only reviewed synthetic-data media to a later release.
