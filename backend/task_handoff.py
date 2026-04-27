"""
Evermind v3.0 — Task Handoff Protocol

Standardized inter-node communication protocol that ensures high-quality
context transfer between agent nodes.

Inspired by:
  - OpenClaw MCP's structured message passing
  - CrewAI's role-based handoff patterns
  - LangGraph's immutable state objects
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("evermind.task_handoff")


@dataclass
class HandoffPacket:
    """
    Standardized data packet for inter-node task handoff.

    Every node outputs a HandoffPacket when it completes. This packet
    is then injected into the downstream node's context, ensuring:
    1. No context loss between nodes
    2. Structured, machine-readable data exchange
    3. Quality signals for downstream decision-making
    """

    # Identity
    source_node: str = ""
    source_node_type: str = ""
    target_node: str = ""
    target_node_type: str = ""
    run_id: str = ""
    subtask_id: str = ""

    # Core deliverables
    context_summary: str = ""
    deliverables: List[str] = field(default_factory=list)
    files_produced: List[Any] = field(default_factory=list)  # [{path, purpose, lines}] or ["path"]
    files_modified: List[Dict[str, str]] = field(default_factory=list)

    # Decision log
    decisions_made: List[str] = field(default_factory=list)
    design_choices: List[Dict[str, str]] = field(default_factory=list)  # [{choice, rationale}]
    rejected_alternatives: List[str] = field(default_factory=list)

    # Quality signals
    quality_score: float = 0.0  # 0-1 self-assessed quality
    confidence_level: str = "medium"  # low, medium, high
    open_questions: List[str] = field(default_factory=list)
    known_issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # References
    reference_urls: List[str] = field(default_factory=list)
    source_bundles: List[Dict[str, str]] = field(default_factory=list)  # [{title, url, content_summary}]

    # Technical details
    technologies_used: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    api_patterns: List[str] = field(default_factory=list)

    # Metrics
    token_usage: Dict[str, int] = field(default_factory=dict)
    duration_seconds: float = 0.0
    tool_calls_count: int = 0
    iterations: int = 0

    # Timestamp
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for JSON storage."""
        return {
            "source_node": self.source_node,
            "source_node_type": self.source_node_type,
            "target_node": self.target_node,
            "target_node_type": self.target_node_type,
            "run_id": self.run_id,
            "subtask_id": self.subtask_id,
            "context_summary": self.context_summary,
            "deliverables": self.deliverables,
            "files_produced": self.files_produced,
            "files_modified": self.files_modified,
            "decisions_made": self.decisions_made,
            "design_choices": self.design_choices,
            "rejected_alternatives": self.rejected_alternatives,
            "quality_score": self.quality_score,
            "confidence_level": self.confidence_level,
            "open_questions": self.open_questions,
            "known_issues": self.known_issues,
            "warnings": self.warnings,
            "reference_urls": self.reference_urls,
            "source_bundles": self.source_bundles,
            "technologies_used": self.technologies_used,
            "dependencies": self.dependencies,
            "api_patterns": self.api_patterns,
            "token_usage": self.token_usage,
            "duration_seconds": self.duration_seconds,
            "tool_calls_count": self.tool_calls_count,
            "iterations": self.iterations,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HandoffPacket":
        """Deserialize from dict."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def to_context_message(self, lang: str = "en", *, verbose: bool = False) -> str:
        """
        Format the handoff packet as a readable context message
        for injection into the downstream node's prompt.

        v6.1.5 (maintainer 2026-04-19): THIN handoff. Default = ≤400 bytes rendered,
        reference to OpenHands AgentDelegateAction (4 fields) + MetaGPT Document
        .get_meta() (path-only refs) + CrewAI TaskOutput (auto summary).
        Prior renderer emitted ~1200 bytes every handoff. Pass verbose=True to
        restore full render for debug/analytics.
        """
        zh = lang == "zh"
        parts: List[str] = []

        header = (
            f"[handoff ← {self.source_node_type or self.source_node or 'node'}]"
            if not zh else
            f"[交接 ← {self.source_node_type or self.source_node or '节点'}]"
        )
        parts.append(header)

        brief = (self.context_summary or "").strip()
        if brief:
            brief_capped = brief if len(brief) <= 400 else brief[:397] + "..."
            parts.append(brief_capped)

        # File refs (path-only by default; drops 2000-byte source_bundles)
        file_bits: List[str] = []
        for f in (self.files_produced or [])[:5]:
            if isinstance(f, dict):
                p = str(f.get("path") or "").strip()
            else:
                p = str(f or "").strip()
            if p:
                file_bits.append(p)
        if file_bits:
            label = "产出" if zh else "Files"
            parts.append(f"{label}: " + ", ".join(file_bits))

        # Decisions (3 max, no rationale)
        dec = [d for d in (self.decisions_made or []) if str(d).strip()][:3]
        if dec:
            label = "决策" if zh else "Decisions"
            parts.append(f"{label}: " + " | ".join(d[:120] for d in dec))

        # Blockers (warnings + known_issues merged, 2 max)
        blockers: List[str] = []
        for source in (self.warnings, self.known_issues):
            for b in (source or []):
                s = str(b).strip()
                if s and s not in blockers:
                    blockers.append(s)
        if blockers:
            label = "待办" if zh else "Blockers"
            parts.append(f"{label}: " + " | ".join(b[:160] for b in blockers[:2]))

        # v6.1.5 (Opus Y1): preserve 1 open_question — low cost, high value
        # when analyst flags ambiguous briefs.
        if self.open_questions:
            label = "疑问" if zh else "Q"
            q = str(self.open_questions[0])[:120]
            if q.strip():
                parts.append(f"{label}: {q}")

        if not verbose:
            return "\n".join(parts)

        # Verbose fallback (original behavior for debug pages)
        parts.append("")
        if self.deliverables:
            parts.append(("交付" if zh else "Deliverables") + ":")
            parts.extend(f"- {item}" for item in self.deliverables[:10])
        if self.design_choices:
            parts.append(("设计" if zh else "Design choices") + ":")
            for dc in self.design_choices[:4]:
                parts.append(f"- {dc.get('choice','')}: {dc.get('rationale','')}")
        if self.source_bundles:
            parts.append(("参考" if zh else "References") + ":")
            for sb in self.source_bundles[:3]:
                title = sb.get("title", "")
                url = sb.get("url", "")
                parts.append(f"- {title} ({url})")
        if self.technologies_used:
            parts.append(("技术" if zh else "Tech") + ": " + ", ".join(self.technologies_used))
        if self.open_questions:
            parts.append(("疑问" if zh else "Open questions") + ":")
            parts.extend(f"- {q}" for q in self.open_questions[:3])
        return "\n".join(parts)


#
# ChatDev-style edge payload processors (v6.1.5)
# ─────────────────────────────────────────────
# Reference: ChatDev 2.0 workflow/graph.py:L373-394 `payload_processor` callback
# on edges. MetaGPT Document.get_meta() — path-only refs. Returning None drops
# the payload entirely (like ChatDev's regex processor returning None).
#
from typing import Callable

PayloadProcessor = Callable[[HandoffPacket], Optional[HandoffPacket]]


def drop_verbose_fields(packet: HandoffPacket) -> HandoffPacket:
    """Strip heavy debug fields; keep brief + files + decisions."""
    stripped = HandoffPacket.from_dict(packet.to_dict())
    stripped.source_bundles = []
    stripped.reference_urls = []
    stripped.design_choices = []
    stripped.rejected_alternatives = []
    stripped.api_patterns = []
    stripped.technologies_used = []
    stripped.token_usage = {}
    stripped.duration_seconds = 0.0
    stripped.tool_calls_count = 0
    stripped.iterations = 0
    return stripped


def keep_file_refs_only(packet: HandoffPacket) -> HandoffPacket:
    """For integrator/merger: only care about WHERE artifacts landed."""
    stripped = HandoffPacket.from_dict(packet.to_dict())
    stripped.decisions_made = []
    stripped.design_choices = []
    stripped.source_bundles = []
    stripped.reference_urls = []
    stripped.open_questions = []
    stripped.context_summary = f"{packet.source_node_type} produced the files listed below."
    return stripped


def summarize_brief(max_len: int = 200) -> PayloadProcessor:
    """Cap context_summary to max_len; OpenHands AgentDelegateAction style."""
    def _proc(packet: HandoffPacket) -> HandoffPacket:
        out = HandoffPacket.from_dict(packet.to_dict())
        s = (out.context_summary or "").strip()
        if len(s) > max_len:
            out.context_summary = s[: max_len - 3] + "..."
        return out
    return _proc


BUILT_IN_PROCESSORS: Dict[str, PayloadProcessor] = {
    "drop_verbose": drop_verbose_fields,
    "file_refs_only": keep_file_refs_only,
    "summarize_200": summarize_brief(200),
    "summarize_400": summarize_brief(400),
}


def apply_edge_processor(
    packet: HandoffPacket,
    processor: Any,
) -> Optional[HandoffPacket]:
    """Apply a processor by name or callable; None return drops packet."""
    if processor is None:
        return packet
    fn: Optional[PayloadProcessor]
    if callable(processor):
        fn = processor
    elif isinstance(processor, str):
        fn = BUILT_IN_PROCESSORS.get(processor)
    else:
        fn = None
    if fn is None:
        return packet
    try:
        return fn(packet)
    except Exception:  # processor bugs must never break pipeline
        logger.exception("edge payload_processor %r failed; passthrough", processor)
        return packet


class HandoffValidator:
    """Validates handoff packets to ensure data quality."""

    @staticmethod
    def validate(packet: HandoffPacket) -> List[str]:
        """Return list of validation warnings (empty = valid)."""
        warnings = []

        if not packet.source_node_type:
            warnings.append("Missing source_node_type")
        if not packet.context_summary:
            warnings.append("Missing context_summary — downstream nodes may lack context")
        if packet.quality_score < 0 or packet.quality_score > 1:
            warnings.append(f"Invalid quality_score: {packet.quality_score}")

        return warnings


class HandoffBuilder:
    """Builder pattern for constructing handoff packets from node execution results."""

    @staticmethod
    def from_agentic_result(
        result: Dict[str, Any],
        source_node: str,
        source_node_type: str,
        target_node: str = "",
        target_node_type: str = "",
        run_id: str = "",
        subtask_id: str = "",
        output_summary: str = "",
        lang: str = "en",
    ) -> HandoffPacket:
        """Build a HandoffPacket from an AgenticLoop result.

        Populates richer fields beyond basic metrics:
        - technologies_used from file extensions
        - decisions_made from thinking traces
        - source_bundles from web_fetch tool results
        - quality_score / confidence_level from exhaustion status
        """
        # ── Detect technologies ──
        tech_set: set = set()
        _tech_map = {
            "html": "HTML5", "css": "CSS3", "js": "JavaScript", "ts": "TypeScript",
            "tsx": "React", "jsx": "React", "py": "Python", "json": "JSON",
            "glsl": "WebGL", "svg": "SVG",
        }
        all_files = list(result.get("files_created", [])) + list(result.get("files_modified", []))
        for f in all_files:
            ext = str(f).rsplit(".", 1)[-1].lower() if "." in str(f) else ""
            if ext in _tech_map:
                tech_set.add(_tech_map[ext])

        # ── Extract decisions from traces ──
        decisions: List[str] = []
        for trace in result.get("traces", []):
            summary = str(trace.get("summary", "")).strip()
            tools = trace.get("tools", [])
            if summary and len(summary) > 30 and tools:
                decisions.append(summary[:200])

        # ── Extract file purposes from tool results ──
        file_purposes: Dict[str, str] = {}
        for tr in result.get("tool_results", []):
            tool_name = str(tr.get("tool") or "").lower()
            args = tr.get("args") if isinstance(tr.get("args"), dict) else {}
            if tool_name in {"file_write", "write_file", "file_edit"}:
                path = str(args.get("path") or args.get("file_path") or "")
                if path:
                    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
                    file_purposes[path] = ext.upper() + " file" if ext else "artifact"

        # ── Quality assessment ──
        exhausted = result.get("exhausted", False)
        # Multi-factor quality scoring
        quality = _compute_quality_score(result, exhausted)
        confidence = "low" if exhausted else ("high" if result.get("success") and quality >= 0.7 else "medium")

        # Build structured context summary instead of raw truncation
        context_summary = str(output_summary or "").strip()
        if not context_summary:
            context_summary = _build_handoff_summary(
                result,
                source_node_type,
                tech_set,
                decisions,
                lang=lang,
            )

        deliverables: List[str] = []
        for item in result.get("files_created", [])[:8]:
            path = str(item or "").strip()
            if not path:
                continue
            purpose = file_purposes.get(path, "")
            label = Path(path).name or path
            deliverables.append(f"{label} — {purpose}" if purpose else label)
        for item in result.get("files_modified", [])[:6]:
            path = str(item or "").strip()
            if not path:
                continue
            purpose = file_purposes.get(path, "")
            label = Path(path).name or path
            text = f"Patched {label}" if lang != "zh" else f"修补 {label}"
            if purpose:
                text = f"{text} — {purpose}"
            deliverables.append(text)

        design_choices = [
            {
                "choice": decision[:120],
                "rationale": (
                    "Derived from tool-guided execution trace"
                    if lang != "zh" else
                    "来自本轮带工具执行的决策轨迹"
                ),
            }
            for decision in decisions[:6]
        ]

        known_issues: List[str] = []
        error_text = str(result.get("error") or "").strip()
        if error_text:
            known_issues.append(error_text[:280])
        if exhausted:
            known_issues.append(
                f"Loop exhausted: {result.get('exhaustion_reason', 'unknown')}"
                if lang != "zh" else
                f"执行被安全阈值截断：{result.get('exhaustion_reason', 'unknown')}"
            )

        packet = HandoffPacket(
            source_node=source_node,
            source_node_type=source_node_type,
            target_node=target_node,
            target_node_type=target_node_type,
            run_id=run_id,
            subtask_id=subtask_id,
            context_summary=context_summary,
            deliverables=deliverables[:12],
            files_produced=[
                {"path": f, "purpose": file_purposes.get(f, ""), "lines": ""}
                for f in result.get("files_created", [])
            ],
            files_modified=[
                {"path": f, "purpose": file_purposes.get(f, ""), "lines": ""}
                for f in result.get("files_modified", [])
            ],
            decisions_made=decisions[:10],
            design_choices=design_choices,
            technologies_used=sorted(tech_set),
            quality_score=quality,
            confidence_level=confidence,
            known_issues=known_issues[:6],
            token_usage=result.get("usage", {}),
            duration_seconds=result.get("duration_seconds", 0),
            tool_calls_count=sum(result.get("tool_call_stats", {}).values()),
            iterations=result.get("iterations", 0),
        )

        # ── Warnings ──
        if exhausted:
            packet.warnings.append(
                f"Loop exhausted ({result.get('exhaustion_reason', 'unknown')}) — output may be incomplete"
            )

        # ── Extract reference URLs and source bundles from fetch/search results ──
        for tr in result.get("tool_results", []):
            tool_name = str(tr.get("tool") or "").strip().lower()
            metadata = tr.get("metadata") if isinstance(tr.get("metadata"), dict) else {}
            if tool_name in {"web_fetch", "web_search", "source_fetch"}:
                args = tr.get("args") if isinstance(tr.get("args"), dict) else {}
                url = str(args.get("url") or "").strip()
                if url and url not in packet.reference_urls:
                    packet.reference_urls.append(url)
                for item in metadata.get("results", [])[:5]:
                    item_url = str(item.get("url") or "").strip()
                    if item_url and item_url not in packet.reference_urls:
                        packet.reference_urls.append(item_url)
                text = str(tr.get("result") or "")
                for found_url in re.findall(r'https?://[^\s<>"\']+', text)[:5]:
                    if found_url not in packet.reference_urls:
                        packet.reference_urls.append(found_url)
                # Build source_bundle entries for web_fetch results
                if url and text:
                    packet.source_bundles.append({
                        "title": url.split("/")[-1][:50] or url[:50],
                        "url": url,
                        "content_summary": text[:500],
                    })
                elif tool_name == "web_search":
                    for item in metadata.get("results", [])[:3]:
                        item_url = str(item.get("url") or "").strip()
                        if not item_url:
                            continue
                        packet.source_bundles.append({
                            "title": str(item.get("title") or item_url)[:80],
                            "url": item_url,
                            "content_summary": str(item.get("snippet") or "")[:500],
                        })

        return packet


# ─── Helpers ─────────────────────────────────────────────


def _compute_quality_score(result: Dict[str, Any], exhausted: bool) -> float:
    """Multi-factor quality score (0.0 - 1.0) for handoff assessment.

    Factors:
    - Base: success=0.6, failure=0.2, exhausted=0.3
    - Bonus: files created (+0.15), tool diversity (+0.1), reasonable iterations (+0.05)
    - Penalty: zero files (-0.1), exhaustion (-0.2)
    """
    if exhausted:
        score = 0.3
    elif result.get("success"):
        score = 0.6
    else:
        score = 0.2

    # File output bonus
    created = len(result.get("files_created", []))
    if created > 0:
        score += min(0.15, created * 0.03)
    else:
        score -= 0.1

    # Tool diversity bonus (using more types = more thorough)
    tool_types = len(result.get("tool_call_stats", {}))
    if tool_types >= 3:
        score += 0.1
    elif tool_types >= 2:
        score += 0.05

    # Iteration efficiency
    iters = result.get("iterations", 0)
    max_iters = 20  # typical max
    if 1 <= iters <= max_iters * 0.7:
        score += 0.05  # used iterations reasonably, didn't exhaust

    return round(min(1.0, max(0.0, score)), 2)


def _build_handoff_summary(
    result: Dict[str, Any],
    source_type: str,
    tech_set: set,
    decisions: List[str],
    lang: str = "en",
) -> str:
    """Build a structured, human-readable handoff summary."""
    zh = lang == "zh"
    lines: list = []
    role_labels = {
        "planner": ("规划师", "Planner"),
        "analyst": ("分析师", "Analyst"),
        "builder": ("构建者", "Builder"),
        "merger": ("合并器", "Merger"),
        "reviewer": ("审查员", "Reviewer"),
        "tester": ("测试员", "Tester"),
        "debugger": ("调试员", "Debugger"),
        "polisher": ("抛光器", "Polisher"),
    }
    role_label = role_labels.get(str(source_type or "").strip().lower(), (source_type or "节点", source_type or "Node"))[0 if zh else 1]

    success = result.get("success", False)
    exhausted = result.get("exhausted", False)
    if success:
        lines.append(
            f"## 交接总览\n{role_label}已经完成本轮工作，当前结果可以作为下游节点的直接起点。"
            if zh else
            f"## Handoff Overview\nThe {role_label} finished this pass and produced a usable starting point for the downstream node."
        )
    elif exhausted:
        lines.append(
            f"## 交接总览\n{role_label}没有完整收口，本轮被 {result.get('exhaustion_reason', 'safety limit')} 截断，下游接手时需要先补齐未完成部分。"
            if zh else
            f"## Handoff Overview\nThe {role_label} did not fully finish; execution was truncated by {result.get('exhaustion_reason', 'safety limit')}. The downstream node should complete the missing work first."
        )
    else:
        lines.append(
            f"## 交接总览\n{role_label}本轮失败，下游节点接手时要先根据错误或回退要求修复。"
            if zh else
            f"## Handoff Overview\nThe {role_label} failed in this pass, so the downstream node should start by repairing the blocking issues."
        )

    created = result.get("files_created", [])
    modified = result.get("files_modified", [])
    if created:
        label = "本轮新增的关键文件" if zh else "Key files created in this pass"
        lines.append(f"\n### {label}")
        for item in created[:8]:
            name = Path(str(item)).name or str(item)
            lines.append(f"- {name}")
    if modified:
        label = "本轮修改的关键文件" if zh else "Key files patched in this pass"
        lines.append(f"\n### {label}")
        for item in modified[:8]:
            name = Path(str(item)).name or str(item)
            lines.append(f"- {name}")

    if tech_set:
        label = "涉及技术" if zh else "Technologies involved"
        lines.append(f"\n### {label}\n{', '.join(sorted(tech_set))}")

    if decisions:
        label = "关键决策" if zh else "Key decisions"
        lines.append(f"\n### {label}")
        for d in decisions[:5]:
            lines.append(f"- {d}")

    stats = result.get("tool_call_stats", {})
    if stats:
        tool_strs = [f"{name}={count}" for name, count in sorted(stats.items()) if count > 0]
        label = "执行方式" if zh else "Execution pattern"
        lines.append(
            f"\n### {label}\n本轮主要使用了 {', '.join(tool_strs)}。"
            if zh else
            f"\n### {label}\nThis pass mainly used {', '.join(tool_strs)}."
        )

    next_step_lines: List[str] = []
    normalized_source = str(source_type or "").strip().lower()
    if normalized_source in {"builder", "merger", "polisher", "debugger"} and (created or modified):
        next_step_lines.append(
            "下游节点应先读取现有文件，在原文件上定点修补，不要忽略当前产物后整段重写。"
            if zh else
            "The downstream node should inspect the current files first and patch them in place instead of rewriting from scratch."
        )
    if normalized_source == "analyst":
        next_step_lines.append(
            "Builder 应先落实这里的技术结论和参考，再开始编码，避免重新搜索和重新定义接口。"
            if zh else
            "The builder should implement these technical findings first instead of re-researching or redefining the contracts."
        )
    if normalized_source == "reviewer":
        next_step_lines.append(
            "上游节点必须逐条兑现回退要求，并保留当前仍然有效的实现，不允许为了修一个问题把整个项目重写。"
            if zh else
            "Upstream nodes must fix each rollback item while preserving the still-working implementation; they should not rewrite the whole project to fix one issue."
        )
    if next_step_lines:
        label = "下游接手建议" if zh else "Downstream guidance"
        lines.append(f"\n### {label}")
        lines.extend(f"- {item}" for item in next_step_lines[:4])

    raw = str(result.get("output", "")).strip()
    if raw and len(raw) > 100:
        label = "补充原始片段" if zh else "Supplementary raw excerpt"
        lines.append(f"\n### {label}\n{raw[-400:]}")
    elif raw:
        lines.append(f"\n{raw}")

    return "\n".join(lines)
