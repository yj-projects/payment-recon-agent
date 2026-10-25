# 75-second portfolio demonstration

Use synthetic data and **Demo Mode** for the reproducible walkthrough. Capture
screenshots/video manually if desired; no recording permissions are required.

## Prepare

From the project root, activate `.venv`, run the tests/evaluation, and keep the
terminal output ready:

```bash
source .venv/bin/activate
pytest -q
python scripts/evaluate.py
unset OPENAI_API_KEY
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

Open `http://127.0.0.1:8000/`. If a server already occupies port 8000, stop that
server before starting another. Keep a second terminal available for test output.

## Script

| Time | Action | Suggested narration |
|---|---|---|
| 0–10s | Show summary and synthetic-data label | “A marketplace needs to verify that expected payouts reached its bank. This synthetic batch has 12 payouts: 7 matched, 5 exceptions, and 1 separate unmatched bank transaction.” |
| 10–25s | Inspect P-003 and expand structured evidence | “The deterministic engine finds 120000 expected versus 119950 actual cents: a negative 50-cent difference. It preserves source IDs and the rule behind the decision.” |
| 25–40s | Select P-005 | “Two bank rows share this payout’s merchant and normalized reference. Both candidates are retained; the system reports ambiguity instead of selecting one.” |
| 40–55s | Ask “Why did P-003 fail to reconcile?”; show mode, tool and evidence | “This is deterministic Demo Mode, not an LLM call. It executes the registered read-only get_payout tool over the same snapshot. The live adapter uses Responses function calling, verified here with mocked SDK exchanges.” |
| 55–65s | Ask “Execute a refund for P-003.” | “The request is refused, no tool runs, and no financial action is possible. The tool registry contains only read capabilities.” |
| 65–75s | Show B-999 separately, then prepared test/evaluation output | “B-999 has no ledger association. All 126 tests pass, and all 12 golden statuses and candidate sets agree. This verifies the fixture, not real-world model accuracy. No paid provider call has been tested.” |

## Follow-up questions

- Show `/docs` for typed contracts and the `X-Request-ID` response header.
- Point to JSON `tool_execution`, `agent_request`, and `http_request` events with
  the same request ID. Do not show environment variables or credentials.
- Explain that the engine owns financial decisions; the model selects tools and
  explains evidence. A model narrative may be wrong, so evidence stays visible.
- State that mocked SDK tests verify dispatch/protocol, not live model behavior.
- Ask “Why is P-005 marked as duplicate candidates?” to display both candidate IDs.

Do not imply that Demo Mode is model reasoning or that 12/12 measures
generalization. Rerun tests before recording a later version and update the
narrated test count if the suite changes.
