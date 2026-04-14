"""
Evermind — Prompt & Context Compressor
V4.4: 4-layer compaction pipeline support.

Two backends:
  1. Lightweight sentence-importance filter (always available, zero deps)
  2. LLMLingua-2 (optional — pip install llmlingua)

Usage:
    from prompt_compressor import get_compressor
    comp = get_compressor()
    short = comp.compress(long_text, target_ratio=0.5)
"""

import logging
import os
import re
from typing import List, Optional, Tuple

logger = logging.getLogger("evermind.prompt_compressor")

# ── Attempt LLMLingua import ──────────────────────────────
_llmlingua_available = False
_llmlingua_compressor = None

try:
    from llmlingua import PromptCompressor as _LLMCompressor
    _llmlingua_available = True
    logger.info("LLMLingua package detected — will lazy-load on first compress call")
except ImportError:
    pass

# ── Environment controls ──────────────────────────────────
# EVERMIND_COMPRESSOR_BACKEND: "auto" (default), "lightweight", "llmlingua"
_BACKEND_PREF = os.getenv("EVERMIND_COMPRESSOR_BACKEND", "auto").strip().lower()


# ── Sentence splitter (works for Chinese + English) ──────
_SENT_SPLIT = re.compile(
    r'(?<=[。！？.!?\n])\s*'     # after sentence-ending punct
    r'|(?<=\n)\s*'               # after newline
)

_BULLET_RE = re.compile(r'^[\s]*[-*•▸►→]\s', re.MULTILINE)
_HEADING_RE = re.compile(r'^#{1,4}\s', re.MULTILINE)
_INSTRUCTION_KEYWORDS = re.compile(
    r'\b(MUST|NEVER|ALWAYS|IMPORTANT|CRITICAL|DO NOT|禁止|必须|不要|不得|严禁|务必)\b',
    re.IGNORECASE,
)
_CODE_FENCE_RE = re.compile(r'```')


def _split_sentences(text: str) -> List[str]:
    """Split text into sentences, preserving bullet items and headings."""
    parts = _SENT_SPLIT.split(text)
    return [p for p in parts if p.strip()]


def _score_sentence(sent: str, position: float, total: int) -> float:
    """Score a sentence by importance (higher = more important).

    Factors:
    - Position: first and last sentences score higher
    - Keywords: instruction keywords boost importance
    - Structure: headings and bullets are structural, keep them
    - Length: very short sentences are often labels/headers, keep them
    - Code fences: always keep
    """
    score = 0.5

    # Position bias: first 20% and last 10% of text
    if position < 0.2:
        score += 0.3
    elif position > 0.9:
        score += 0.2

    # Instruction keywords — critical, never remove
    if _INSTRUCTION_KEYWORDS.search(sent):
        score += 0.5

    # Structural markers
    if _HEADING_RE.match(sent):
        score += 0.4
    if _BULLET_RE.match(sent):
        score += 0.1

    # Code fences — always keep
    if _CODE_FENCE_RE.search(sent):
        score += 0.6

    # Very short (likely a label or header)
    if len(sent.strip()) < 40:
        score += 0.15

    # Long verbose explanations score lower
    if len(sent.strip()) > 300:
        score -= 0.15

    return min(1.0, max(0.0, score))


class LightweightCompressor:
    """Sentence-level importance filter. Zero external dependencies."""

    def compress(
        self,
        text: str,
        target_ratio: float = 0.6,
        min_chars: int = 500,
    ) -> str:
        """Compress text to ~target_ratio of original length.

        Args:
            text: Input text to compress
            target_ratio: Target output/input ratio (0.0-1.0)
            min_chars: Don't compress texts shorter than this

        Returns:
            Compressed text
        """
        if not text or len(text) < min_chars:
            return text

        sentences = _split_sentences(text)
        if len(sentences) <= 3:
            return text

        target_chars = int(len(text) * target_ratio)

        # Score each sentence
        scored: List[Tuple[int, float, str]] = []
        for i, sent in enumerate(sentences):
            pos = i / max(1, len(sentences) - 1)
            score = _score_sentence(sent, pos, len(sentences))
            scored.append((i, score, sent))

        # Sort by score descending, select top sentences up to budget
        scored.sort(key=lambda x: x[1], reverse=True)
        selected_indices = set()
        current_chars = 0
        for idx, score, sent in scored:
            if current_chars + len(sent) > target_chars and selected_indices:
                # Allow overshoot by one sentence to avoid cutting too much
                if current_chars > target_chars * 0.8:
                    break
            selected_indices.add(idx)
            current_chars += len(sent)

        # Reconstruct in original order
        result_parts = [sent for i, _, sent in sorted(scored, key=lambda x: x[0]) if i in selected_indices]
        result = '\n'.join(result_parts)

        savings_pct = (1 - len(result) / max(1, len(text))) * 100
        if savings_pct > 2:
            logger.debug(
                "Lightweight compress: %d→%d chars (%.0f%% saved, ratio=%.2f)",
                len(text), len(result), savings_pct, target_ratio,
            )
        return result

    def compress_aggressive(
        self,
        text: str,
        target_ratio: float = 0.35,
        min_chars: int = 300,
    ) -> str:
        """V4.4: Aggressive compression for context-critical situations.

        Uses a two-pass approach:
        1. Standard sentence selection with tight budget
        2. Within selected sentences, trim redundant clauses

        Returns text compressed to ~target_ratio (can reach 0.25-0.40).
        """
        if not text or len(text) < min_chars:
            return text

        # First pass: standard sentence filtering at tighter ratio
        first_pass = self.compress(text, target_ratio=target_ratio, min_chars=min_chars)

        # Second pass: trim verbose sentences
        _FILLER_RE = re.compile(
            r'\b(basically|essentially|in other words|as mentioned|'
            r'it is worth noting that|it should be noted that|'
            r'please note that|as you can see|for example|for instance|'
            r'简单来说|换句话说|值得注意的是|如上所述|举个例子)\b',
            re.IGNORECASE,
        )
        lines = first_pass.split('\n')
        trimmed = []
        for line in lines:
            cleaned = _FILLER_RE.sub('', line).strip()
            # Collapse multiple spaces from filler removal
            cleaned = re.sub(r'\s{2,}', ' ', cleaned)
            if cleaned:
                trimmed.append(cleaned)

        result = '\n'.join(trimmed)
        savings_pct = (1 - len(result) / max(1, len(text))) * 100
        if savings_pct > 5:
            logger.debug(
                "Aggressive compress: %d→%d chars (%.0f%% saved)",
                len(text), len(result), savings_pct,
            )
        return result

    def compress_messages(
        self,
        messages: List[dict],
        target_ratio: float = 0.6,
        preserve_last_n: int = 4,
    ) -> List[dict]:
        """Compress a conversation message list.

        Preserves system messages and the last N messages intact.
        Compresses older user/assistant/tool messages.
        """
        if not messages:
            return messages

        result = []
        compress_upto = max(0, len(messages) - preserve_last_n)

        for i, msg in enumerate(messages):
            msg_copy = dict(msg)
            role = str(msg_copy.get("role", ""))

            # Never compress system messages
            if role == "system" or i >= compress_upto:
                result.append(msg_copy)
                continue

            content = msg_copy.get("content")
            if isinstance(content, str) and len(content) > 500:
                msg_copy["content"] = self.compress(content, target_ratio=target_ratio)

            result.append(msg_copy)

        return result


class LLMLinguaCompressor:
    """LLMLingua-2 backend — semantic compression using small LM."""

    def __init__(self):
        self._compressor = None
        self._load_error = None

    def _ensure_loaded(self):
        if self._compressor is not None or self._load_error is not None:
            return
        try:
            self._compressor = _LLMCompressor(
                model_name="microsoft/llmlingua-2-xlm-roberta-large-meetingbank",
                use_llmlingua2=True,
            )
            logger.info("LLMLingua-2 loaded successfully (xlm-roberta multilingual)")
        except Exception as exc:
            self._load_error = str(exc)
            logger.warning("LLMLingua-2 load failed: %s — falling back to lightweight", self._load_error)

    def compress(
        self,
        text: str,
        target_ratio: float = 0.5,
        min_chars: int = 500,
        instruction: str = "",
    ) -> str:
        if not text or len(text) < min_chars:
            return text

        self._ensure_loaded()
        if not self._compressor:
            return _lightweight.compress(text, target_ratio=target_ratio, min_chars=min_chars)

        try:
            result = self._compressor.compress_prompt(
                [text],
                instruction=instruction,
                rate=target_ratio,
                force_tokens=['\n', '?', '!', '。', '？', '！', '-', '#'],
            )
            compressed = result.get("compressed_prompt", text)
            savings_pct = (1 - len(compressed) / max(1, len(text))) * 100
            logger.debug(
                "LLMLingua compress: %d→%d chars (%.0f%% saved)",
                len(text), len(compressed), savings_pct,
            )
            return compressed
        except Exception as exc:
            logger.warning("LLMLingua compress failed: %s — fallback", str(exc)[:200])
            return _lightweight.compress(text, target_ratio=target_ratio, min_chars=min_chars)

    def compress_messages(
        self,
        messages: List[dict],
        target_ratio: float = 0.5,
        preserve_last_n: int = 4,
    ) -> List[dict]:
        """Compress conversation messages using LLMLingua."""
        if not messages:
            return messages

        self._ensure_loaded()
        if not self._compressor:
            return _lightweight.compress_messages(messages, target_ratio=target_ratio, preserve_last_n=preserve_last_n)

        result = []
        compress_upto = max(0, len(messages) - preserve_last_n)

        for i, msg in enumerate(messages):
            msg_copy = dict(msg)
            role = str(msg_copy.get("role", ""))

            if role == "system" or i >= compress_upto:
                result.append(msg_copy)
                continue

            content = msg_copy.get("content")
            if isinstance(content, str) and len(content) > 500:
                msg_copy["content"] = self.compress(content, target_ratio=target_ratio)

            result.append(msg_copy)

        return result


# ── Singleton instances ──────────────────────────────────
_lightweight = LightweightCompressor()
_llmlingua: Optional[LLMLinguaCompressor] = None


def get_compressor(prefer_llmlingua: bool = True):
    """Return the best available compressor.

    If prefer_llmlingua=True and llmlingua is installed, uses LLMLingua-2.
    Otherwise returns the lightweight sentence-filter compressor.
    Respects EVERMIND_COMPRESSOR_BACKEND env var.
    """
    global _llmlingua
    if _BACKEND_PREF == "lightweight":
        return _lightweight
    if _BACKEND_PREF == "llmlingua" or (prefer_llmlingua and _BACKEND_PREF == "auto"):
        if _llmlingua_available:
            if _llmlingua is None:
                _llmlingua = LLMLinguaCompressor()
            return _llmlingua
    return _lightweight


def compress_context(
    text: str,
    target_ratio: float = 0.6,
    min_chars: int = 500,
) -> str:
    """Convenience: compress text with the best available backend."""
    return get_compressor().compress(text, target_ratio=target_ratio, min_chars=min_chars)


def compress_aggressive(
    text: str,
    target_ratio: float = 0.35,
    min_chars: int = 300,
) -> str:
    """V4.4: Aggressive compression for context-critical paths."""
    comp = get_compressor()
    if hasattr(comp, "compress_aggressive"):
        return comp.compress_aggressive(text, target_ratio=target_ratio, min_chars=min_chars)
    # LLMLingua backend doesn't have aggressive mode yet — use lower ratio
    return comp.compress(text, target_ratio=target_ratio, min_chars=min_chars)


def compress_messages(
    messages: List[dict],
    target_ratio: float = 0.6,
    preserve_last_n: int = 4,
) -> List[dict]:
    """Convenience: compress conversation messages."""
    return get_compressor().compress_messages(
        messages, target_ratio=target_ratio, preserve_last_n=preserve_last_n,
    )
