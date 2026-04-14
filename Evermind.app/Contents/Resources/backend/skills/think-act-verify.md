---
name: think-act-verify
description: Think→Plan→Act→Verify execution cycle for disciplined implementation
metadata:
  evermind:
    applies_to: [builder, merger, debugger]
---

# Think → Act → Verify Protocol

## Before Every Action
State in ONE sentence what you are about to do and why.

## Read Before Write
- Before writing any file, read its current contents first with file_ops read.
- Before editing code, verify the current state matches your expectation.
- Never overwrite unseen content. Never guess what a file contains.

## Execution Discipline
- Independent operations: execute in parallel for efficiency.
- Dependent operations: execute strictly in sequence, verify each step.
- On tool failure: read current state first, diagnose the error, then retry with a corrected approach. Do NOT blindly retry the same action.

## Feature Completion Verification
Before declaring your work done:
1. Count features_requested vs features_implemented.
2. If ratio < 0.8 — you are NOT done. Continue implementing.
3. Trace every requirement from the brief to a specific line in your output.
4. Check: zero TODO, zero FIXME, zero placeholder, zero stub.

## Output Quality Gate
- Every file must be syntactically valid and runnable.
- Every function must have a clear purpose — no dead code.
- Prefer new files over overwriting unless the task explicitly requires modification.
