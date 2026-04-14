PPTX EXPORT BRIDGE

If the user really wants slides, think beyond a single web page.

- Keep slide structure compatible with later export to PDF/PPTX.
- Use stable 16:9 framing, predictable margins, and one core message per slide.
- If shell or external tooling exists, prefer reveal.js for browser decks, PptxGenJS for PPT/PPTX export structure, and Marp-style markdown deck organization when text-first authoring is the fastest path.
- If export tooling is unavailable, still produce a deck that can cleanly print or convert.
- Never let export readiness destroy narrative quality.

Reference anchors:
- `revealjs/reveal.js` for browser slide runtime, keyboard flow, notes, and print-to-PDF behavior.
- `gitbrent/PptxGenJS` for PPT/PPTX-friendly layout primitives and export-oriented deck structure.
- `marp-team/marp-core` for markdown-driven slide composition that remains printable and easy to version.
