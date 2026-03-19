# Codex Round 4 Review (Stability + Cost Guardrails)

## What was fixed

### 1) Dynamic output budget for OpenAI-compatible path
- File: `backend/ai_bridge.py`
- Added per-node policy:
  - `builder`: default `8192` tokens, `180s` timeout
  - others: default `4096` tokens, `120s` timeout
- Configurable via env:
  - `EVERMIND_BUILDER_MAX_TOKENS`
  - `EVERMIND_DEFAULT_MAX_TOKENS`
  - `EVERMIND_BUILDER_TIMEOUT_SEC`
  - `EVERMIND_DEFAULT_TIMEOUT_SEC`

Reason: avoid forcing `16384` on every task (cost/latency spike), while still preventing builder truncation.

### 2) Truncation auto-continue
- File: `backend/ai_bridge.py`
- If finish reason is `length`, backend now asks model to continue from last point.
- Controlled by `EVERMIND_MAX_CONTINUATIONS` (default `2`).

Reason: makes HTML generation resilient even when output is long or provider output caps change.

### 3) Safer message serialization in tool loop
- File: `backend/ai_bridge.py`
- Assistant tool-call message is now serialized to plain dict before next request.

Reason: avoids SDK object-shape mismatch risk across versions.

### 4) Usage accounting improved
- File: `backend/ai_bridge.py`
- Usage now merges across continuation/tool-call turns instead of only returning last turn usage.

### 5) Tester fail parsing made stricter
- File: `backend/orchestrator.py`
- `_parse_test_result()` now prioritizes strong failure markers (e.g., missing head/style, truncated HTML) before pass heuristics.
- Cloud-only deployment warnings are still ignored as non-fatal.

Reason: prevent false pass when output contains both “created successfully” and real HTML structure errors.

## Added tests

- `backend/tests/test_ai_bridge.py`
  - `TestNodeTokenAndTimeoutPolicy`
- `backend/tests/test_orchestrator.py`
  - `TestParseTestResult`

## Validation run

1. `python3 -m unittest -q tests/test_ai_bridge.py tests/test_orchestrator.py` -> PASS  
2. Real Kimi builder call (`gpt-5.4` fallback to `kimi-coding`) -> complete HTML returned  
3. Forced low token budget (`EVERMIND_BUILDER_MAX_TOKENS=1024`) -> continuation event triggered and response still completed  
4. `bash ~/Desktop/update_evermind.sh` -> PASS (6/6)

## Suggested next step for Antigravity

Use this runtime profile first:

```bash
export EVERMIND_BUILDER_MAX_TOKENS=8192
export EVERMIND_MAX_CONTINUATIONS=2
```

Then run one acceptance case:
- Prompt: `创建一个简单的个人网站`
- Expect:
  - complete HTML code block (`<!DOCTYPE html>` ... `</html>`)
  - tester status `pass`
  - preview link emitted

