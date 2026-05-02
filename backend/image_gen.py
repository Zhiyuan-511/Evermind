"""
Evermind — Image Generation Adapter (v6.1.15)

Unified adapter for real image generation across 6 providers:
- tongyi (阿里通义万相)       -> dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis
- doubao-image (字节豆包)     -> ark.cn-beijing.volces.com/api/v3/images/generations
- wenxin (百度文心一格)       -> aip.baidubce.com (legacy sig) — optional
- seedream (豆包 Seedream 4)  -> ark.cn-beijing.volces.com with model=doubao-seedream-4-0-*
- flux-fal (FLUX.1 schnell)   -> fal.run/fal-ai/flux/schnell
- dalle-3 / openai-compat     -> any OpenAI-compatible /v1/images/generations

Design rules:
1. No-key → return None (imagegen node degrades to SVG placeholders cleanly)
2. Hard timeout 30s per call — never block the pipeline
3. Save to /tmp/evermind_output/assets/<slug>.webp for deterministic path
4. Cache by (provider, prompt_hash, size) to dedupe redundant calls
5. rembg + PIL smart-crop is OPTIONAL (gated on config.auto_crop)

Usage:
    gen = ImageGen(config)
    if not gen.available:
        return None  # degrade to SVG

    result = await gen.generate(
        prompt="A cozy artisan pottery studio, warm morning light, minimalist composition",
        output_slug="hero-feature",
        size="1536x1024",
    )
    # result = {"status":"ok","files":{"16:9":"./assets/hero-feature_16x9.webp", ...}}
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import httpx  # type: ignore
except Exception:
    httpx = None  # type: ignore

try:
    from PIL import Image  # type: ignore
except Exception:
    Image = None  # type: ignore

logger = logging.getLogger("evermind.image_gen")

_DEFAULT_TIMEOUT = float(os.getenv("EVERMIND_IMAGE_GEN_TIMEOUT_SEC", "45"))
_DEFAULT_OUTPUT_DIR = os.getenv("EVERMIND_IMAGE_GEN_OUTPUT_DIR", "/tmp/evermind_output/assets")

# Cache: (provider, prompt_hash, size) -> absolute path
_IMAGE_CACHE: Dict[str, str] = {}


class ImageGen:
    """Uniform image generation adapter.

    Hard-typed errors return `None`. Never raises on missing config.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = (config or {}).get("image_generation") if isinstance(config, dict) else {}
        cfg = cfg if isinstance(cfg, dict) else {}
        self.provider: str = str(cfg.get("provider") or "").strip().lower()
        self.api_key: str = str(cfg.get("api_key") or "").strip()
        self.base_url: str = str(cfg.get("base_url") or "").strip().rstrip("/")
        self.default_size: str = str(cfg.get("default_size") or "1024x1024").strip()
        self.default_model: str = str(cfg.get("default_model") or "").strip()
        self.max_images: int = max(1, min(int(cfg.get("max_images_per_run") or 10), 40))
        self.auto_crop: bool = bool(cfg.get("auto_crop", True))
        self._count_this_run = 0

    @property
    def available(self) -> bool:
        """True only if provider + key are set and httpx is importable."""
        if not httpx:
            return False
        if not self.provider:
            return False
        if not self.api_key:
            return False
        return True

    def _prompt_hash(self, prompt: str, size: str) -> str:
        return hashlib.sha1(f"{self.provider}|{prompt}|{size}".encode("utf-8")).hexdigest()[:12]

    async def generate(
        self,
        prompt: str,
        output_slug: str,
        size: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Generate an image. Returns dict with paths on success, None on any failure.

        The returned dict shape:
          {
            "status": "ok",
            "files": {"raw": "/abs/path.webp", "16x9": "...", "1x1": "...", ...},
            "prompt": <echo>,
            "provider": <name>,
          }
        """
        if not self.available:
            return None
        if self._count_this_run >= self.max_images:
            logger.info("ImageGen quota reached this run (%s)", self.max_images)
            return None

        prompt = str(prompt or "").strip()
        if len(prompt) < 3:
            return None

        size = str(size or self.default_size or "1024x1024").strip()
        cache_key = self._prompt_hash(prompt, size)
        if cache_key in _IMAGE_CACHE and os.path.exists(_IMAGE_CACHE[cache_key]):
            return {
                "status": "ok",
                "provider": self.provider,
                "files": {"raw": _IMAGE_CACHE[cache_key]},
                "prompt": prompt,
                "cached": True,
            }

        output_dir = Path(_DEFAULT_OUTPUT_DIR)
        output_dir.mkdir(parents=True, exist_ok=True)
        raw_path = output_dir / f"{output_slug}-{cache_key}.webp"

        try:
            image_bytes = await asyncio.wait_for(
                self._dispatch(prompt, size, model or self.default_model),
                timeout=_DEFAULT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("ImageGen timeout (%ss) for %s", _DEFAULT_TIMEOUT, self.provider)
            return None
        except Exception as e:
            logger.warning("ImageGen failed for %s: %s", self.provider, str(e)[:200])
            return None

        if not image_bytes:
            return None

        try:
            if Image is not None:
                img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
                img.save(str(raw_path), "WEBP", quality=88, method=6)
            else:
                raw_path.write_bytes(image_bytes)
        except Exception as e:
            logger.warning("ImageGen save failed: %s", e)
            return None

        _IMAGE_CACHE[cache_key] = str(raw_path)
        self._count_this_run += 1

        files: Dict[str, str] = {"raw": str(raw_path)}
        if self.auto_crop and Image is not None:
            try:
                crops = self._auto_crop_ratios(raw_path, output_slug, cache_key)
                files.update(crops)
            except Exception as e:
                logger.warning("ImageGen auto-crop failed: %s", e)

        logger.info(
            "ImageGen ok: provider=%s slug=%s size=%s path=%s",
            self.provider, output_slug, size, raw_path,
        )
        return {
            "status": "ok",
            "provider": self.provider,
            "files": files,
            "prompt": prompt,
            "cached": False,
        }

    # ───────────────────────── Provider dispatch ──────────────────────────

    async def _dispatch(self, prompt: str, size: str, model: str) -> Optional[bytes]:
        if self.provider == "tongyi":
            return await self._call_tongyi(prompt, size, model)
        if self.provider in ("doubao-image", "doubao", "seedream"):
            return await self._call_doubao(prompt, size, model)
        if self.provider == "wenxin":
            return await self._call_wenxin(prompt, size, model)
        if self.provider in ("flux-fal", "fal-flux"):
            return await self._call_fal_flux(prompt, size, model)
        if self.provider in ("dalle-3", "dalle3", "openai", "openai-compat"):
            return await self._call_openai_compat(prompt, size, model)
        logger.warning("Unknown image provider: %s", self.provider)
        return None

    async def _call_tongyi(self, prompt: str, size: str, model: str) -> Optional[bytes]:
        """阿里通义万相 (DashScope) — async pattern: submit → poll → fetch."""
        base = self.base_url or "https://dashscope.aliyuncs.com"
        submit_url = f"{base}/api/v1/services/aigc/text2image/image-synthesis"
        model_name = model or "wanx2.1-t2i-turbo"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }
        payload = {
            "model": model_name,
            "input": {"prompt": prompt},
            "parameters": {"size": size.replace("x", "*"), "n": 1},
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(submit_url, json=payload, headers=headers)
            if r.status_code != 200:
                logger.warning("tongyi submit %s: %s", r.status_code, r.text[:200])
                return None
            task_id = (r.json().get("output") or {}).get("task_id")
            if not task_id:
                return None
            task_url = f"{base}/api/v1/tasks/{task_id}"
            poll_headers = {"Authorization": f"Bearer {self.api_key}"}
            for _ in range(30):  # up to ~30 × 1s
                await asyncio.sleep(1.0)
                tr = await client.get(task_url, headers=poll_headers)
                if tr.status_code != 200:
                    continue
                data = tr.json().get("output") or {}
                status = str(data.get("task_status") or "").upper()
                if status == "SUCCEEDED":
                    results = data.get("results") or []
                    if results and results[0].get("url"):
                        ir = await client.get(results[0]["url"])
                        return ir.content if ir.status_code == 200 else None
                    return None
                if status in ("FAILED", "CANCELED", "UNKNOWN"):
                    logger.warning("tongyi task %s: %s", status, data.get("message", "")[:200])
                    return None
            return None

    async def _call_doubao(self, prompt: str, size: str, model: str) -> Optional[bytes]:
        """豆包/Seedream — OpenAI-shaped /api/v3/images/generations on Volcengine Ark."""
        base = self.base_url or "https://ark.cn-beijing.volces.com/api/v3"
        model_name = model or "doubao-seedream-4-0-250828"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model_name,
            "prompt": prompt,
            "size": size,
            "n": 1,
            "response_format": "url",
        }
        async with httpx.AsyncClient(timeout=45.0) as client:
            r = await client.post(f"{base}/images/generations", json=payload, headers=headers)
            if r.status_code != 200:
                logger.warning("doubao %s: %s", r.status_code, r.text[:200])
                return None
            data = r.json().get("data") or []
            if not data:
                return None
            if data[0].get("b64_json"):
                return base64.b64decode(data[0]["b64_json"])
            url = data[0].get("url")
            if url:
                ir = await client.get(url)
                return ir.content if ir.status_code == 200 else None
        return None

    async def _call_wenxin(self, prompt: str, size: str, model: str) -> Optional[bytes]:
        """百度文心一格 — simplified (requires ak/sk flow; user can alternatively
        use an OpenAI-compatible relay that fronts Wenxin)."""
        logger.info("Wenxin direct is AK/SK-based; prefer openai-compat relay. Skipping.")
        return None

    async def _call_fal_flux(self, prompt: str, size: str, model: str) -> Optional[bytes]:
        """FLUX.1 schnell via fal.ai."""
        model_path = model or "fal-ai/flux/schnell"
        url = f"https://fal.run/{model_path}"
        headers = {
            "Authorization": f"Key {self.api_key}",
            "Content-Type": "application/json",
        }
        w, _, h = size.partition("x")
        payload = {
            "prompt": prompt,
            "image_size": {"width": int(w or 1024), "height": int(h or 1024)},
            "num_inference_steps": 4,
            "num_images": 1,
            "enable_safety_checker": False,
        }
        async with httpx.AsyncClient(timeout=45.0) as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code != 200:
                logger.warning("fal-flux %s: %s", r.status_code, r.text[:200])
                return None
            data = r.json()
            images = data.get("images") or []
            if not images:
                return None
            img_url = images[0].get("url")
            if not img_url:
                return None
            ir = await client.get(img_url)
            return ir.content if ir.status_code == 200 else None

    async def _call_openai_compat(self, prompt: str, size: str, model: str) -> Optional[bytes]:
        """Generic OpenAI-compatible /v1/images/generations (DALL-E-3 shape).
        Works for Azure OpenAI, self-hosted one-api relays, etc."""
        base = self.base_url or "https://api.openai.com/v1"
        model_name = model or "dall-e-3"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model_name,
            "prompt": prompt,
            "size": size,
            "n": 1,
            "response_format": "b64_json",
        }
        async with httpx.AsyncClient(timeout=45.0) as client:
            r = await client.post(f"{base}/images/generations", json=payload, headers=headers)
            if r.status_code != 200:
                logger.warning("openai-compat image %s: %s", r.status_code, r.text[:200])
                return None
            data = r.json().get("data") or []
            if not data:
                return None
            if data[0].get("b64_json"):
                return base64.b64decode(data[0]["b64_json"])
            url = data[0].get("url")
            if url:
                ir = await client.get(url)
                return ir.content if ir.status_code == 200 else None
        return None

    # ───────────────────────── Auto-crop ──────────────────────────────────

    def _auto_crop_ratios(self, raw_path: Path, slug: str, key: str) -> Dict[str, str]:
        """Center-crop the generated image to 4 canonical aspect ratios."""
        if Image is None:
            return {}
        img = Image.open(str(raw_path)).convert("RGB")
        w, h = img.size
        ratios = {
            "16x9": (16, 9),
            "4x3": (4, 3),
            "1x1": (1, 1),
            "3x4": (3, 4),
        }
        out: Dict[str, str] = {}
        out_dir = raw_path.parent
        for ratio_name, (rw, rh) in ratios.items():
            target_ratio = rw / rh
            cur_ratio = w / max(1, h)
            if cur_ratio > target_ratio:
                # too wide — crop width
                new_w = int(round(h * target_ratio))
                left = (w - new_w) // 2
                crop = img.crop((left, 0, left + new_w, h))
            else:
                # too tall — crop height
                new_h = int(round(w / target_ratio))
                top = (h - new_h) // 2
                crop = img.crop((0, top, w, top + new_h))
            out_path = out_dir / f"{slug}-{key}_{ratio_name}.webp"
            crop.save(str(out_path), "WEBP", quality=86, method=6)
            out[ratio_name] = str(out_path)
        return out


def get_image_gen(config: Optional[Dict[str, Any]]) -> ImageGen:
    """Factory for use by orchestrator and imagegen node."""
    return ImageGen(config)


__all__ = ["ImageGen", "get_image_gen"]
