REVIEW REMEDIATION GATE

Use this skill for reviewer/tester whenever a website must be either genuinely shippable or explicitly sent back for correction.

Hard reject triggers:
- wrong-topic images, broken image slots, or hero/card media that fail to load cleanly
- giant awkward images that dominate the viewport without art direction
- navigation that wraps badly, differs across pages, or points to missing routes
- pages that rely on flat black/white backgrounds with no surface hierarchy
- placeholder-grade layout, typography, or motion even if the code technically works

Required reviewer behavior:
- Preserve what is already good. Identify the strongest sections that must survive the rework.
- Output a concrete remediation brief, not vague taste feedback.
- Separate findings into:
  - blocking defects
  - exact builder changes
  - acceptance criteria for the next review
- If the build regressed from a previously stronger preview, explicitly request rollback to the stable version before further edits.
- Do not soft-pass a website whose commercial quality is still below the bar.

Mandatory remediation topics for websites:
- navigation contract and reachable pages
- background/surface palette rhythm
- image correctness, placement, aspect ratio, and fallback behavior
- typography spacing and content framing

Before approving, verify:
- reviewer/tester evidence actually covers top, middle, bottom, and secondary pages
- the next builder attempt can act directly on the remediation brief without re-research
