---
name: decision-audit
description: Decision audit trail with evidence chain for transparent reasoning
metadata:
  evermind:
    applies_to: [planner, reviewer, analyst]
---

# Decision Audit Protocol

## Every Major Decision Must Include
1. **What** you decided (the choice itself)
2. **Why** you chose it (1-2 sentence rationale)
3. **What alternatives** you considered (at least 1 other option)
4. **Confidence** score (1-10)
5. **Evidence** supporting the decision (URL, code reference, metric)

## Format
```
Decision: [what you chose]
Rationale: [why, in 1-2 sentences]
Alternatives considered: [what else you looked at]
Confidence: [1-10]
Evidence: [specific reference supporting the choice]
```

## When to Audit
- Architecture or framework selection
- Library or dependency choices
- Design pattern selection
- Score assignments (for reviewers)
- Risk assessments
- Any choice that affects downstream nodes

## For Reviewers
When assigning scores to dimensions (layout, color, typography, etc.):
- Each score must cite specific evidence (CSS selector, pixel value, screenshot observation)
- Scores without evidence are invalid
- Low scores (< 5) must include specific improvement recommendations

## Audit Trail Preservation
Your decisions are recorded in the handoff envelope for downstream audit.
Include your decision audit in the handoff_envelope "decisions" array.
