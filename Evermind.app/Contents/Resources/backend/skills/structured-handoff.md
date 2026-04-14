---
name: structured-handoff
description: Structured delivery envelope for machine-readable node-to-node handoff
metadata:
  evermind:
    applies_to: ["*"]
---

# Structured Handoff Protocol

## Output Envelope (Mandatory)
Your output MUST end with a machine-readable handoff envelope. This enables downstream nodes to understand what you produced, what issues remain, and what to focus on.

Format (JSON inside XML tags):
```
<handoff_envelope>
{
  "confidence": 8,
  "decisions": [
    "Chose Three.js over Babylon.js: lighter bundle, better community support",
    "Used CSS Grid over Flexbox for layout: 2D grid requirements"
  ],
  "deliverables": [
    "index.html (main entry, 45KB)",
    "js/gameEngine.js (core loop, 12KB)",
    "css/hud.css (heads-up display styles)"
  ],
  "open_questions": [
    "Asset loading order not verified under slow network",
    "Mobile touch controls untested"
  ],
  "known_issues": [
    "Enemy AI pathfinding occasionally clips through walls",
    "Audio context requires user gesture on Safari"
  ],
  "next_node_guidance": "Merger should wire gameEngine.js into index.html via script tag before the closing body tag. Watch for CSS class name collisions in .hud-* namespace."
}
</handoff_envelope>
```

## Rules
- confidence: integer 1-10, your self-assessed overall quality
- decisions: list your major choices with 1-sentence rationale each
- deliverables: list every file/artifact you produced with size or purpose
- open_questions: things you are unsure about that downstream should verify
- known_issues: bugs or limitations you are aware of but did not fix
- next_node_guidance: specific instructions for the next node in the pipeline
