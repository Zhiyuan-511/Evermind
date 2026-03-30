RESILIENT MEDIA DELIVERY

Never ship a visually important image slot without a failure plan.

Media rules:
- For location-specific, travel, cultural, or historical pages, use only user-provided or analyst-verified image URLs. Do not invent "probably right" remote photo URLs.
- If exact photo confidence is low, replace the slot with a finished CSS/SVG composition and a clear location caption instead of a wrong photo.
- Content images must use real `<img>` elements inside a wrapper; avoid putting important media only in `background-image`.
- Default image contract:
  - `loading="lazy"` for below-the-fold media
  - `decoding="async"`
  - `referrerpolicy="no-referrer"` for remote hosts
  - explicit `width`/`height` or `aspect-ratio`
  - `object-fit: cover`
- Add an `onerror` recovery path for remote images that swaps to a styled fallback class, inline SVG, or local gradient composition.
- Keep hero media and card media on separate size budgets. Do not reuse a huge hero asset for every card.

Failure handling:
- Broken remote image must degrade into a designed panel, not an empty hole.
- Wrong-topic image is worse than no image. Prefer designed fallback over semantic mismatch.
- Avatar, thumbnail, and supporting gallery assets should never stretch or collapse the layout.

Before finishing, verify:
- every visible image has matching `alt` text and nearby labels
- there are no empty `src`, fake local paths, or dead remote placeholders
- the page stays credible when 1-2 remote media requests fail
