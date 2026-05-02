"""
Evermind — Video Review Adapter (v6.1.15)

Feeds a short gameplay/animation video to a vision model for quality judgment.
Supports Chinese relay-friendly models:
- tongyi (Qwen-VL-Max via DashScope) — native video
- doubao-vl (Doubao Vision Pro via Volcengine Ark) — native video
- gemini (2.5 Flash) — native video, best cost/perf but needs Google key

Graceful fallback: if no vision model configured, returns None → reviewer
falls back to screenshot-only path.

Design rules:
1. Probe order: tongyi → doubao-vl → gemini → None (no fallback to frame-sample)
2. Hard timeout 60s
3. Expected output: JSON {pass:bool, issues:[str], confidence:0-100}
4. Video file is produced by Playwright context record_video_dir elsewhere

Usage:
    reviewer = VideoReview(config)
    if not reviewer.available:
        return None
    result = await reviewer.judge(video_path, task_type="game", goal="3D TPS shooter")
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import httpx  # type: ignore
except Exception:
    httpx = None  # type: ignore

logger = logging.getLogger("evermind.video_review")

_DEFAULT_TIMEOUT = float(os.getenv("EVERMIND_VIDEO_REVIEW_TIMEOUT_SEC", "60"))


def _extract_json_verdict(raw: str) -> Optional[Dict[str, Any]]:
    """Find first JSON object in LLM text, tolerant to fences and extra prose."""
    if not raw:
        return None
    text = raw.strip()
    # Strip fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    # Find first { ... } block
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        # Try to fix trailing commas
        t = re.sub(r",(\s*[}\]])", r"\1", m.group(0))
        try:
            return json.loads(t)
        except Exception:
            return None


class VideoReview:
    """Vision-model adapter for short gameplay/animation review."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = (config or {}).get("video_review") if isinstance(config, dict) else {}
        cfg = cfg if isinstance(cfg, dict) else {}
        self.provider: str = str(cfg.get("provider") or "").strip().lower()
        self.api_key: str = str(cfg.get("api_key") or "").strip()
        self.base_url: str = str(cfg.get("base_url") or "").strip().rstrip("/")
        self.model: str = str(cfg.get("model") or "").strip()
        # Auto-detect fallback: reuse DashScope key if user has tongyi VL via analyst config
        if not self.provider or not self.api_key:
            auto = self._auto_detect(config or {})
            if auto:
                self.provider = auto["provider"]
                self.api_key = auto["api_key"]
                self.base_url = auto.get("base_url", "")
                self.model = auto.get("model", "")

    @staticmethod
    def _auto_detect(config: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Detect a usable vision-capable provider from user's existing keys."""
        # Qwen-VL via DashScope (user's existing analyst key, if any)
        qwen_key = config.get("qwen_api_key") or config.get("dashscope_api_key")
        if qwen_key:
            return {
                "provider": "tongyi",
                "api_key": str(qwen_key).strip(),
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "model": "qwen-vl-max-latest",
            }
        # Doubao via Volcengine
        doubao_key = config.get("doubao_api_key") or config.get("volcengine_api_key")
        if doubao_key:
            return {
                "provider": "doubao-vl",
                "api_key": str(doubao_key).strip(),
                "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "model": "doubao-vision-pro-32k-241028",
            }
        # Gemini via Google
        gemini_key = config.get("gemini_api_key") or os.getenv("GEMINI_API_KEY", "")
        if gemini_key:
            return {
                "provider": "gemini",
                "api_key": str(gemini_key).strip(),
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "model": "gemini-2.5-flash",
            }
        return None

    @property
    def available(self) -> bool:
        if not httpx:
            return False
        return bool(self.provider and self.api_key)

    async def judge(
        self,
        video_path: str,
        task_type: str = "game",
        goal: str = "",
        custom_rubric: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Send video to vision model, return structured verdict or None on failure."""
        if not self.available:
            return None
        p = Path(video_path)
        if not p.exists() or p.stat().st_size < 1024:
            logger.warning("Video review: video missing or too small: %s", video_path)
            return None
        rubric = self._build_rubric(task_type, goal, custom_rubric)
        try:
            raw = await asyncio.wait_for(
                self._dispatch(str(p.resolve()), rubric),
                timeout=_DEFAULT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("Video review timeout (%ss)", _DEFAULT_TIMEOUT)
            return None
        except Exception as e:
            logger.warning("Video review failed: %s", str(e)[:200])
            return None
        verdict = _extract_json_verdict(raw or "")
        if verdict is None:
            return {"status": "parse_error", "raw": str(raw)[:800]}
        verdict.setdefault("status", "ok")
        verdict.setdefault("provider", self.provider)
        return verdict

    @staticmethod
    def _build_rubric(task_type: str, goal: str, custom: Optional[List[str]]) -> str:
        tt = (task_type or "").lower()
        if tt == "game":
            base = (
                "Watch the clip and answer YES/NO for each:\n"
                "- wasd_moves_player: does WASD produce visible player translation?\n"
                "- mouse_rotates_camera: does mouse move rotate camera yaw/pitch?\n"
                "- fire_produces_projectile: does clicking fire spawn bullets or trigger animation?\n"
                "- enemy_visible: are enemies/monsters rendered and moving (not frozen)?\n"
                "- transitions_smooth: are motion transitions smooth (no jitter)?\n"
            )
        elif tt in ("website", "landing", "portfolio"):
            base = (
                "Watch the clip and answer:\n"
                "- scroll_reveal_works: do sections animate in smoothly on scroll?\n"
                "- nav_link_functional: does a nav link click cause content change?\n"
                "- images_render: do all visible images load (no broken icons)?\n"
                "- content_visible: is there substantive visible copy (not placeholder)?\n"
            )
        elif tt in ("slides", "presentation"):
            base = (
                "Watch the clip and answer:\n"
                "- arrow_navigates: do arrow keys advance slides?\n"
                "- content_per_slide: does each slide have headline + body content?\n"
                "- transitions_smooth: smooth slide transitions?\n"
            )
        else:
            base = (
                "Watch the clip and identify any critical bugs visible:\n"
                "- frozen_frames\n- broken_layout\n- runtime errors visible\n"
            )
        extras = ""
        if custom:
            extras = "\nAdditional checks:\n" + "\n".join(f"- {c}" for c in custom)
        return (
            f"You are an experienced QA reviewer for a {task_type} project. Goal: {goal or 'n/a'}.\n\n"
            f"{base}{extras}\n\n"
            "Respond ONLY with a JSON object:\n"
            "{\"pass\": bool, \"confidence\": 0-100, \"issues\": [str], \"notes\": \"brief summary\"}\n"
            "Set pass=true only if the core loop works (critical items above are YES)."
        )

    async def _dispatch(self, video_path: str, rubric: str) -> Optional[str]:
        if self.provider == "tongyi":
            return await self._call_qwen_vl(video_path, rubric)
        if self.provider == "doubao-vl":
            return await self._call_doubao_vl(video_path, rubric)
        if self.provider == "gemini":
            return await self._call_gemini(video_path, rubric)
        logger.warning("Unknown video review provider: %s", self.provider)
        return None

    async def _call_qwen_vl(self, video_path: str, rubric: str) -> Optional[str]:
        """Qwen-VL-Max via DashScope OpenAI-compat."""
        base = self.base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        model = self.model or "qwen-vl-max-latest"
        # DashScope video accepts a local file URI via `video_url: file://...` OR base64 data URL
        b = Path(video_path).read_bytes()
        data_url = "data:video/webm;base64," + base64.b64encode(b).decode()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "video_url", "video_url": {"url": data_url}},
                    {"type": "text", "text": rubric},
                ],
            }],
            "temperature": 0.1,
        }
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            r = await client.post(f"{base}/chat/completions", json=payload, headers=headers)
            if r.status_code != 200:
                logger.warning("qwen-vl %s: %s", r.status_code, r.text[:200])
                return None
            data = r.json()
            return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""

    async def _call_doubao_vl(self, video_path: str, rubric: str) -> Optional[str]:
        """Doubao Vision Pro via Volcengine Ark OpenAI-compat."""
        base = self.base_url or "https://ark.cn-beijing.volces.com/api/v3"
        model = self.model or "doubao-vision-pro-32k-241028"
        b = Path(video_path).read_bytes()
        data_url = "data:video/webm;base64," + base64.b64encode(b).decode()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "video_url", "video_url": {"url": data_url}},
                    {"type": "text", "text": rubric},
                ],
            }],
            "temperature": 0.1,
        }
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            r = await client.post(f"{base}/chat/completions", json=payload, headers=headers)
            if r.status_code != 200:
                logger.warning("doubao-vl %s: %s", r.status_code, r.text[:200])
                return None
            data = r.json()
            return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""

    async def _call_gemini(self, video_path: str, rubric: str) -> Optional[str]:
        """Gemini 2.5 Flash with inline video (for videos < 20MB)."""
        base = self.base_url or "https://generativelanguage.googleapis.com/v1beta"
        model = self.model or "gemini-2.5-flash"
        b = Path(video_path).read_bytes()
        if len(b) > 19_000_000:
            logger.warning("Gemini inline video >19MB; skip (use File API in future)")
            return None
        url = f"{base}/models/{model}:generateContent?key={self.api_key}"
        payload = {
            "contents": [{
                "parts": [
                    {"inlineData": {"mimeType": "video/webm", "data": base64.b64encode(b).decode()}},
                    {"text": rubric},
                ]
            }],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
        }
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            r = await client.post(url, json=payload)
            if r.status_code != 200:
                logger.warning("gemini %s: %s", r.status_code, r.text[:200])
                return None
            data = r.json()
            try:
                return data["candidates"][0]["content"]["parts"][0]["text"]
            except Exception:
                return None


def is_video_review_available(config: Optional[Dict[str, Any]] = None) -> bool:
    """Quick predicate for orchestrator: should we record video at all?"""
    return VideoReview(config).available


__all__ = ["VideoReview", "is_video_review_available", "_extract_json_verdict"]
