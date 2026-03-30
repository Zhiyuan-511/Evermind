EDITORIAL LAYOUT COMPOSER

Use this skill when a site needs stronger typography rhythm, better media placement, and navigation that does not fall apart across pages.

Layout rules:
- Pick one navigation contract for the whole site and reuse it verbatim across every route: same brand, same links, same class names, same mobile toggle behavior.
- Treat hero media as art-directed framing, not a giant raw image dump. Constrain it with aspect ratio, crop, overlay, caption, and surrounding whitespace.
- Alternate layout densities: full-bleed hero, split-content editorial row, compact card grid, then a calmer text-led section.
- Prefer mixed editorial patterns over identical repeated cards: feature split, staggered media columns, inset quote/testimonial, timeline strip, gallery band.
- Large images must be earned. Do not let a single image dominate the viewport unless it is the hero or a deliberate showcase panel.
- Card media should stay inside designed frames with `overflow:hidden`, `aspect-ratio`, and a consistent radius system.

Navigation rules:
- Desktop nav must not wrap awkwardly. If the link count is high, reduce label length, tighten gap, or switch to a compact grouped pattern.
- Mobile nav needs an explicit toggle and open/close state. Do not rely on hidden overflow or broken class names.
- Active-page styling must be consistent across all routes.

Do not:
- mix `.nav-links`, `.nav-menu`, `.main-nav` structures randomly across different pages
- paste a full-width image between text blocks with no framing
- use the same layout composition on every page

Before finishing, verify:
- no route has broken nav alignment or a different link set
- image sizes feel intentional instead of oversized
- content hierarchy still reads well when images are disabled
