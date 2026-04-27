"""
Evermind v7.1g — Accuracy-Critical Asset Prefetcher

Detects when a goal needs authoritative imagery (sign language, anatomy,
flags, etc) and pre-downloads ALL required images via httpx BEFORE the
orchestrator dispatches builders. Eliminates LLM "invent SVG" failures.

The prefetcher is goal-aware:
- Goal mentions ASL/sign-language → fetch 26 letter SVGs from Wikimedia
- Goal mentions flags + countries → fetch country flags from Wikimedia
- Goal mentions anatomy → could fetch from anatomical sources

For ASL specifically, the Wikimedia upload URLs follow a deterministic hash
pattern computable from the filename (MD5 hex prefix). We resolve via the
File: page redirect path.

Output: writes into <output_dir>/frontend/public/assets/<domain>/<id>.<ext>
+ <output_dir>/shared/<domain>-manifest.json with verified entries.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("evermind.asset_prefetcher")

# Hard-coded verified Wikimedia URLs for ASL fingerspelling alphabet.
# Built from commons.wikimedia.org/wiki/File:Sign_language_<L>.svg lookups.
# These are PD/CC images from wpclipart, hand-illustrated, consistent style.
_ASL_LETTER_URLS: Dict[str, str] = {
    "A": "https://upload.wikimedia.org/wikipedia/commons/2/27/Sign_language_A.svg",
    "B": "https://upload.wikimedia.org/wikipedia/commons/d/d6/Sign_language_B.svg",
    "C": "https://upload.wikimedia.org/wikipedia/commons/0/02/Sign_language_C.svg",
    "D": "https://upload.wikimedia.org/wikipedia/commons/2/2a/Sign_language_D.svg",
    "E": "https://upload.wikimedia.org/wikipedia/commons/0/06/Sign_language_E.svg",
    "F": "https://upload.wikimedia.org/wikipedia/commons/0/02/Sign_language_F.svg",
    "G": "https://upload.wikimedia.org/wikipedia/commons/2/22/Sign_language_G.svg",
    "H": "https://upload.wikimedia.org/wikipedia/commons/c/c1/Sign_language_H.svg",
    "I": "https://upload.wikimedia.org/wikipedia/commons/4/45/Sign_language_I.svg",
    "J": "https://upload.wikimedia.org/wikipedia/commons/3/35/Sign_language_J.svg",
    "K": "https://upload.wikimedia.org/wikipedia/commons/8/8c/Sign_language_K.svg",
    "L": "https://upload.wikimedia.org/wikipedia/commons/9/93/Sign_language_L.svg",
    "M": "https://upload.wikimedia.org/wikipedia/commons/0/0a/Sign_language_M.svg",
    "N": "https://upload.wikimedia.org/wikipedia/commons/4/4a/Sign_language_N.svg",
    "O": "https://upload.wikimedia.org/wikipedia/commons/0/04/Sign_language_O.svg",
    "P": "https://upload.wikimedia.org/wikipedia/commons/3/35/Sign_language_P.svg",
    "Q": "https://upload.wikimedia.org/wikipedia/commons/c/c2/Sign_language_Q.svg",
    "R": "https://upload.wikimedia.org/wikipedia/commons/d/d6/Sign_language_R.svg",
    "S": "https://upload.wikimedia.org/wikipedia/commons/8/89/Sign_language_S.svg",
    "T": "https://upload.wikimedia.org/wikipedia/commons/5/56/Sign_language_T.svg",
    "U": "https://upload.wikimedia.org/wikipedia/commons/0/05/Sign_language_U.svg",
    "V": "https://upload.wikimedia.org/wikipedia/commons/c/c4/Sign_language_V.svg",
    "W": "https://upload.wikimedia.org/wikipedia/commons/0/0d/Sign_language_W.svg",
    "X": "https://upload.wikimedia.org/wikipedia/commons/7/7b/Sign_language_X.svg",
    "Y": "https://upload.wikimedia.org/wikipedia/commons/c/cd/Sign_language_Y.svg",
    "Z": "https://upload.wikimedia.org/wikipedia/commons/a/ab/Sign_language_Z.svg",
}

_FALLBACK_RESOLVE_PATTERN = (
    "https://commons.wikimedia.org/wiki/File:Sign_language_{letter}.svg"
)


def detect_accuracy_critical_domain(goal: str) -> Optional[str]:
    """Returns domain name if the goal needs authoritative imagery."""
    g = goal.lower()
    sign_language_hints = ("sign language", "asl", "fingerspelling", "手语", "手势")
    if any(h in g for h in sign_language_hints):
        return "asl_alphabet"
    # Future: add flags, anatomy, etc.
    return None


async def _fetch_one(client, url: str, timeout: float = 20.0,
                      max_retries: int = 3) -> Optional[bytes]:
    """Fetch with 429 backoff + transient retry."""
    for attempt in range(max_retries):
        try:
            r = await client.get(
                url, timeout=timeout, follow_redirects=True,
                headers={
                    "User-Agent": "Evermind/7.1g (https://github.com/evermind; asset-prefetcher)",
                    "Accept": "image/svg+xml,image/*;q=0.8,*/*;q=0.5",
                },
            )
            if r.status_code == 429:
                # Honor retry-after; cap at 30s
                retry_after = float(r.headers.get("retry-after", "5"))
                wait = min(retry_after, 30.0)
                logger.info("Wikimedia 429 on %s; sleeping %.1fs (attempt %d)", url, wait, attempt + 1)
                await asyncio.sleep(wait)
                continue
            if r.status_code != 200:
                logger.debug("Prefetch %s → %d", url, r.status_code)
                return None
            body = r.content
            if not body or len(body) < 200:
                return None
            return body
        except Exception as e:
            logger.debug("Prefetch %s failed (attempt %d): %s", url, attempt + 1, e)
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
    return None


async def _resolve_via_file_page(client, letter: str) -> Optional[str]:
    """Resolve canonical upload URL via Wikimedia MediaWiki API.
    Returns clean JSON with imageinfo.url — far more reliable than HTML scraping.
    """
    api_url = (
        "https://commons.wikimedia.org/w/api.php"
        f"?action=query&titles=File:Sign_language_{letter}.svg"
        "&prop=imageinfo&iiprop=url&format=json"
    )
    try:
        r = await client.get(
            api_url,
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": "Evermind/7.1g (asset-prefetcher)"},
        )
        if r.status_code != 200:
            return None
        data = r.json()
        pages = (data.get("query") or {}).get("pages") or {}
        for page in pages.values():
            imageinfo = page.get("imageinfo") or []
            if imageinfo and imageinfo[0].get("url"):
                return imageinfo[0]["url"]
    except Exception as e:
        logger.debug("API resolve failed for %s: %s", letter, e)
    return None


def _is_real_svg(body: bytes) -> bool:
    """Quick magic-byte check: SVG should start with <svg or <?xml."""
    head = body[:200].lower()
    if b"<svg" in head or b"<?xml" in head:
        # Also confirm it's not an HTML 404 page
        if b"<html" not in head and b"<!doctype html" not in head:
            return True
    return False


async def prefetch_asl_alphabet(output_dir: Path) -> Dict[str, Any]:
    """Download all 26 ASL letter SVGs to <output_dir>/frontend/public/assets/asl/.
    Also writes <output_dir>/shared/asl_alphabet-manifest.json. Returns report.

    Strategy: ALWAYS resolve via the File: page (most reliable). Hard-coded
    URLs are seeded but treated as fallback if File: page lookup fails.
    """
    try:
        import httpx
    except ImportError:
        logger.warning("httpx not available; skipping prefetch")
        return {"available": False, "reason": "httpx_missing"}

    asset_dir = output_dir / "frontend" / "public" / "assets" / "asl"
    asset_dir.mkdir(parents=True, exist_ok=True)
    shared_dir = output_dir / "shared"
    shared_dir.mkdir(parents=True, exist_ok=True)

    items: List[Dict[str, Any]] = []
    fetched = 0
    failed = 0

    async with httpx.AsyncClient(http2=False, timeout=30.0) as client:
        # Wikimedia rate-limits aggressively. Run sequentially with 0.7s
        # spacing — 26 letters × ~3s/each = ~80s total. Worth the wait.
        semaphore = asyncio.Semaphore(1)

        async def _do_letter(letter: str) -> None:
            nonlocal fetched, failed
            async with semaphore:
                # Strategy A: File: page lookup (most reliable). Retry up to 2 times.
                resolved_url: Optional[str] = None
                for _attempt in range(2):
                    resolved_url = await _resolve_via_file_page(client, letter)
                    if resolved_url:
                        break
                    await asyncio.sleep(0.5)
                source_url = resolved_url
                body: Optional[bytes] = None
                if resolved_url:
                    for _attempt in range(2):
                        body = await _fetch_one(client, resolved_url, timeout=30)
                        if body and _is_real_svg(body):
                            break
                        await asyncio.sleep(0.5)
                # Strategy B: hard-coded fallback URL
                if not body or not _is_real_svg(body):
                    fallback = _ASL_LETTER_URLS.get(letter)
                    if fallback:
                        body2 = await _fetch_one(client, fallback, timeout=30)
                        if body2 and _is_real_svg(body2):
                            body = body2
                            source_url = fallback
                if body and _is_real_svg(body):
                    # v7.1i (maintainer 2026-04-25): write BOTH naming conventions.
                    # Builders inconsistently reference either `A.svg` or
                    # `letter_A.svg` — if file with prompt-named convention
                    # missing, builder writes a 293-byte placeholder fake.
                    # Writing both names is cheap (16KB × 26 × 2 = 832KB) and
                    # eliminates that failure mode entirely.
                    out_path = asset_dir / f"{letter}.svg"
                    out_path.write_bytes(body)
                    alt_path = asset_dir / f"letter_{letter}.svg"
                    alt_path.write_bytes(body)
                    items.append({
                        "id": letter,
                        "local_path": f"frontend/public/assets/asl/{letter}.svg",
                        "alt_local_path": f"frontend/public/assets/asl/letter_{letter}.svg",
                        "source_url": source_url,
                        "verified": True,
                        "size_bytes": len(body),
                        "content_hint": f"ASL letter {letter} handshape",
                        "attribution": "Wikimedia Commons (PD/CC)",
                    })
                    fetched += 1
                else:
                    items.append({
                        "id": letter,
                        "local_path": None,
                        "source_url": source_url or _ASL_LETTER_URLS.get(letter),
                        "verified": False,
                        "failure_reason": "fetch_or_magic_failed",
                        "content_hint": f"ASL letter {letter} handshape",
                    })
                    failed += 1
                # Polite spacing between requests to avoid 429
                await asyncio.sleep(0.7)

        await asyncio.gather(*[
            _do_letter(letter) for letter in _ASL_LETTER_URLS.keys()
        ])

    # Sort items A-Z
    items.sort(key=lambda x: x["id"])

    manifest = {
        "domain": "asl_alphabet",
        "fetched_at": int(time.time() * 1000),
        "total_attempted": len(_ASL_LETTER_URLS),
        "total_verified": fetched,
        "total_failed": failed,
        "items": items,
    }

    manifest_path = shared_dir / "asl_alphabet-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    logger.info(
        "[asset_prefetcher] ASL alphabet: %d/%d verified, manifest at %s",
        fetched, len(_ASL_LETTER_URLS), manifest_path,
    )
    return manifest


async def prefetch_for_goal(goal: str, output_dir: Path) -> Optional[Dict[str, Any]]:
    """Top-level: detect domain, dispatch the right prefetcher.
    Returns manifest dict on success, None if no prefetch needed.
    """
    domain = detect_accuracy_critical_domain(goal)
    if not domain:
        return None
    if domain == "asl_alphabet":
        return await prefetch_asl_alphabet(output_dir)
    return None


def verify_assets_intact_sync(goal: str, output_dir: Path) -> Dict[str, Any]:
    """Sync wrapper around verify_assets_intact for use from non-async code paths."""
    try:
        return asyncio.run(verify_assets_intact(goal, output_dir))
    except RuntimeError:
        # Inside an event loop already — schedule and wait via a worker thread.
        import threading
        result_box: Dict[str, Any] = {}
        def _run():
            new_loop = asyncio.new_event_loop()
            try:
                result_box["r"] = new_loop.run_until_complete(verify_assets_intact(goal, output_dir))
            finally:
                new_loop.close()
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=180)
        return result_box.get("r", {"checked": False, "reason": "timeout"})


async def verify_assets_intact(goal: str, output_dir: Path) -> Dict[str, Any]:
    """v7.1i (maintainer 2026-04-25): post-run integrity check.
    Builders/patchers sometimes delete prefetched assets. Re-verify the
    expected asset count is on disk; re-fetch any missing.

    Returns:
      {"checked": True, "domain": "...", "before": N, "after": N, "refetched": M}
      OR {"checked": False, "reason": "..."}
    """
    domain = detect_accuracy_critical_domain(goal)
    if not domain:
        return {"checked": False, "reason": "no_accuracy_critical_domain"}

    if domain == "asl_alphabet":
        asset_dir = output_dir / "frontend" / "public" / "assets" / "asl"
        # Builders sometimes move assets to root or rename, so check 3 candidate paths
        candidates = [
            output_dir / "frontend" / "public" / "assets" / "asl",
            output_dir / "assets" / "asl",
            output_dir / "frontend" / "public" / "assets" / "signs",
            output_dir / "assets" / "signs",
        ]
        existing_count = 0
        existing_dir = None
        for d in candidates:
            if d.is_dir():
                count = sum(1 for f in d.glob("*.svg") if f.is_file() and f.stat().st_size > 1000)
                if count > existing_count:
                    existing_count = count
                    existing_dir = d
        before = existing_count
        if before >= 24:
            logger.info(
                "[v7.1i verify_assets] ASL: %d/26 SVGs present at %s — OK",
                before, existing_dir,
            )
            return {"checked": True, "domain": domain, "before": before, "after": before, "refetched": 0}
        # Re-fetch
        logger.warning(
            "[v7.1i verify_assets] ASL alphabet only %d/26 SVGs present — RE-FETCHING",
            before,
        )
        result = await prefetch_asl_alphabet(output_dir)
        after = result.get("total_verified", 0) if isinstance(result, dict) else 0
        return {
            "checked": True, "domain": domain,
            "before": before, "after": after,
            "refetched": after - before,
        }
    return {"checked": False, "reason": f"no_handler_for_{domain}"}


# Entrypoint for ad-hoc runs / tests
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/evermind_output")
    goal = sys.argv[2] if len(sys.argv) > 2 else "build a sign language teaching site"
    rep = asyncio.run(prefetch_for_goal(goal, out))
    print(json.dumps(rep, indent=2, ensure_ascii=False) if rep else "no prefetch")
