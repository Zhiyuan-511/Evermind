---
name: error-recovery
description: Failure mode learning and error prevention protocol
metadata:
  evermind:
    applies_to: [builder, merger, debugger]
---

# Error Recovery Protocol

## Iron Laws (from production experience)

1. **Check upstream issues first**: Before starting work, read any known_issues from upstream nodes. Do not repeat mistakes that earlier nodes already identified.

2. **Same error twice = change strategy**: If you encounter the same error a second time, you MUST change your approach before the third attempt. Do not retry the identical action expecting different results.

3. **Token discipline**: Do not output unnecessary commentary, explanations, or verbose logging. Every token in your output should be functional code or essential documentation.

4. **Read before write**: Never overwrite a file you haven't read. Never assume file contents. Always verify current state before modification.

5. **Fail fast, fail informatively**: If something is broken, report the specific error (file, line, symptom) rather than a vague "something went wrong."

## Error Reporting Format
When encountering an error during execution:
```
Trigger: [what you were doing when the error occurred]
Root cause: [your best diagnosis of WHY it failed]
Solution: [what you did or recommend doing to fix it]
Confidence: [1-10 how sure you are about the root cause]
```

## Recovery Strategy
1. On first failure: diagnose root cause, apply targeted fix.
2. On second failure with same symptom: change approach entirely.
3. On third failure: escalate via <wtf_escalation> — do not keep retrying.

## Known Failure Patterns
- Writing files without reading first → stale content overwrite
- Generating HTML without closing tags → parser failures downstream
- Using CommonJS in browser code → runtime module errors
- Exceeding context window → truncated output with broken syntax
