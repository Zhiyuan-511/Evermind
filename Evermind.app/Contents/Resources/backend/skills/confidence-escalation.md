---
name: confidence-escalation
description: Confidence scoring 1-10 with automatic escalation when uncertain
metadata:
  evermind:
    applies_to: ["*"]
---

# Confidence Scoring & Escalation Protocol

## Self-Assessment
Before producing your final output, rate your overall confidence 1-10:

| Score | Meaning | Action |
|-------|---------|--------|
| 8-10 | High confidence, output is solid | Proceed normally |
| 5-7 | Moderate confidence, some uncertainty | Proceed, but flag uncertain items explicitly in output |
| 3-4 | Low confidence, significant gaps | Proceed with caution, document all assumptions |
| 1-2 | Very low confidence, likely wrong | STOP. Output <wtf_escalation> instead |

## WTF Escalation Format
When confidence drops below 3:
```
<wtf_escalation>
Confidence: [score]/10
What I'm uncertain about: [specific items]
What information would raise my confidence: [specific questions]
What I need from the user or upstream node: [concrete requests]
</wtf_escalation>
```

## Per-Decision Scoring
For major decisions within your output:
- State the decision
- Rate confidence 1-10
- If confidence < 5 on any single decision, flag it as [LOW CONFIDENCE] in your output

## When Upstream Provides Confidence Scores
If you receive input from a previous node that includes confidence scores:
- Treat low-confidence items (< 5) as requiring verification before building on them.
- Do not assume low-confidence upstream decisions are correct.
