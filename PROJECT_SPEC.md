# PROJECT_SPEC.md — Payment Reconciliation Agent (MVP)

## 1. Product story
A marketplace/merchant wants to check that expected payouts in an internal settlement ledger appeared in a bank deposit export. A reviewer needs to locate exceptions and ask a plain-English question about why a payout is not reconciled.

**Portfolio thesis:** deterministic financial controls + FastAPI + evaluations + audit-friendly logging + an optional read-only LLM tool-calling interface. This is synthetic, educational software, not a production finance control.

## 2. Definition of done (5-hour cap)
- Read two committed CSV fixtures from `data/`.
- Expose deterministic recon results via FastAPI, with traceable per-payout status and reason.
- Provide a tiny interactive browser UI at `/` and built-in API docs at `/docs`.
- Compare outcomes against the provided `expected_labels.csv` in an evaluation script.
- Include unit and API tests for correctness and edge cases.
- Implement an LLM-powered `/agent/ask` that invokes **read-only, predefined tools** when OPENAI_API_KEY is present; a clearly labeled deterministic demo mode if not.
- Produce human-readable README, minimal structured logs, Dockerfile, and, if simple, GitHub Actions CI.

## 3. Exact synthetic CSV schemas
`data/ledger.csv`:
- `payout_id` (unique text)
- `merchant_id` (text)
- `currency` (ISO 4217 text)
- `expected_amount_cents` (integer minor units)
- `expected_date` (YYYY-MM-DD)
- `reference` (text)

`data/bank.csv`:
- `bank_txn_id` (unique text)
- `merchant_id` (text)
- `currency` (ISO 4217 text)
- `actual_amount_cents` (integer minor units)
- `posted_date` (YYYY-MM-DD)
- `reference` (text)

`data/expected_labels.csv`:
- `payout_id`, `expected_status`, `expected_bank_txn_ids`, `expected_reason`
- `expected_bank_txn_ids` is semicolon-delimited if multiple candidates exist, empty if none.

**No machine learning model is needed**. Keep data loader and matching logic deterministic and easy to inspect.

## 4. Matching rules and priority
Reference normalization: strip all characters other than A-Z and 0-9 and uppercase. Normalize currency case.

For each ledger payout:
1. Select bank candidates with the **same merchant_id and normalized reference** (do not pre-filter by amount, currency, or date; otherwise exceptions become hidden).
2. If there are zero candidates, label `MISSING_IN_BANK`.
3. If there are more than one candidate, label `DUPLICATE_CANDIDATES`. Preserve and show every candidate ID. Do not auto-select one.
4. If there is exactly one candidate and currency differs, label `CURRENCY_MISMATCH`.
5. If currency matches but amount in cents differs, label `AMOUNT_MISMATCH` (include signed difference in cents: bank minus expected).
6. If all above match but bank posted date is before expected date or more than **2 calendar days after** it, label `DATE_OUT_OF_WINDOW`.
7. Else label `MATCHED`.

Do not do fuzzy matching, fee netting, tolerance matching, one-to-many payouts, reversals, or automatic resolution in MVP. One bank row with no candidate association to any ledger payout is `UNMATCHED_BANK`; report it separately. These rules are an illustrative convention, not a banking standard.

Expected seeded demo outcome: **12 ledger payouts; 7 MATCHED; 5 payout exceptions** (one of each of the five exception categories); and **1 UNMATCHED_BANK** row. Exception-heavy fixture is intentional and should not be marketed as representative accuracy or incidence.

## 5. API contract (suggested; keep implementation practical)
- `GET /health` => `{"status":"ok"}`.
- `GET /reconcile` => `{summary:{total_payouts,matched,exceptions,unmatched_bank}, results:[...]}`.
- `GET /exceptions` => payout exceptions plus separately described unmatched bank transactions.
- `GET /payouts/{payout_id}` => payout, status, matching evidence, reason.
- `POST /agent/ask` with `{"question":"Why is P-003 not reconciled?"}` => `{mode,answer,tool_calls}`.

## 6. Agent/function calling (no model training)
Implement three server-side **read-only** tools: `get_summary()`, `list_exceptions()`, and `get_payout(payout_id)`.
When `OPENAI_API_KEY` is provided, use OpenAI SDK tool/function calling to let an LLM choose these tools, run server-side tools, and summarize returned structured results. Limit turns/tool calls, enforce valid payout IDs and tool names, handle API failures gracefully, and never hallucinate a bank match.
When no API key is provided, `/agent/ask` must still work in an explicit `demo-mode` with deterministic/template-backed explanations. Do not call this a genuine LLM agent.
Important: signing into Codex with a ChatGPT account **does not itself give the sample app an OpenAI API key**. Optional API usage can incur charges. Never embed or commit API keys.

## 7. Tests and evaluation
At minimum:
- reference normalization
- matched same-day and two-day-later cases
- amount mismatch
- duplicate candidates are not automatically resolved
- bank deposit outside accepted window
- currency mismatch
- missing payout / unmatched bank
- unknown payout API path and normal API route response
- `/agent/ask` demo fallback works without an API key

Use `scripts/evaluate.py` to compare every ledger status and candidate ID set against `data/expected_labels.csv`; print 12/12 correct when rules implemented and summarize each outcome. This is fixture agreement, not generalization performance.

## 8. Browser demo and presentation
A single HTML page is enough: title and explanation, cards for Total/Matched/Exceptions/Unmatched bank, a table with colored statuses, an input to inspect `P-003` via the query endpoint, and a clear `demo-mode` vs `live-llm` indicator. Basic clean CSS only. No external JS libraries/CDN necessary.

One-minute demo script:
1. Open `/`: show deterministic overview (12 payouts, 7 matched, 5 exceptions, 1 stray bank row).
2. Click `P-003`: show expected vs actual amounts and mismatch reason.
3. Click `P-005`: show two candidates and explain why automatic selection is unsafe.
4. Ask: `Why is P-003 not reconciled?` See read-only tool result; clearly identify demo/live mode.
5. Show `/docs`, `pytest -q`, and the GitHub README.

## 9. Suggested simple repo layout
```
app/
  __init__.py
  main.py             # FastAPI app
  reconciliation.py   # pure deterministic matching rules
  models.py            # typed output contracts (optional)
  agent.py             # read-only tools; optional LLM path
static/
  index.html
scripts/
  evaluate.py
  demo.sh              # optional

tests/
  test_reconciliation.py
  test_api.py

data/
  ledger.csv
  bank.csv
  expected_labels.csv
.github/workflows/ci.yml  # optional
AGENTS.md
PROJECT_SPEC.md
README.md
pyproject.toml
.gitignore
Dockerfile
.env.example
```

## 10. Explicit non-goals / guardrails
No training, no RAG, no authentication, no real databases, no card networks or banking APIs, no payment execution, no user PII, no background queue, no data warehouse, no production security claims. Keep all financial amounts in integer minor units. Public GitHub publication is gated on human review of staged files for secrets.

## 11. Prioritized acceptance order
**P0:** deterministic reconcile engine, CSV fixtures, tests, `GET /reconcile`, `/exceptions`, `/payouts/{id}`, readable README.
**P1:** tiny webpage, evaluation script, request logging, `/agent/ask` with truthful demo mode, authentic optional tool-calling path.
**P2:** Dockerfile, GitHub Actions, polished demo video/script, optional hosted deployment. If time is running out, cut P2 before weakening P0/P1 correctness.
