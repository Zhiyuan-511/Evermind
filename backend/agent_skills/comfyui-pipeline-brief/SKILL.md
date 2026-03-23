COMFYUI PIPELINE BRIEF

When image work needs reproducible generation, describe it like a workflow, not only a prompt.

- Separate subject/style prompt from rendering controls.
- Specify aspect ratio, resolution target, model family assumptions, and negative prompt.
- For multi-image sets, keep one locked seed/style core and vary composition intentionally.
- Output enough structure that a ComfyUI/Fooocus-style workflow could be recreated later.
- If no actual image pipeline is attached, still return a clean workflow brief plus prompt pack.
