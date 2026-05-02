"""
quality_guard.py -- Retry Quality Degradation Guard + Output Extraction

Fused from 7 mature open-source projects:
- AutoGen CODE_BLOCK_PATTERN: code extraction from markdown (microsoft/autogen)
- Aider multi-fence: handles 6+ fence styles (paul-gauthier/aider)
- CrewAI GuardrailResult: structured pass/fail + error feedback (crewAIInc/crewAI)
- OpenHands StuckDetector: 5-pattern stuck detection (All-Hands-AI/OpenHands)
- LangGraph RetryPolicy: exponential backoff + jitter (langchain-ai/langgraph)
- MetaGPT CodeParser + OutputRepair: output format repair (geekan/MetaGPT)
- Instructor FailedAttempt: historical attempt tracking (jxnl/instructor)

Used by Evermind builder/merger nodes for retry quality protection.
"""

import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# -- AutoGen CODE_BLOCK_PATTERN (from autogen-ext/code_executors/_common.py) --

# Battle-tested regex from Microsoft AutoGen for extracting code blocks from markdown
CODE_BLOCK_PATTERN = r"```[ \t]*(\w+)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```"

# -- Aider multi-fence support (from aider/coders/editblock_coder.py) ----------

# LLMs use unpredictable fence styles; Aider handles all of them
ALL_FENCES = [
    ("```", "```"),
    ("````", "````"),
    ("<source>", "</source>"),
    ("<code>", "</code>"),
    ("<pre>", "</pre>"),
    ("<codeblock>", "</codeblock>"),
    ("<sourcecode>", "</sourcecode>"),
]


# -- LangGraph RetryPolicy (from langgraph/types.py) --------------------------

@dataclass
class RetryPolicy:
    """Structured retry policy -- ported from LangGraph"""
    max_attempts: int = 3
    initial_interval: float = 0.5
    backoff_factor: float = 2.0
    max_interval: float = 60.0
    jitter: bool = True


# -- CrewAI GuardrailResult (from crewai/utilities/guardrail.py) ---------------

@dataclass
class GuardrailResult:
    """Structured quality check result -- ported from CrewAI.
    On failure, error is fed back to the agent as retry context."""
    success: bool
    result: Optional[str] = None
    error: Optional[str] = None
    score: float = 0.0
    missing: Optional[List[str]] = None

    @classmethod
    def passed(cls, result: str, score: float = 1.0) -> "GuardrailResult":
        return cls(success=True, result=result, score=score)

    @classmethod
    def failed(cls, error: str, score: float = 0.0, missing: Optional[List[str]] = None) -> "GuardrailResult":
        return cls(success=False, error=error, score=score, missing=missing)


# -- Instructor AttemptRecord (from instructor/core/exceptions.py) -------------

@dataclass
class AttemptRecord:
    """Single attempt record -- ported from Instructor FailedAttempt"""
    attempt: int
    output: str
    score: float
    length: int
    error: Optional[str] = None
    file_sizes: Optional[Dict[str, int]] = None
    timestamp: float = field(default_factory=time.time)


# -- HTML Extraction Pipeline (fused: AutoGen + Aider + MetaGPT) ---------------

class HTMLExtractor:
    """Extract valid HTML from messy AI output that may contain markdown fences, prose, etc.

    Fused from:
    - AutoGen CODE_BLOCK_PATTERN (primary regex)
    - Aider multi-fence support (fallback fences)
    - MetaGPT CodeParser (progressive fallback chain)

    The key architectural insight from all 3 projects:
    NEVER trust a single extraction strategy. Use a cascade.
    """

    @staticmethod
    def extract(raw_output: str) -> str:
        """Extract HTML from AI output. Returns cleaned HTML or raw output as fallback.

        Strategy chain (stops at first success):
        1. AutoGen pattern: ```html ... ```
        2. AutoGen pattern: ``` ... ``` (any lang) containing HTML markers
        3. Aider fences: <source>, <code>, <pre>, <codeblock>, etc.
        4. Direct HTML detection: <!DOCTYPE html> ... </html>
        5. Fallback: return raw output stripped
        """
        if not raw_output:
            return ""

        raw = raw_output.strip()

        # Strategy 1: AutoGen CODE_BLOCK_PATTERN with html/htm language hint
        html_fence = re.search(
            r"```[ \t]*(?:html|htm)[ \t]*\r?\n(.*?)\r?\n[ \t]*```",
            raw, re.DOTALL | re.IGNORECASE,
        )
        if html_fence:
            extracted = html_fence.group(1).strip()
            if len(extracted) > 200:
                return extracted

        # Strategy 2: AutoGen pattern for any fence containing HTML markers
        all_blocks = re.findall(CODE_BLOCK_PATTERN, raw, re.DOTALL)
        for lang, content in all_blocks:
            content = content.strip()
            if len(content) > 200 and re.search(
                r"<!doctype\s+html|<html[\s>]|<head[\s>]|<body[\s>]",
                content, re.IGNORECASE,
            ):
                return content

        # Strategy 3: Aider multi-fence support
        for open_fence, close_fence in ALL_FENCES[1:]:  # skip ``` (already tried)
            pattern = re.escape(open_fence) + r"\s*(.*?)\s*" + re.escape(close_fence)
            match = re.search(pattern, raw, re.DOTALL | re.IGNORECASE)
            if match:
                content = match.group(1).strip()
                if len(content) > 200 and re.search(
                    r"<!doctype\s+html|<html[\s>]", content, re.IGNORECASE,
                ):
                    return content

        # Strategy 4: Direct HTML root detection (no fences)
        html_start = re.search(r"<!doctype\s+html|<html[\s>]", raw, re.IGNORECASE)
        html_end_pos = raw.rfind("</html>")
        if html_start and html_end_pos > html_start.start():
            return raw[html_start.start():html_end_pos + 7]

        # Strategy 5: If most of the content is HTML-like, return as-is
        if raw.count("<") > 10 and raw.count(">") > 10:
            return raw

        return raw

    @staticmethod
    def extract_all_code_blocks(raw_output: str) -> List[Tuple[str, str]]:
        """Extract all (language, content) pairs from markdown code fences.
        Ported from AutoGen CODE_BLOCK_PATTERN."""
        if not raw_output:
            return []
        return re.findall(CODE_BLOCK_PATTERN, raw_output, re.DOTALL)


# -- HTML Completeness Validator -----------------------------------------------

class HTMLValidator:
    """Validate HTML structural completeness.
    Checks for required elements without external dependencies (no BS4 needed).
    """

    REQUIRED_ELEMENTS = [
        ("doctype", r"<!doctype\s+html", "<!DOCTYPE html>"),
        ("html_open", r"<html[\s>]", "<html>"),
        ("head", r"<head[\s>]", "<head>"),
        ("body", r"<body[\s>]", "<body>"),
        ("html_close", r"</html>", "</html>"),
        ("body_close", r"</body>", "</body>"),
    ]

    @classmethod
    def validate(cls, html: str) -> GuardrailResult:
        """Check HTML completeness. Returns GuardrailResult with score 0.0-1.0."""
        if not html or len(html.strip()) < 50:
            return GuardrailResult.failed(
                "HTML content too short or empty",
                score=0.0,
                missing=["content"],
            )

        missing = []
        for name, pattern, display in cls.REQUIRED_ELEMENTS:
            if not re.search(pattern, html, re.IGNORECASE):
                missing.append(display)

        checks_total = len(cls.REQUIRED_ELEMENTS)
        checks_passed = checks_total - len(missing)
        score = checks_passed / checks_total

        # Additional content quality checks
        has_style = bool(re.search(r"<style[\s>]", html, re.IGNORECASE))
        has_script = bool(re.search(r"<script[\s>]", html, re.IGNORECASE))
        content_len = len(html)

        # Boost score for substantial content
        if content_len > 5000:
            score = min(1.0, score + 0.1)
        if has_style:
            score = min(1.0, score + 0.05)
        if has_script:
            score = min(1.0, score + 0.05)

        if missing:
            return GuardrailResult.failed(
                f"Missing HTML elements: {', '.join(missing)}",
                score=score,
                missing=missing,
            )

        return GuardrailResult.passed(html, score=score)


# -- OpenHands StuckDetector (from openhands/controller/stuck.py) ---------------

class StuckDetector:
    """Detect agent stuck loops -- ported from OpenHands.

    Detects 5 patterns (matching OpenHands source):
    1. Repeating output: consecutive outputs nearly identical
    2. Quality degradation: 3 consecutive score drops
    3. Size collapse: output < 50% of previous
    4. Oscillation: A-B-A-B pattern across 4+ attempts
    5. Identical hash: exact same output repeated
    """

    @staticmethod
    def is_output_repeating(
        attempts: List[AttemptRecord],
        similarity_threshold: float = 0.90,
    ) -> bool:
        """Detect if last two outputs are nearly identical (character-level similarity)."""
        if len(attempts) < 2:
            return False
        last = attempts[-1].output
        prev = attempts[-2].output
        if not last or not prev:
            return False
        shorter = min(len(last), len(prev))
        if shorter < 100:
            return False
        common = sum(1 for a, b in zip(last[:shorter], prev[:shorter]) if a == b)
        similarity = common / shorter
        return similarity >= similarity_threshold

    @staticmethod
    def is_quality_degrading(attempts: List[AttemptRecord]) -> bool:
        """Detect quality declining on each retry (3 consecutive drops)."""
        if len(attempts) < 3:
            return False
        scores = [a.score for a in attempts[-3:]]
        return scores[0] > scores[1] > scores[2]

    @staticmethod
    def is_size_collapsing(
        attempts: List[AttemptRecord],
        collapse_ratio: float = 0.5,
    ) -> bool:
        """Detect output size collapse (e.g. 70KB -> 6KB)."""
        if len(attempts) < 2:
            return False
        prev_len = attempts[-2].length
        curr_len = attempts[-1].length
        if prev_len < 1000:
            return False
        return curr_len < prev_len * collapse_ratio

    @staticmethod
    def is_oscillating(attempts: List[AttemptRecord]) -> bool:
        """Detect A-B-A-B oscillation pattern across 4+ attempts.
        Ported from OpenHands alternating pattern detection."""
        if len(attempts) < 4:
            return False
        import hashlib
        hashes = [
            hashlib.md5((a.output or "")[:2000].encode()).hexdigest()
            for a in attempts[-4:]
        ]
        # A-B-A-B pattern
        return hashes[0] == hashes[2] and hashes[1] == hashes[3] and hashes[0] != hashes[1]

    @staticmethod
    def detect_garbage_repetition(output: str, threshold: float = 0.4) -> bool:
        """Detect repetitive garbage content (e.g. same line repeated 100+ times)."""
        if not output or len(output) < 500:
            return False
        lines = output.strip().splitlines()
        if len(lines) < 10:
            return False
        from collections import Counter
        counter = Counter(line.strip() for line in lines if line.strip())
        if not counter:
            return False
        most_common_count = counter.most_common(1)[0][1]
        return most_common_count / len(lines) > threshold

    @staticmethod
    def detect_html_garbage_repetition(html_content: str, *, threshold: float = 0.25) -> bool:
        """Detect repetitive garbage in HTML content by stripping tags and analyzing text diversity.

        Checks:
        1. Word-level diversity (unique/total) below threshold
        2. Bigram repetition: any single bigram > 15% of all bigrams
        """
        if not html_content or len(html_content) < 500:
            return False
        text = re.sub(r"<[^>]+>", " ", html_content)
        text = re.sub(r"\s+", " ", text).strip()
        if not text or len(text) < 200:
            return False
        words = text.lower().split()
        if len(words) < 20:
            return False
        diversity = len(set(words)) / len(words)
        if diversity < threshold:
            return True
        if len(words) >= 10:
            bigrams = [f"{words[i]} {words[i + 1]}" for i in range(len(words) - 1)]
            counts: Dict[str, int] = {}
            for bg in bigrams:
                counts[bg] = counts.get(bg, 0) + 1
            if counts:
                max_count = max(counts.values())
                if max_count > max(10, len(bigrams) * 0.15):
                    return True
        return False


# -- MetaGPT OutputRepair (from metagpt/utils/repair_llm_raw_output.py) --------

class OutputRepair:
    """AI output repair pipeline -- ported from MetaGPT.

    Repairs common format issues before retry, avoiding unnecessary API calls.
    """

    @staticmethod
    def extract_html_from_raw_output(raw_output: str) -> str:
        """Extract HTML from raw output. Delegates to HTMLExtractor."""
        return HTMLExtractor.extract(raw_output)

    @staticmethod
    def repair_missing_closing_tags(html: str) -> str:
        """Fix common unclosed tag issues."""
        tags_to_check = ["style", "script", "body", "html"]
        for tag in tags_to_check:
            open_count = len(re.findall(rf"<{tag}[\s>]", html, re.IGNORECASE))
            close_count = len(re.findall(rf"</{tag}>", html, re.IGNORECASE))
            if open_count > close_count:
                html = html + f"\n</{tag}>"
        return html

    @staticmethod
    def repair_truncated_html(html: str) -> str:
        """Repair truncated HTML by closing unclosed tags.
        Stack-based approach without external dependencies."""
        if not html:
            return html
        # Find all unclosed tags
        tag_stack = []
        for match in re.finditer(r"<(/?)(\w+)[\s>/]", html):
            is_close = match.group(1) == "/"
            tag_name = match.group(2).lower()
            void_tags = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}
            if tag_name in void_tags:
                continue
            if is_close:
                if tag_stack and tag_stack[-1] == tag_name:
                    tag_stack.pop()
            else:
                tag_stack.append(tag_name)
        # Close remaining tags in reverse order
        for tag in reversed(tag_stack):
            html = html + f"\n</{tag}>"
        return html

    @staticmethod
    def repair_execution_report_tags(output: str) -> str:
        """Fix <execution_report> tag format issues."""
        if not output:
            return output
        output = re.sub(
            r"<Execution_Report>", "<execution_report>",
            output, flags=re.IGNORECASE,
        )
        output = re.sub(
            r"</Execution_Report>", "</execution_report>",
            output, flags=re.IGNORECASE,
        )
        if "<execution_report>" in output.lower() and "</execution_report>" not in output.lower():
            output = output + "\n</execution_report>"
        return output

    @staticmethod
    def repair_handoff_envelope_json(output: str) -> str:
        """Fix JSON format issues in <handoff_envelope>."""
        match = re.search(
            r"<handoff_envelope>(.*?)</handoff_envelope>",
            output, re.DOTALL | re.IGNORECASE,
        )
        if not match:
            return output
        json_str = match.group(1).strip()
        try:
            import json
            json.loads(json_str)
            return output
        except Exception:
            pass
        repaired = json_str
        repaired = re.sub(r"//.*$", "", repaired, flags=re.MULTILINE)
        repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
        try:
            import json
            json.loads(repaired)
            output = output.replace(json_str, repaired)
        except Exception:
            pass
        return output


# -- QualityDegradationGuard (fused from all 7 projects) ----------------------

class QualityDegradationGuard:
    """Comprehensive quality degradation protection.

    Fused from:
    1. Record each attempt's quality and output (Instructor)
    2. Detect stuck loops (OpenHands)
    3. Detect quality decline (OpenHands)
    4. Return historical best output (Instructor best-so-far)
    5. Calculate backoff intervals (LangGraph)
    6. Repair output format (MetaGPT)
    7. Structured pass/fail with error feedback (CrewAI)
    """

    def __init__(
        self,
        policy: Optional[RetryPolicy] = None,
        score_fn: Optional[Callable[[str], float]] = None,
    ):
        self.policy = policy or RetryPolicy()
        self.score_fn = score_fn
        self.attempts: List[AttemptRecord] = []
        self.stuck_detector = StuckDetector()
        self.output_repair = OutputRepair()
        self.html_extractor = HTMLExtractor()
        self.html_validator = HTMLValidator()

    def record_attempt(
        self,
        output: str,
        score: float,
        error: Optional[str] = None,
        file_sizes: Optional[Dict[str, int]] = None,
    ) -> AttemptRecord:
        """Record one attempt."""
        record = AttemptRecord(
            attempt=len(self.attempts) + 1,
            output=output,
            score=score,
            length=len(output or ""),
            error=error,
            file_sizes=file_sizes,
        )
        self.attempts.append(record)
        logger.info(
            "QualityGuard: attempt=%d score=%.1f len=%d error=%s",
            record.attempt, record.score, record.length,
            (record.error or "")[:100],
        )
        return record

    @property
    def best_so_far(self) -> Optional[AttemptRecord]:
        """Return best historical attempt (Instructor pattern)."""
        if not self.attempts:
            return None
        return max(self.attempts, key=lambda a: a.score)

    def should_stop_retry(self) -> Tuple[bool, str]:
        """Comprehensive retry termination decision.

        Returns: (should_stop: bool, reason: str)
        """
        if len(self.attempts) >= self.policy.max_attempts:
            return True, f"max_attempts({self.policy.max_attempts}) reached"

        if StuckDetector.is_output_repeating(self.attempts):
            return True, "stuck_repeating (output too similar to previous)"

        if StuckDetector.is_quality_degrading(self.attempts):
            return True, "quality_degrading (3 consecutive score drops)"

        if StuckDetector.is_size_collapsing(self.attempts):
            return True, "size_collapsing (output drastically shorter)"

        if StuckDetector.is_oscillating(self.attempts):
            return True, "stuck_oscillating (A-B-A-B pattern detected)"

        # Check for garbage repetition in latest output
        if self.attempts and StuckDetector.detect_garbage_repetition(self.attempts[-1].output):
            return True, "garbage_repetition (same content repeated)"

        return False, ""

    def get_backoff_seconds(self) -> float:
        """Calculate retry backoff -- from LangGraph."""
        attempt = len(self.attempts)
        interval = min(
            self.policy.max_interval,
            self.policy.initial_interval * (self.policy.backoff_factor ** max(0, attempt - 1)),
        )
        if self.policy.jitter:
            interval += random.uniform(0, 1)
        return interval

    def validate_html_output(self, raw_output: str) -> GuardrailResult:
        """Full pipeline: extract -> validate -> return GuardrailResult (CrewAI pattern).

        This is the main entry point for builder output validation.
        """
        # Step 1: Extract HTML from markdown/prose
        html = self.html_extractor.extract(raw_output)

        # Step 2: Validate completeness
        result = self.html_validator.validate(html)

        # Step 3: Check for garbage
        if StuckDetector.detect_garbage_repetition(html):
            return GuardrailResult.failed(
                "Repetitive garbage content detected",
                score=max(0.0, result.score - 0.5),
            )

        return result

    def repair_output(self, raw_output: str) -> str:
        """Repair output format issues -- from MetaGPT."""
        output = self.output_repair.repair_execution_report_tags(raw_output)
        output = self.output_repair.repair_handoff_envelope_json(output)
        return output

    def build_retry_context(self) -> str:
        """Build retry context string with error feedback -- from CrewAI guardrail pattern.

        Feed specific failure reasons back to the agent so it can adjust approach.
        """
        if not self.attempts:
            return ""
        last = self.attempts[-1]
        parts = []
        if last.error:
            parts.append(f"Previous attempt failed: {last.error}")
        parts.append(f"Previous quality score: {last.score:.1f}/10")
        parts.append(f"Previous output length: {last.length} chars")
        if len(self.attempts) >= 2:
            best = self.best_so_far
            if best and best.attempt != last.attempt:
                parts.append(f"Best attempt so far was #{best.attempt} (score={best.score:.1f})")
        return "\n".join(parts)
