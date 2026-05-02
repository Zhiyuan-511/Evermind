"""
Evermind v3.1 — Professional Report Generator

Generates Cursor/Codex-quality walkthrough reports for every node execution.
Reports are based on actual tool call records and file changes (not model hallucination).

v3.1 overhaul:
  - Mermaid execution flow diagrams (dynamic from actual tool calls)
  - Mermaid Gantt timeline showing parallel/serial execution
  - Layered token/cost breakdown (input/output/cached)
  - Step-by-step execution trace table
  - Reference URLs and code snippets
  - Human-readable summaries (no "AI-generated" boilerplate)

Report structure modeled after Cursor Debug Mode + Langfuse traces:
  1. Header with key metrics (status, duration, tokens, cost)
  2. Execution Flow (Mermaid flowchart)
  3. Step-by-step Activity Log (table)
  4. File Changes with code highlights
  5. Key Decisions & Rationale
  6. Token & Cost Breakdown
  7. Execution Timeline (Mermaid Gantt)
  8. References & Source Links
  9. Collaboration Handoff Notes
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("evermind.report_generator")


@dataclass
class NodeReport:
    """Structured report for a single node's execution."""

    # Identity
    node_label: str = ""
    node_type: str = ""
    node_key: str = ""
    run_id: str = ""
    subtask_id: str = ""
    model_used: str = ""

    # Summary
    task_brief: str = ""
    outcome_summary: str = ""
    success: bool = True

    # Files
    files_created: List[Dict[str, Any]] = field(default_factory=list)
    files_modified: List[Dict[str, Any]] = field(default_factory=list)

    # Decisions
    key_decisions: List[Dict[str, str]] = field(default_factory=list)
    technologies_chosen: List[str] = field(default_factory=list)

    # Tool usage
    tool_call_timeline: List[Dict[str, Any]] = field(default_factory=list)
    tool_call_stats: Dict[str, int] = field(default_factory=dict)

    # References
    search_queries: List[str] = field(default_factory=list)
    reference_urls: List[str] = field(default_factory=list)
    source_bundles_used: int = 0

    # Metrics
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost: float = 0.0
    duration_seconds: float = 0.0
    iterations: int = 0
    context_compressions: int = 0
    api_calls: int = 0
    retries: int = 0

    # Collaboration
    warnings_for_downstream: List[str] = field(default_factory=list)
    review_focus_areas: List[str] = field(default_factory=list)
    merge_notes: List[str] = field(default_factory=list)

    # Timestamps
    started_at: float = 0.0
    completed_at: float = 0.0

    # Code snippets for report
    code_snippets: List[Dict[str, str]] = field(default_factory=list)

    # Antigravity-quality fields
    role_description: str = ""          # 1-2 sentence role description
    fallback_used: str = ""             # Fallback scheme name (e.g. "deterministic skeleton")
    fallback_reason: str = ""           # Reason for fallback
    analysis_narrative: str = ""        # In-depth analysis narrative (extracted from raw_output)


class ReportGenerator:
    """Generates Cursor/Codex-quality walkthrough reports from node execution data."""

    @staticmethod
    def generate(report: NodeReport, lang: str = "en") -> str:
        """Generate a professional markdown walkthrough report."""
        zh = lang == "zh"
        sections: List[str] = []

        # ── Header with Key Metrics ──
        sections.append(_build_header(report, zh))

        # ── Role Description (antigravity) ──
        role_desc = _build_role_description(report.node_type, zh)
        if role_desc:
            sections.append(role_desc)

        # ── Degradation Notice (antigravity) ──
        deg_notice = _build_degradation_notice(report, zh)
        if deg_notice:
            sections.append(deg_notice)

        # ── Execution Flow (Mermaid) ──
        flow = _generate_execution_flow(report, zh)
        if flow:
            sections.append(
                f"\n## {'执行流程' if zh else 'Execution Flow'}\n\n```mermaid\n{flow}\n```"
            )

        # ── Outcome Summary ──
        if report.outcome_summary:
            sections.append(
                f"\n## {'执行结果' if zh else 'Outcome'}\n\n{report.outcome_summary}"
            )

        # ── Step-by-step Activity Log ──
        if report.tool_call_timeline:
            sections.append(_build_activity_table(report, zh))

        # ── File Changes ──
        if report.files_created or report.files_modified:
            sections.append(_build_file_changes(report, zh))

        # ── Code Snippets (v7.0 maintainer) ──
        # Only surface code for nodes that actually WRITE code. For
        # research/review nodes (analyst, planner, reviewer, tester, scribe,
        # deployer, uidesign) the walkthrough should be narrative prose —
        # real code belongs in the dedicated research dossier / handoff
        # artifact, not the walkthrough. Observed analyst walkthroughs
        # dumping <cross_node_contract> YAML blocks as "core code", which
        # confuses the story-telling purpose of the walkthrough.
        _CODE_WRITER_NODES = {
            "builder", "merger", "polisher", "patcher", "debugger",
            "imagegen", "spritesheet", "assetimport",
        }
        _nt_norm = str(report.node_type or "").strip().lower()
        _node_key_norm = str(report.node_key or "").strip().lower()
        # builder1/builder2/merger etc. all collapse to "builder" family
        _is_code_writer = any(
            _nt_norm == k or _node_key_norm.startswith(k) or _nt_norm.startswith(k)
            for k in _CODE_WRITER_NODES
        )
        if report.code_snippets and _is_code_writer:
            sections.append(_build_code_snippets(report, zh))

        # ── Key Decisions ──
        if report.key_decisions:
            sections.append(_build_decisions(report, zh))

        # ── Token & Cost Breakdown ──
        if report.total_tokens > 0 or report.prompt_tokens > 0:
            sections.append(_build_token_breakdown(report, zh))

        # ── Execution Timeline (Gantt) ──
        gantt = _generate_gantt(report, zh)
        if gantt:
            sections.append(
                f"\n## {'执行时间线' if zh else 'Execution Timeline'}\n\n```mermaid\n{gantt}\n```"
            )

        # ── Tool Usage Statistics ──
        if report.tool_call_stats:
            sections.append(_build_tool_stats(report, zh))

        # ── References ──
        if report.reference_urls or report.search_queries:
            sections.append(_build_references(report, zh))

        # ── Collaboration Handoff ──
        has_collab = report.warnings_for_downstream or report.review_focus_areas or report.merge_notes
        if has_collab:
            sections.append(_build_collaboration(report, zh))

        return "\n".join(sections)

    @staticmethod
    def from_agentic_result(
        result: Dict[str, Any],
        node_label: str,
        node_type: str,
        node_key: str = "",
        run_id: str = "",
        subtask_id: str = "",
        model_used: str = "",
        task_brief: str = "",
        lang: str = "en",
    ) -> NodeReport:
        """Build a NodeReport from an AgenticLoop result dict."""
        # ── Infer file purposes from tool_results ──
        file_purposes: Dict[str, str] = {}
        code_snippets: List[Dict[str, str]] = []
        for tr in result.get("tool_results", []):
            tool_name = str(tr.get("tool") or "").lower()
            args = tr.get("args") if isinstance(tr.get("args"), dict) else {}
            # V4.2 FIX (Codex #4): bridge emits writes as file_ops with action=write
            _is_write = (
                tool_name in {"file_write", "write_file", "file_edit"}
                or (tool_name == "file_ops" and str(args.get("action") or "").lower() == "write")
            )
            if _is_write:
                path = str(args.get("path") or args.get("file_path") or "")
                content = str(args.get("content") or args.get("new_string") or "")
                if path:
                    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
                    first_lines = content[:300].strip()
                    purpose = _infer_file_purpose(path, ext, first_lines, lang)
                    file_purposes[path] = purpose
                    # V4.3: Extract focused code snippets — only the most
                    # representative 8 lines, not raw dumps of entire files.
                    if content and len(content) > 100:
                        snippet_lines = content.split("\n")
                        # Find the most interesting section: look for class/function
                        # definitions, event handlers, or main logic blocks
                        _best_start = 0
                        for idx, ln in enumerate(snippet_lines[:60]):
                            _stripped = ln.strip().lower()
                            if any(kw in _stripped for kw in (
                                "function ", "class ", "const ", "def ",
                                "export ", "addEventListener", "init(", "setup(",
                                "createscene", "animate(", "render(",
                            )):
                                _best_start = idx
                                break
                        meaningful = [l for l in snippet_lines[_best_start:_best_start + 20] if l.strip()][:8]
                        if meaningful:
                            code_snippets.append({
                                "file": path.rsplit("/", 1)[-1] if "/" in path else path,
                                "language": _ext_to_language(ext),
                                "code": "\n".join(meaningful),
                                "purpose": purpose,
                            })

        # ── Detect technologies ──
        tech_set: set = set()
        all_files = list(result.get("files_created", [])) + list(result.get("files_modified", []))
        for f in all_files:
            ext = str(f).rsplit(".", 1)[-1].lower() if "." in str(f) else ""
            tech = _ext_to_tech(ext)
            if tech:
                tech_set.add(tech)

        # ── Count API calls and retries ──
        api_calls = len([
            tr for tr in result.get("tool_results", [])
            if str(tr.get("tool") or "").lower() not in {"context_compress"}
        ])

        usage = result.get("usage", {}) or {}

        report = NodeReport(
            node_label=node_label,
            node_type=node_type,
            node_key=node_key,
            run_id=run_id,
            subtask_id=subtask_id,
            model_used=model_used,
            task_brief=task_brief,
            outcome_summary=_build_structured_summary(result, node_type, task_brief, file_purposes, tech_set, lang),
            success=result.get("success", False),
            files_created=[
                {"path": f, "purpose": file_purposes.get(f, ""), "lines_added": _count_lines(result, f)}
                for f in result.get("files_created", [])
            ],
            files_modified=[
                {"path": f, "purpose": file_purposes.get(f, ""), "lines_changed": ""}
                for f in result.get("files_modified", [])
            ],
            technologies_chosen=sorted(tech_set),
            tool_call_stats=result.get("tool_call_stats", {}),
            search_queries=result.get("search_queries", []),
            total_tokens=usage.get("total_tokens", 0),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            cached_tokens=usage.get("cached_tokens", 0) or usage.get("cache_read_input_tokens", 0),
            cost=result.get("cost", 0.0) or 0.0,
            duration_seconds=result.get("duration_seconds", 0),
            iterations=result.get("iterations", 0),
            context_compressions=sum(result.get("context_compressions", {}).values()) if isinstance(result.get("context_compressions"), dict) else 0,
            api_calls=api_calls,
            retries=result.get("retries", 0),
            started_at=result.get("started_at", 0),
            completed_at=result.get("completed_at", 0) or time.time(),
            code_snippets=code_snippets[:5],  # Max 5 snippets
            # Antigravity-quality fields
            fallback_used=str(result.get("fallback_used", "") or ""),
            fallback_reason=str(result.get("fallback_reason", "") or ""),
            analysis_narrative=str(result.get("analysis_narrative", "") or ""),
        )

        # ── Build tool call timeline ──
        for tr in result.get("tool_results", []):
            report.tool_call_timeline.append({
                "ts": tr.get("started_at", 0),
                "tool": tr.get("tool", "?"),
                "description": _describe_tool_call(tr, lang),
                "duration_ms": tr.get("duration_ms", 0),
                "success": tr.get("error") is None,
            })
            # Extract reference URLs
            tool_name = str(tr.get("tool") or "").strip().lower()
            if tool_name in {"web_fetch", "web_search", "source_fetch"}:
                args = tr.get("args") if isinstance(tr.get("args"), dict) else {}
                metadata = tr.get("metadata") if isinstance(tr.get("metadata"), dict) else {}
                url = str(args.get("url") or "").strip()
                if url and url not in report.reference_urls:
                    report.reference_urls.append(url)
                for item in metadata.get("results", [])[:8]:
                    item_url = str(item.get("url") or "").strip()
                    if item_url and item_url not in report.reference_urls:
                        report.reference_urls.append(item_url)

        # ── Extract key decisions ──
        for trace in result.get("traces", []):
            summary = str(trace.get("summary", "")).strip()
            if summary and len(summary) > 30:
                tools_used = trace.get("tools", [])
                if tools_used:
                    report.key_decisions.append({
                        "decision": summary[:200],
                        "rationale": f"Used {', '.join(tools_used[:3])} tools"
                    })

        # ── Warnings and review areas ──
        if result.get("exhausted"):
            report.warnings_for_downstream.append(
                f"Loop exhausted: {result.get('exhaustion_reason', 'unknown')} — output may be incomplete"
            )
        if result.get("context_compressions", {}).get("full", 0) > 0:
            report.warnings_for_downstream.append(
                "Full context collapse occurred — early context details may be lost"
            )
        if not result.get("files_created"):
            report.review_focus_areas.append(
                "No new files created — verify if the task required file output"
            )

        return report


# ─── Section Builders ─────────────────────────────────────────

# ── Role Descriptions (antigravity standard) ──
_ROLE_DESCRIPTIONS = {
    "planner": {
        "zh": ("流水线编排器", "负责将用户目标分解为可执行的 DAG 节点序列。产出包括架构设计、模块拆分、Builder 所有权映射和子系统契约。下游所有节点的执行质量直接取决于规划的精度。"),
        "en": ("Pipeline Orchestrator", "Responsible for decomposing user goals into an executable DAG node sequence. Produces architecture design, module breakdown, Builder ownership maps, and subsystem contracts. Execution quality of all downstream nodes depends directly on planning precision."),
    },
    "analyst": {
        "zh": ("研究分析师", "对目标领域进行深度调研，收集参考资料、竞品分析和可行性评估。产出结构化交接文档，为 Builder 提供精确的技术方向、参考代码片段和风险预警。"),
        "en": ("Research Analyst", "Conducts deep research on the target domain, gathering references, competitive analysis, and feasibility assessments. Produces structured handoff documents with precise technical direction, reference code snippets, and risk warnings for Builders."),
    },
    "builder": {
        "zh": ("代码构建者", "根据规划和分析结果编写完整的生产级代码。严格遵循所有权映射，只修改分配给自己的文件。产出经过自验证的、可运行的代码模块。"),
        "en": ("Code Builder", "Writes complete production-grade code based on planning and analysis results. Strictly follows ownership mapping, only modifying assigned files. Produces self-verified, runnable code modules."),
    },
    "merger": {
        "zh": ("代码集成器", "将多个 Builder 的并行产物合并为统一的可运行应用。解决命名冲突、去重 CSS/JS、统一导航和状态管理，确保合并后的代码无回归。"),
        "en": ("Code Integrator", "Merges parallel outputs from multiple Builders into a unified runnable application. Resolves naming conflicts, deduplicates CSS/JS, unifies navigation and state management, ensuring no regressions post-merge."),
    },
    "reviewer": {
        "zh": ("质量审查员", "对合并后的产物进行多维度评分（布局、配色、排版、动画、响应式、功能完整性、原创性）。评分低于阈值时自动拦截，要求 Builder 返工。"),
        "en": ("Quality Reviewer", "Performs multi-dimensional scoring of merged output (layout, color, typography, animation, responsive, functionality, completeness, originality). Auto-blocks when scores fall below threshold, requiring Builder rework."),
    },
    "tester": {
        "zh": ("端到端测试员", "通过浏览器实际加载和交互验证产物。执行结构检查、视觉测试、多页完整性、性能信号和可访问性审查。必须提供截图证据。"),
        "en": ("End-to-End Tester", "Validates output through actual browser loading and interaction. Performs structural checks, visual tests, multi-page completeness, performance signals, and accessibility review. Must provide screenshot evidence."),
    },
    "debugger": {
        "zh": ("故障修复员", "定位并修复 Tester/Reviewer 发现的缺陷。每次修复包含根因分析、具体改动说明、验证证据和副作用风险评估。"),
        "en": ("Fault Resolver", "Locates and fixes defects found by Tester/Reviewer. Each fix includes root cause analysis, specific change description, verification evidence, and side-effect risk assessment."),
    },
    "polisher": {
        "zh": ("质量打磨器", "对通过审查的产物进行最终优化：性能调优、视觉微调、代码清理和边缘场景加固，不改变核心功能。"),
        "en": ("Quality Polisher", "Performs final optimization on reviewed output: performance tuning, visual refinement, code cleanup, and edge-case hardening without altering core functionality."),
    },
    "deployer": {
        "zh": ("部署执行员", "将最终产物部署到预览/生产环境，生成部署回执和访问链接。"),
        "en": ("Deployment Executor", "Deploys final output to preview/production environments, generating deployment receipts and access links."),
    },
    "imagegen": {
        "zh": ("视觉资产生成器", "根据设计规范生成图像/精灵/纹理等视觉资产，包含设计文档和使用说明。"),
        "en": ("Visual Asset Generator", "Generates images/sprites/textures and other visual assets according to design specs, including design docs and usage instructions."),
    },
    "spritesheet": {
        "zh": ("精灵表处理器", "将多个独立图像合并为优化的精灵表，生成对应的 CSS/JSON 映射。"),
        "en": ("Spritesheet Processor", "Combines multiple individual images into optimized spritesheets, generating corresponding CSS/JSON mappings."),
    },
}


def _build_role_description(node_type: str, zh: bool) -> str:
    """Build a role description paragraph for the node type."""
    role = str(node_type or "").strip().lower()
    desc = _ROLE_DESCRIPTIONS.get(role)
    if not desc:
        return ""
    label, narrative = desc["zh" if zh else "en"]
    return f"\n### {'角色' if zh else 'Role'}: {label}\n\n{narrative}"


def _build_degradation_notice(report: NodeReport, zh: bool) -> str:
    """Build degradation notice when fallback mechanisms were used."""
    if not report.fallback_used:
        return ""
    title = "### 降级通知" if zh else "### Degradation Notice"
    if zh:
        body = f"在线 {report.node_label} 未能在规定时间内返回结构化输出。本次使用了 **{report.fallback_used}** 作为降级方案。"
    else:
        body = f"Online {report.node_label} did not return structured output in time. A **{report.fallback_used}** was used as fallback."
    if report.fallback_reason:
        body += f"\n\n{'原因' if zh else 'Reason'}: {report.fallback_reason}"
    return f"\n{title}\n\n{body}"


def _build_header(report: NodeReport, zh: bool) -> str:
    """Build antigravity-style header with Execution Snapshot table."""
    status_icon = "Passed" if report.success else "Failed"
    duration = _format_duration(report.duration_seconds)
    tokens = _format_tokens(report.total_tokens)
    cost = f"${report.cost:.4f}" if report.cost > 0 else "—"
    model = report.model_used or "—"

    title = f"# {report.node_label} — {'执行报告' if zh else 'Execution Report'}"

    # Executive one-liner
    lines = [title, ""]
    if report.task_brief:
        brief = report.task_brief[:300]
        lines.append(f"> {brief}")
        lines.append("")

    # Execution Snapshot table
    lines.append(f"## {'执行快照' if zh else 'Execution Snapshot'}")
    lines.append("")
    lines.append(f"| {'指标' if zh else 'Metric'} | {'值' if zh else 'Value'} |")
    lines.append("|--------|-------|")
    lines.append(f"| {'状态' if zh else 'Status'} | {status_icon} |")
    lines.append(f"| {'耗时' if zh else 'Duration'} | {duration} |")
    lines.append(f"| {'模型' if zh else 'Model'} | `{model}` |")
    if report.total_tokens > 0:
        detail = f" (in: {_format_tokens(report.prompt_tokens)} / out: {_format_tokens(report.completion_tokens)})"
        lines.append(f"| Token | {tokens}{detail} |")
    if report.cost > 0:
        lines.append(f"| {'费用' if zh else 'Cost'} | {cost} |")
    if report.retries > 0:
        lines.append(f"| {'重试' if zh else 'Retries'} | {report.retries} |")
    if report.iterations > 0:
        lines.append(f"| {'迭代' if zh else 'Iterations'} | {report.iterations} |")

    return "\n".join(lines)


def _build_activity_table(report: NodeReport, zh: bool) -> str:
    """Build a step-by-step activity log as a table.

    V4.2: Enhanced to handle entries with missing 'tool' or 'ts' fields
    by inferring tool name from 'path'/'written' keys and computing
    relative timestamps from the first entry.
    """
    if zh:
        header = "\n## 执行轨迹\n"
        cols = "| # | 时间 | 工具 | 操作 | 耗时 | 状态 |"
    else:
        header = "\n## Execution Trace\n"
        cols = "| # | Time | Tool | Action | Duration | Status |"
    sep = "|---|------|------|--------|----------|--------|"

    rows = [header, cols, sep]
    timeline = report.tool_call_timeline[:30]  # Max 30 steps

    # Compute base timestamp for relative display
    base_ts = 0.0
    for entry in timeline:
        t = float(entry.get("ts", 0) or 0)
        if t > 0:
            base_ts = t
            break
    if not base_ts and report.started_at:
        base_ts = float(report.started_at)

    for i, entry in enumerate(timeline, 1):
        raw_ts = float(entry.get("ts", 0) or 0)
        if raw_ts > 0 and base_ts > 0:
            ts = _format_time(raw_ts)
        elif base_ts > 0:
            # Estimate from position
            ts = _format_time(base_ts + i * 2)
        else:
            ts = f"+{i}"

        # Infer tool name from multiple sources
        tool = str(entry.get("tool") or "").strip()
        if not tool:
            # Fallback: infer from entry shape
            if entry.get("written") or entry.get("path"):
                tool = "file_ops"
            elif entry.get("url"):
                tool = "web_fetch"
            else:
                tool = "unknown"
        icon = _tool_icon(tool)

        desc = entry.get("description", "")
        if not desc:
            # Build description from available data
            desc = _describe_tool_call(entry, "zh" if zh else "en")
        desc = desc[:60]

        dur = int(entry.get("duration_ms", 0) or 0)
        if dur > 1000:
            dur_text = f"{dur / 1000:.1f}s"
        elif dur > 0:
            dur_text = f"{dur}ms"
        else:
            dur_text = "—"
        status = "done" if entry.get("success", True) else "**FAIL**"
        rows.append(f"| {i} | `{ts}` | {icon} {tool} | {desc} | {dur_text} | {status} |")

    if len(report.tool_call_timeline) > 30:
        remaining = len(report.tool_call_timeline) - 30
        rows.append(f"\n*...{'还有' if zh else 'and'} {remaining} {'步' if zh else 'more steps'}*")

    return "\n".join(rows)


def _build_file_changes(report: NodeReport, zh: bool) -> str:
    """Build file changes section with line counts."""
    lines = [f"\n## {'文件变更' if zh else 'File Changes'}\n"]

    if report.files_created:
        lines.append(f"### {'新建文件' if zh else 'Created'}\n")
        for f in report.files_created:
            path = f.get("path", "?") if isinstance(f, dict) else str(f)
            purpose = f.get("purpose", "") if isinstance(f, dict) else ""
            line_count = f.get("lines_added", "") if isinstance(f, dict) else ""
            filename = path.rsplit("/", 1)[-1] if "/" in path else path
            parts = [f"- `{filename}`"]
            if purpose:
                parts.append(f"— {purpose}")
            if line_count:
                parts.append(f"({line_count} {'行' if zh else 'lines'})")
            lines.append(" ".join(parts))

    if report.files_modified:
        lines.append(f"\n### {'修改文件' if zh else 'Modified'}\n")
        for f in report.files_modified:
            path = f.get("path", "?") if isinstance(f, dict) else str(f)
            purpose = f.get("purpose", "") if isinstance(f, dict) else ""
            filename = path.rsplit("/", 1)[-1] if "/" in path else path
            parts = [f"- `{filename}`"]
            if purpose:
                parts.append(f"— {purpose}")
            lines.append(" ".join(parts))

    return "\n".join(lines)


def _build_code_snippets(report: NodeReport, zh: bool) -> str:
    """Show key code snippets — focused on core logic, not raw dumps.

    V4.3: Reduced from 5 snippets of 15 lines to 3 snippets of 8 lines,
    with context explaining why each snippet matters.
    """
    lines = [f"\n## {'核心代码片段' if zh else 'Core Code Highlights'}\n"]
    _intro = ("以下是本节点产出中最能体现核心逻辑的代码片段：" if zh
              else "Key code sections that best represent this node's core logic:")
    lines.append(_intro + "\n")
    for snippet in report.code_snippets[:3]:
        file_name = snippet.get("file", "?")
        language = snippet.get("language", "")
        code = snippet.get("code", "")
        purpose = snippet.get("purpose", "")
        if purpose:
            lines.append(f"**`{file_name}`** — {purpose}")
        else:
            lines.append(f"**`{file_name}`**")
        lines.append(f"```{language}\n{code}\n```\n")
    return "\n".join(lines)


def _build_decisions(report: NodeReport, zh: bool) -> str:
    """Build key decisions section."""
    lines = [f"\n## {'关键决策' if zh else 'Key Decisions'}\n"]
    for i, d in enumerate(report.key_decisions[:8], 1):
        choice = d.get("choice") or d.get("decision") or d.get("title") or ""
        rationale = d.get("rationale") or d.get("why") or ""
        lines.append(f"**{i}. {choice}**")
        if rationale:
            lines.append(f"> {rationale}\n")
    return "\n".join(lines)


def _build_token_breakdown(report: NodeReport, zh: bool) -> str:
    """Build layered token/cost breakdown table."""
    if zh:
        header = "\n## Token 消耗明细\n"
        cols = "| 类型 | Token 数 | 占比 | 费用 |"
    else:
        header = "\n## Token Usage Breakdown\n"
        cols = "| Type | Tokens | Share | Cost |"
    sep = "|------|---------|-------|------|"

    total = max(report.total_tokens, report.prompt_tokens + report.completion_tokens, 1)
    prompt_pct = f"{(report.prompt_tokens * 100 / total):.0f}%" if report.prompt_tokens else "—"
    comp_pct = f"{(report.completion_tokens * 100 / total):.0f}%" if report.completion_tokens else "—"

    rows = [header, cols, sep]
    rows.append(f"| {'输入' if zh else 'Input (Prompt)'} | {_format_tokens(report.prompt_tokens)} | {prompt_pct} | — |")
    rows.append(f"| {'输出' if zh else 'Output (Completion)'} | {_format_tokens(report.completion_tokens)} | {comp_pct} | — |")

    if report.cached_tokens > 0:
        cached_pct = f"{(report.cached_tokens * 100 / total):.0f}%"
        rows.append(f"| {'缓存命中' if zh else 'Cached'} | {_format_tokens(report.cached_tokens)} | {cached_pct} | {'(已节省)' if zh else '(saved)'} |")

    cost_str = f"**${report.cost:.4f}**" if report.cost > 0 else "—"
    rows.append(f"| **{'合计' if zh else 'Total'}** | **{_format_tokens(report.total_tokens)}** | **100%** | {cost_str} |")

    # Additional metrics
    extras = []
    if report.context_compressions > 0:
        extras.append(f"{'上下文压缩' if zh else 'Context compressions'}: {report.context_compressions}")
    if report.api_calls > 0:
        extras.append(f"{'API 调用' if zh else 'API calls'}: {report.api_calls}")
    if report.retries > 0:
        extras.append(f"{'重试次数' if zh else 'Retries'}: {report.retries}")
    if extras:
        rows.append(f"\n*{' | '.join(extras)}*")

    return "\n".join(rows)


def _build_tool_stats(report: NodeReport, zh: bool) -> str:
    """Build tool usage statistics."""
    if zh:
        lines = ["\n## 工具使用统计\n", "| 工具 | 调用次数 | 占比 |"]
    else:
        lines = ["\n## Tool Usage Statistics\n", "| Tool | Calls | Share |"]
    lines.append("|------|--------|------|")

    total_calls = max(sum(report.tool_call_stats.values()), 1)
    for tool, count in sorted(report.tool_call_stats.items(), key=lambda x: -x[1]):
        pct = f"{(count * 100 / total_calls):.0f}%"
        lines.append(f"| {_tool_icon(tool)} {tool} | {count} | {pct} |")

    return "\n".join(lines)


def _build_references(report: NodeReport, zh: bool) -> str:
    """Build references section."""
    lines = [f"\n## {'参考来源' if zh else 'References'}\n"]

    if report.search_queries:
        lines.append(f"### {'搜索查询' if zh else 'Search Queries'}\n")
        for q in report.search_queries[:10]:
            lines.append(f'- "{q}"')

    if report.reference_urls:
        lines.append(f"\n### {'参考链接' if zh else 'Reference URLs'}\n")
        for url in report.reference_urls[:15]:
            # Show domain for readability
            domain = re.search(r'https?://([^/]+)', url)
            domain_str = f" ({domain.group(1)})" if domain else ""
            lines.append(f"- [{url[:80]}]({url}){domain_str}")

    return "\n".join(lines)


def _build_collaboration(report: NodeReport, zh: bool) -> str:
    """Build collaboration handoff notes."""
    lines = [f"\n## {'协作交接' if zh else 'Handoff Notes'}\n"]

    if report.warnings_for_downstream:
        for w in report.warnings_for_downstream:
            lines.append(f"- {w}")

    if report.review_focus_areas:
        lines.append(f"\n### {'审查重点' if zh else 'Review Focus'}\n")
        for area in report.review_focus_areas:
            lines.append(f"- {area}")

    if report.merge_notes:
        lines.append(f"\n### {'合并注意' if zh else 'Merge Notes'}\n")
        for note in report.merge_notes:
            lines.append(f"- {note}")

    return "\n".join(lines)


# ─── Mermaid Generators ─────────────────────────────────────────

def _generate_execution_flow(report: NodeReport, zh: bool) -> str:
    """Generate a Mermaid flowchart showing the node's actual execution flow."""
    if not report.tool_call_timeline and not report.files_created:
        return ""

    lines = ["flowchart TD"]

    # Start node
    task_label = report.task_brief[:40].replace('"', "'") if report.task_brief else report.node_type
    lines.append(f'    START(["{report.node_label}"])')

    # Group tool calls by type for a cleaner flow
    tool_groups: Dict[str, int] = {}
    for entry in report.tool_call_timeline:
        tool = entry.get("tool", "unknown")
        tool_groups[tool] = tool_groups.get(tool, 0) + 1

    prev_node = "START"
    node_idx = 0

    # Show distinct tool phases
    for tool, count in tool_groups.items():
        icon = _tool_icon(tool)
        node_id = f"T{node_idx}"
        if count > 1:
            label = f"{icon} {tool} x{count}"
        else:
            label = f"{icon} {tool}"
        lines.append(f'    {node_id}["{label}"]')
        lines.append(f"    {prev_node} --> {node_id}")
        prev_node = node_id
        node_idx += 1

    # Output files
    if report.files_created:
        for i, f in enumerate(report.files_created[:6]):
            path = f.get("path", "?") if isinstance(f, dict) else str(f)
            filename = path.rsplit("/", 1)[-1] if "/" in path else path
            file_id = f"F{i}"
            lines.append(f'    {file_id}[/"{filename}"/]')
            lines.append(f"    {prev_node} --> {file_id}")

    # Result
    if report.success:
        lines.append(f'    RESULT(["PASS"]):::passStyle')
        lines.append(f"    {prev_node} --> RESULT")
        lines.append("    classDef passStyle fill:#10b981,color:#fff,stroke:#059669")
    else:
        lines.append(f'    RESULT(["FAIL"]):::failStyle')
        lines.append(f"    {prev_node} --> RESULT")
        lines.append("    classDef failStyle fill:#ef4444,color:#fff,stroke:#dc2626")

    lines.append("    classDef default fill:#3b82f6,color:#fff,stroke:#2563eb")

    return "\n".join(lines)


def _generate_gantt(report: NodeReport, zh: bool) -> str:
    """Generate a Mermaid Gantt chart showing execution timeline."""
    if not report.tool_call_timeline or len(report.tool_call_timeline) < 2:
        return ""

    # Only generate for timelines with actual timestamps
    valid_entries = [e for e in report.tool_call_timeline if e.get("ts", 0) > 0]
    if len(valid_entries) < 2:
        return ""

    base_ts = valid_entries[0]["ts"]
    lines = [
        "gantt",
        f"    title {'执行时间线' if zh else 'Execution Timeline'}",
        "    dateFormat X",
        '    axisFormat %Ss',
        f"    section {report.node_label}",
    ]

    for entry in valid_entries[:20]:
        tool = entry.get("tool", "?")
        # v3.1: dateFormat X expects seconds, not milliseconds.
        # Use integer seconds for start offset, duration in seconds (min 1s for visibility).
        start = int(entry["ts"] - base_ts)
        dur_ms = max(entry.get("duration_ms", 100), 100)
        dur = max(dur_ms // 1000, 1)  # At least 1 second for Gantt visibility
        status = "done," if entry.get("success", True) else "crit,"
        lines.append(f"    {tool} :{status} {start}, {start + dur}")

    return "\n".join(lines)


# ─── Helpers ─────────────────────────────────────────

def _extract_narrative_sections(raw_output: str, max_length: int = 2000) -> Dict[str, str]:
    """Extract structured sections from raw_output using markdown headers.

    Returns a dict mapping section names (lowercased) to their content.
    Handles both ## and ### level headers.
    """
    sections: Dict[str, str] = {}
    current_section = ""
    current_lines: list = []

    for line in raw_output.split("\n"):
        stripped = line.strip()
        if stripped.startswith(("## ", "### ")):
            # Save previous section
            if current_section and current_lines:
                content = "\n".join(current_lines).strip()
                if len(content) > 20:
                    sections[current_section] = content[:max_length]
            current_section = stripped.lstrip("#").strip().lower()
            current_lines = []
        else:
            current_lines.append(line)

    # Save last section
    if current_section and current_lines:
        content = "\n".join(current_lines).strip()
        if len(content) > 20:
            sections[current_section] = content[:max_length]

    return sections


def _extract_key_sentences(text: str, keywords: List[str], max_sentences: int = 5) -> List[str]:
    """Extract sentences containing any of the keywords from text."""
    sentences = re.split(r'[。.!！?\n]', text)
    matched = []
    for s in sentences:
        s = s.strip()
        if len(s) < 15:
            continue
        lower_s = s.lower()
        if any(kw.lower() in lower_s for kw in keywords):
            matched.append(s)
            if len(matched) >= max_sentences:
                break
    return matched


def _build_structured_summary(
    result: Dict[str, Any],
    node_type: str,
    task_brief: str,
    file_purposes: Dict[str, str],
    tech_set: set,
    lang: str = "en",
) -> str:
    """Build a rich, node-type-specific outcome summary with real data extraction."""
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
        "polisher": ("优化器", "Polisher"),
        "deployer": ("部署员", "Deployer"),
        "scribe": ("文档节点", "Scribe"),
        "uidesign": ("设计节点", "UI Design"),
        "imagegen": ("图像生成", "Image Gen"),
        "spritesheet": ("精灵表", "Spritesheet"),
        "assetimport": ("资产导入", "Asset Import"),
    }
    role = str(node_type or "").strip().lower()
    role_label = role_labels.get(role, (node_type, node_type))[0 if zh else 1]

    success = result.get("success", False)
    exhausted = result.get("exhausted", False)
    created = result.get("files_created", [])
    raw_output = str(result.get("output", "") or "")
    lower = raw_output.lower()
    tool_results = result.get("tool_results", [])

    # ── Status line — v4.0: Use work_summary for specifics, not generic template ──
    _work_items = result.get("work_summary", []) if isinstance(result.get("work_summary"), list) else []
    if success:
        if _work_items:
            # Use the first 3 work items for a specific, data-driven opening
            _highlights = "；".join(str(w) for w in _work_items[:3])
            lines.append(f"**{role_label}** {'本轮完成：' if zh else 'completed: '}{_highlights}。")
        else:
            lines.append(f"**{role_label}** {'已完成全部职责。' if zh else 'completed all responsibilities.'}")
    elif exhausted:
        reason = result.get("exhaustion_reason", "unknown")
        lines.append(f"**{role_label}** {'在 ' + reason + ' 处被截断。' if zh else 'truncated at ' + reason + '.'}")
    else:
        lines.append(f"**{role_label}** {'执行失败。' if zh else 'failed.'}")

    # ── Task brief context ──
    if task_brief and len(task_brief) > 10:
        _brief = task_brief[:200].replace('\n', ' ')
        lines.append(f"\n{'任务目标' if zh else 'Objective'}: {_brief}")

    # ── File inventory with purposes ──
    if created:
        lines.append(f"\n### {'交付清单' if zh else 'Deliverables'} ({len(created)} {'个文件' if zh else 'files'})")
        for fpath in created[:8]:
            fname = fpath.rsplit('/', 1)[-1] if '/' in fpath else fpath
            purpose = file_purposes.get(fpath, "")
            line_count = _count_lines(result, fpath)
            size_hint = f" ({line_count} lines)" if line_count else ""
            if purpose:
                lines.append(f"- `{fname}`{size_hint} — {purpose}")
            else:
                lines.append(f"- `{fname}`{size_hint}")

    # ── Tech stack ──
    if tech_set:
        lines.append(f"\n{'技术栈' if zh else 'Stack'}: {', '.join(sorted(tech_set))}")

    # ── Node-type specific sections ──
    # v4.0: Prefer AI-written execution report over template-generated sections.
    _ai_report = str(result.get("ai_execution_report") or "").strip()
    # V4.5: Lowered threshold 200→60 — even short AI reports are better than template fallback
    if len(_ai_report) > 60:
        lines.append("")
        lines.append(_ai_report)
    else:
        # Graceful fallback: use regex-based template extraction
        _type_section = _build_role_specific_section(role, result, zh=zh)
        if _type_section:
            lines.append("")
            lines.append(_type_section)

    # ── Execution metrics ──
    _duration = result.get("duration_seconds", 0)
    _model = str(result.get("model") or result.get("model_used") or "").strip()
    _iterations = result.get("iterations", 0)
    _retries = result.get("retries", 0)
    _metrics_parts = []
    if _model:
        _metrics_parts.append(f"{'模型' if zh else 'Model'}: `{_model}`")
    if _duration:
        _metrics_parts.append(f"{'耗时' if zh else 'Duration'}: {_duration:.1f}s")
    if _iterations:
        _metrics_parts.append(f"{'迭代' if zh else 'Iterations'}: {_iterations}")
    if _retries:
        _metrics_parts.append(f"{'重试' if zh else 'Retries'}: {_retries}")
    if _metrics_parts:
        lines.append(f"\n{'**执行指标**' if zh else '**Execution Metrics**'}: {' · '.join(_metrics_parts)}")

    # ── Tool usage metrics — enriched ──
    if tool_results:
        tool_types = {}
        for tr in tool_results:
            tname = str(tr.get("tool") or tr.get("name") or "")
            if not tname:
                # Infer from entry shape
                if tr.get("written") or tr.get("path"):
                    tname = "file_ops"
                else:
                    tname = "unknown"
            tool_types[tname] = tool_types.get(tname, 0) + 1
        if tool_types:
            top_tools = sorted(tool_types.items(), key=lambda x: -x[1])[:6]
            tool_str = ", ".join(f"{_tool_icon(t)} {t}×{c}" for t, c in top_tools)
            lines.append(f"\n{'工具调用' if zh else 'Tool Usage'}: {tool_str} ({'共' if zh else 'total'} {len(tool_results)} {'次' if zh else 'calls'})")

    # ── Error detail (only on failure) ──
    if result.get("error") and not success:
        lines.append(f"\n{'错误详情' if zh else 'Error'}: {str(result['error'])[:400]}")

    return "\n".join(lines)


def _build_role_specific_section(role: str, result: Dict[str, Any], *, zh: bool = True) -> str:
    """Generate node-type-specific deep narrative based on actual execution data.

    Antigravity-quality: extracts real content from raw_output using structured
    section parsing, keyword sentence extraction, and JSON parsing (reviewer).
    Falls back to regex counting only as last resort.
    """
    raw = str(result.get("output", "") or "")
    lower = raw.lower()
    created = result.get("files_created", [])
    sections = _extract_narrative_sections(raw)

    if role == "planner":
        parts = []

        # Architecture decisions — extract from structured sections (multiple keys)
        _arch_found = False
        for key in ("architecture design", "架构设计", "architecture", "技术架构", "system architecture", "overall architecture"):
            if key in sections:
                parts.append(f"{'**架构决策**' if zh else '**Architecture Decisions**'}:\n{sections[key][:800]}")
                _arch_found = True
                break

        # Tech stack extraction from raw text
        tech_mentions = re.findall(r'\b(Three\.js|React|Vue|Canvas|WebGL|WebSocket|Node\.js|Express|Phaser|Babylon\.js|PixiJS|Vite|Webpack|TypeScript|CSS Grid|Flexbox|TailwindCSS|GSAP)\b', raw, re.IGNORECASE)
        if tech_mentions:
            unique_tech = list(dict.fromkeys(t.strip() for t in tech_mentions))[:12]
            parts.append(f"\n{'**技术选型**' if zh else '**Tech Stack Decisions**'}: {', '.join(unique_tech)}")

        # Builder ownership map
        for key in ("builder ownership map", "builder 所有权映射", "builder ownership", "builder分工", "builder assignment", "task distribution"):
            if key in sections:
                parts.append(f"\n{'**Builder 分工**' if zh else '**Builder Ownership**'}:\n{sections[key][:800]}")
                break

        # File structure / project layout
        for key in ("file structure", "文件结构", "project structure", "项目结构", "directory structure"):
            if key in sections:
                parts.append(f"\n{'**文件结构**' if zh else '**File Structure**'}:\n{sections[key][:500]}")
                break

        # Execution strategy
        for key in ("execution plan", "执行计划", "execution strategy", "执行策略", "pipeline", "dag"):
            if key in sections:
                parts.append(f"\n{'**执行策略**' if zh else '**Execution Strategy**'}:\n{sections[key][:600]}")
                break

        # Risk analysis / constraints
        for key in ("risk", "风险", "constraints", "约束", "non-negotiables", "不可妥协"):
            if key in sections:
                parts.append(f"\n{'**风险与约束**' if zh else '**Risks & Constraints**'}:\n{sections[key][:400]}")
                break

        # Fallback: structured extraction from raw text
        if not parts or (len(parts) == 1 and not _arch_found):
            # Count concrete planning artifacts
            node_mentions = len(re.findall(r'(?:builder|node|subtask|子任务|节点)\b', lower))
            file_mentions = re.findall(r'[\w/-]+\.(html|js|css|json|ts|tsx|py)\b', raw)
            module_mentions = re.findall(r'(?:module|模块|component|组件|system|系统)\s*[:：]\s*(.{10,80})', raw, re.IGNORECASE)

            key_sentences = _extract_key_sentences(raw, [
                "architecture", "builder", "parallel", "module", "file",
                "架构", "模块", "并行", "文件", "计划", "responsibility",
            ], max_sentences=8)
            if key_sentences:
                parts.append(f"\n{'**规划要点**' if zh else '**Planning Highlights**'}:")
                for s in key_sentences:
                    parts.append(f"- {s[:250]}")
            if file_mentions:
                unique_files = list(dict.fromkeys(file_mentions))[:10]
                parts.append(f"\n{'**规划文件**' if zh else '**Planned Files**'}: {len(unique_files)} {'种类型' if zh else 'file types'} ({', '.join('.' + f for f in unique_files)})")
            if node_mentions:
                parts.append(f"{'涉及' if zh else 'Involves'} {node_mentions} {'个执行单元' if zh else 'execution units'}")

        title = "### 规划细节\n" if zh else "### Plan Details\n"
        return title + "\n".join(parts) if parts else ""

    if role == "analyst":
        parts = []

        # Research methodology — how many rounds / what was searched
        search_queries = re.findall(r'(?:search|搜索|query|查询)[:\s]+["\']?(.{10,80})["\']?', raw, re.IGNORECASE)
        web_fetches = re.findall(r'https?://[^\s\)\"\'<>]+', raw)
        research_rounds = len(re.findall(r'(?:round|iteration|轮|step)\s*\d+', lower))
        if search_queries or web_fetches or research_rounds:
            _method_parts = []
            if research_rounds:
                _method_parts.append(f"{research_rounds} {'轮研究迭代' if zh else 'research iterations'}")
            if search_queries:
                _method_parts.append(f"{len(search_queries)} {'次搜索查询' if zh else 'search queries'}")
            if web_fetches:
                _method_parts.append(f"{len(web_fetches)} {'个参考来源' if zh else 'reference sources'}")
            parts.append(f"{'**研究范围**' if zh else '**Research Scope**'}: {', '.join(_method_parts)}")

        # Design direction with evidence
        for key in ("design_direction", "设计方向", "design direction", "creative direction", "design approach"):
            if key in sections:
                parts.append(f"\n{'**设计方向**' if zh else '**Design Direction**'}:\n{sections[key][:800]}")
                break

        # Technical recommendations with specifics
        for key in ("technical recommendation", "技术建议", "implementation approach", "实现方案", "recommended approach"):
            if key in sections:
                parts.append(f"\n{'**技术建议**' if zh else '**Technical Recommendations**'}:\n{sections[key][:600]}")
                break

        # Non-negotiables / constraints
        for key in ("non_negotiables", "不可妥协项", "non-negotiables", "核心约束", "must-have", "requirements"):
            if key in sections:
                parts.append(f"\n{'**核心约束**' if zh else '**Non-Negotiables**'}:\n{sections[key][:500]}")
                break

        # Feasibility ratings — extract with full context
        feas_matches = re.findall(r'[-•*]\s*(.{5,120})\s*[:：]\s*(EASY|MODERATE|HARD|INFEASIBLE|可行|中等|困难|不可行)', raw)
        if feas_matches:
            parts.append(f"\n{'**可行性评级**' if zh else '**Feasibility Ratings**'}:")
            for feat, rating in feas_matches[:10]:
                emoji = {"EASY": "[容易]", "MODERATE": "[中等]", "HARD": "[困难]", "INFEASIBLE": "[不可行]",
                         "可行": "[容易]", "中等": "[中等]", "困难": "[困难]", "不可行": "[不可行]"}.get(rating, "[未评]")
                parts.append(f"  {emoji} {feat.strip()}: **{rating}**")

        # Reference sources — grouped by domain with descriptions
        if web_fetches:
            unique_urls = list(dict.fromkeys(web_fetches))[:10]
            domain_groups: dict = {}
            for u in unique_urls:
                parts_url = u.split('/')
                domain = parts_url[2] if len(parts_url) > 2 else u
                domain_groups.setdefault(domain, []).append(u)
            parts.append(f"\n{'**参考来源**' if zh else '**Reference Sources**'} ({len(unique_urls)} {'个链接' if zh else 'links'}):")
            for domain, urls_list in list(domain_groups.items())[:6]:
                if len(urls_list) > 1:
                    parts.append(f"- **{domain}** ({len(urls_list)} {'页' if zh else 'pages'})")
                else:
                    parts.append(f"- [{domain}]({urls_list[0]})")

        # Code snippets from research with language tags
        code_blocks = re.findall(r'```(\w*)\n(.+?)```', raw, re.DOTALL)
        if code_blocks:
            parts.append(f"\n{'**研究代码片段**' if zh else '**Research Code Snippets**'}: {len(code_blocks)} {'个' if zh else ''}")
            for lang_tag, snippet in code_blocks[:3]:
                trimmed = snippet.strip()[:250]
                parts.append(f"```{lang_tag}\n{trimmed}\n```")

        # Builder handoffs — structured with key points
        handoff_sections = {k: v for k, v in sections.items() if "handoff" in k or "交接" in k or "builder" in k}
        if handoff_sections:
            parts.append(f"\n{'**Builder 交接文档**' if zh else '**Builder Handoffs**'}: {len(handoff_sections)} {'份' if zh else 'docs'}")
            for name, content in list(handoff_sections.items())[:4]:
                # Extract key points from each handoff
                key_points = re.findall(r'[-•*]\s+(.{15,150})', content)
                if key_points:
                    parts.append(f"- **{name}**:")
                    for kp in key_points[:4]:
                        parts.append(f"  - {kp.strip()}")
                else:
                    parts.append(f"- **{name}**: {content[:500]}")

        # Fallback — still show meaningful data
        if not parts:
            feas_count = len(re.findall(r'(?:EASY|MODERATE|HARD|INFEASIBLE|可行|困难)', raw))
            handoff_count = len(re.findall(r'builder[_\s]*\d*[_\s]*handoff|交接', lower))
            key_sentences = _extract_key_sentences(raw, [
                "recommend", "suggest", "approach", "design", "architecture",
                "建议", "方案", "设计", "架构", "可行",
            ], max_sentences=6)
            if key_sentences:
                parts.append(f"{'**研究发现**' if zh else '**Research Findings**'}:")
                for s in key_sentences:
                    parts.append(f"- {s[:250]}")
            if feas_count:
                parts.append(f"{'完成了' if zh else 'Completed'} {feas_count} {'项可行性评级' if zh else 'feasibility ratings'}")
            if handoff_count:
                parts.append(f"{'生成了' if zh else 'Generated'} {handoff_count} {'份 Builder 交接文档' if zh else 'Builder handoff docs'}")

        title = "### 分析成果\n" if zh else "### Analysis Results\n"
        return title + "\n".join(parts) if parts else ""

    if role == "builder":
        parts = []

        # Detect implemented modules (comprehensive detection)
        modules = []
        for kw, label_zh, label_en in [
            ("player", "玩家控制", "Player Controller"), ("camera", "镜头系统", "Camera System"),
            ("weapon", "武器系统", "Weapon System"), ("enemy", "敌人AI", "Enemy AI"),
            ("collision", "碰撞检测", "Collision"), ("particle", "粒子效果", "Particles"),
            ("hud", "HUD", "HUD"), ("score", "计分", "Scoring"), ("level", "关卡", "Level"),
            ("physics", "物理", "Physics"), ("animation", "动画", "Animation"),
            ("state machine", "状态机", "State Machine"), ("game loop", "游戏循环", "Game Loop"),
            ("renderer", "渲染器", "Renderer"), ("sound", "音效", "Sound"),
            ("navigation", "导航", "Navigation"), ("carousel", "轮播", "Carousel"),
            ("form", "表单", "Form"), ("modal", "弹窗", "Modal"),
            ("three", "3D渲染", "3D Rendering"), ("canvas", "Canvas绘制", "Canvas Drawing"),
            ("webgl", "WebGL", "WebGL"), ("shader", "着色器", "Shader"),
            ("raycaster", "射线检测", "Raycaster"), ("inventory", "背包系统", "Inventory"),
            ("minimap", "小地图", "Minimap"), ("pathfind", "寻路", "Pathfinding"),
            ("terrain", "地形", "Terrain"), ("skybox", "天空盒", "Skybox"),
            ("lighting", "光照", "Lighting"), ("shadow", "阴影", "Shadows"),
            ("audio", "音频系统", "Audio System"), ("input", "输入处理", "Input Handler"),
            ("ui overlay", "UI层", "UI Overlay"), ("health", "生命值", "Health System"),
            ("spawn", "生成系统", "Spawn System"), ("projectile", "弹道", "Projectile"),
        ]:
            if kw in lower:
                modules.append(label_zh if zh else label_en)
        if modules:
            parts.append(f"{'**实现模块**' if zh else '**Modules Built**'} ({len(modules)}):\n" + ", ".join(modules[:15]))

        # File inventory with per-file breakdown
        if created:
            total_lines = 0
            file_details = []
            for f in created:
                fname = f.rsplit('/', 1)[-1] if '/' in f else f
                lc = _count_lines(result, f)
                if lc:
                    total_lines += int(lc)
                    file_details.append(f"`{fname}` ({lc}L)")
                else:
                    file_details.append(f"`{fname}`")
            parts.append(f"\n{'**代码产出**' if zh else '**Code Output**'}: {len(created)} {'个文件' if zh else 'files'}" + (f", ~{total_lines:,} {'行代码' if zh else 'lines'}" if total_lines else ""))
            if file_details:
                parts.append("  " + " · ".join(file_details[:10]))

        # Extract actual function/class names — enriched with context
        func_matches = re.findall(r'(?:function\s+(\w+)|const\s+(\w+)\s*=\s*(?:\([^)]*\)|\w+)\s*=>|class\s+(\w+))', raw)
        func_names = list(dict.fromkeys(next(n for n in m if n) for m in func_matches[:20]))
        if func_names:
            parts.append(f"\n{'**关键函数/类**' if zh else '**Key Functions/Classes**'} ({len(func_names)}):\n`{'`, `'.join(func_names[:15])}`")

        # Extract event listeners and interaction setup
        event_matches = re.findall(r"addEventListener\(['\"](\w+)['\"]", raw)
        if event_matches:
            unique_events = list(dict.fromkeys(event_matches))[:10]
            parts.append(f"\n{'**交互事件**' if zh else '**Event Listeners**'}: {', '.join(unique_events)}")

        # Key tech patterns — more comprehensive
        patterns = []
        if "three.js" in lower or ("three" in lower and "scene" in lower):
            patterns.append("Three.js 3D")
        if "requestanimationframe" in lower:
            patterns.append("requestAnimationFrame")
        if "canvas" in lower and "getcontext" in lower:
            patterns.append("Canvas 2D")
        if "websocket" in lower:
            patterns.append("WebSocket")
        if "async" in lower and "await" in lower:
            patterns.append("async/await")
        if "promise" in lower:
            patterns.append("Promise chain")
        if "gltfloader" in lower or "fbxloader" in lower or "objloader" in lower:
            patterns.append("3D model loading")
        if "orbitcontrols" in lower or "pointerlock" in lower:
            patterns.append("camera controls")
        if "ammo" in lower or "cannon" in lower or "rapier" in lower:
            patterns.append("physics engine")
        if "gsap" in lower or "tween" in lower:
            patterns.append("animation lib")
        if patterns:
            parts.append(f"\n{'**技术模式**' if zh else '**Tech Patterns**'}: {' · '.join(patterns)}")

        # Architecture insights from sections
        for key in ("architecture", "structure", "design", "模块设计", "系统架构"):
            if key in sections:
                parts.append(f"\n{'**架构说明**' if zh else '**Architecture Notes**'}:\n{sections[key][:400]}")
                break

        # Extract CSS/styling approach
        css_patterns = []
        if "css grid" in lower or "display: grid" in lower:
            css_patterns.append("CSS Grid")
        if "flexbox" in lower or "display: flex" in lower:
            css_patterns.append("Flexbox")
        if "tailwind" in lower:
            css_patterns.append("Tailwind")
        if "@keyframes" in lower:
            css_patterns.append("CSS Animations")
        if "transform" in lower and "transition" in lower:
            css_patterns.append("CSS Transitions")
        if css_patterns:
            parts.append(f"\n{'**样式方案**' if zh else '**Styling**'}: {', '.join(css_patterns)}")

        title = "### 构建细节\n" if zh else "### Build Details\n"
        return title + "\n".join(parts) if parts else ""

    if role == "merger":
        parts = []

        # Builder integration scope with detail
        b1 = any(k in lower for k in ("builder-1", "builder_1", "builder 1"))
        b2 = any(k in lower for k in ("builder-2", "builder_2", "builder 2"))
        if b1 and b2:
            parts.append(f"{'**集成范围**' if zh else '**Integration Scope**'}: Builder-1 + Builder-2 {'双线合并' if zh else 'dual-lane merge'}")
        elif b1:
            parts.append(f"{'**集成范围**' if zh else '**Integration Scope**'}: Builder-1 {'单线集成' if zh else 'single-lane integration'}")

        # Merge strategy details from sections
        for key in ("merge strategy", "合并策略", "integration plan", "集成方案"):
            if key in sections:
                parts.append(f"\n{'**合并策略**' if zh else '**Merge Strategy**'}:\n{sections[key][:500]}")
                break

        # Conflict resolution — extract with details
        conflicts = _extract_key_sentences(raw, [
            "conflict", "冲突", "dedup", "去重", "rename", "重命名",
            "duplicate", "重复", "override", "覆盖", "collision",
        ], max_sentences=5)
        if conflicts:
            parts.append(f"\n{'**冲突解决**' if zh else '**Conflict Resolution**'} ({len(conflicts)} {'项' if zh else 'items'}):")
            for c in conflicts:
                parts.append(f"  - {c[:250]}")

        # Structural changes
        structural = _extract_key_sentences(raw, [
            "refactor", "重构", "restructure", "reorganize", "合并", "拆分",
            "moved", "relocated", "extracted", "inlined",
        ], max_sentences=4)
        if structural:
            parts.append(f"\n{'**结构调整**' if zh else '**Structural Changes**'}:")
            for s in structural:
                parts.append(f"  - {s[:250]}")

        # Integration verification
        verify = _extract_key_sentences(raw, [
            "verified", "tested", "confirmed", "works", "验证", "测试", "确认",
        ], max_sentences=3)
        if verify:
            parts.append(f"\n{'**集成验证**' if zh else '**Integration Verification**'}:")
            for v in verify:
                parts.append(f"  - {v[:200]}")

        # File inventory with breakdown
        if created:
            total_lines = 0
            for f in created:
                lc = _count_lines(result, f)
                if lc:
                    total_lines += int(lc)
            parts.append(f"\n{'**合并产出**' if zh else '**Merged Output**'}: {len(created)} {'个文件' if zh else 'files'}" + (f", ~{total_lines:,} {'行' if zh else 'lines'}" if total_lines else ""))
            # Show file list
            for f in created[:8]:
                fname = f.rsplit('/', 1)[-1] if '/' in f else f
                parts.append(f"  - `{fname}`")

        title = "### 合并策略\n" if zh else "### Merge Strategy\n"
        return title + "\n".join(parts) if parts else ""

    if role == "reviewer":
        parts = []

        # Extract verdict with confidence
        if "approved" in lower or "通过" in lower:
            parts.append("**结论: 通过** [OK]" if zh else "**Verdict: APPROVED** [OK]")
        elif "rejected" in lower or "不通过" in lower:
            parts.append("**结论: 未通过** [X]" if zh else "**Verdict: REJECTED** [X]")
        elif "conditional" in lower or "有条件" in lower:
            parts.append("**结论: 有条件通过**" if zh else "**Verdict: CONDITIONAL**")

        # Extract scores from JSON output (reviewer outputs strict JSON)
        score_data = {}
        try:
            score_data = json.loads(raw) if raw.strip().startswith("{") else {}
        except (json.JSONDecodeError, ValueError):
            pass

        scores = score_data.get("scores", {})
        if scores:
            score_lines = []
            for dim, val in scores.items():
                # Add visual bar for scores
                val_num = float(val) if isinstance(val, (int, float)) else 0
                bar = "█" * int(val_num) + "░" * (10 - int(val_num))
                score_lines.append(f"  {bar} {dim}: **{val}**/10")
            if score_lines:
                parts.append(f"\n{'**维度评分**' if zh else '**Dimension Scores**'}:")
                parts.extend(score_lines)
            avg = score_data.get("average")
            if avg:
                parts.append(f"\n{'**综合均分**' if zh else '**Average Score**'}: **{avg}**/10")

        # Zone analysis
        zones = score_data.get("zone_analysis", [])
        if zones:
            parts.append(f"\n{'**区域分析**' if zh else '**Zone Analysis**'}:")
            for z in zones[:8]:
                parts.append(f"  - {z[:200]}")

        # Blocking issues with severity
        blocking = score_data.get("blocking_issues", [])
        if blocking:
            parts.append(f"\n{'**阻断问题**' if zh else '**Blocking Issues**'} ({len(blocking)}):")
            for b in blocking[:6]:
                parts.append(f"  - 阻塞：{b[:200]}")

        # Warnings / suggestions
        warnings = score_data.get("warnings", score_data.get("suggestions", []))
        if warnings:
            parts.append(f"\n{'**改进建议**' if zh else '**Suggestions**'} ({len(warnings)}):")
            for w in warnings[:5]:
                parts.append(f"  - 警告：{w[:200]}")

        # Strengths
        strengths = score_data.get("strengths", [])
        if strengths:
            parts.append(f"\n{'**优势项**' if zh else '**Strengths**'} ({len(strengths)}):")
            for s in strengths[:6]:
                parts.append(f"  [OK] {s[:200]}")

        # If no JSON data, try regex extraction (non-JSON reviewer output)
        if not scores and not blocking and not strengths:
            # Try extracting scores from text patterns
            score_pattern = re.findall(r'(\w[\w\s]{3,30})[:：]\s*(\d+(?:\.\d+)?)\s*/\s*10', raw)
            if score_pattern:
                parts.append(f"\n{'**维度评分**' if zh else '**Dimension Scores**'}:")
                for dim, val in score_pattern[:8]:
                    val_num = float(val)
                    bar = "█" * int(val_num) + "░" * (10 - int(val_num))
                    parts.append(f"  {bar} {dim.strip()}: **{val}**/10")

            # Extract issues with context
            issue_sentences = _extract_key_sentences(raw, [
                "issue", "bug", "problem", "error", "missing", "broken",
                "问题", "缺陷", "错误", "缺失", "损坏",
            ], max_sentences=5)
            if issue_sentences:
                parts.append(f"\n{'**发现问题**' if zh else '**Issues Found**'}:")
                for iss in issue_sentences:
                    parts.append(f"  - {iss[:200]}")

            # Extract strengths with context
            strength_sentences = _extract_key_sentences(raw, [
                "strength", "good", "excellent", "well", "impressive",
                "优点", "强项", "出色", "优秀",
            ], max_sentences=4)
            if strength_sentences:
                parts.append(f"\n{'**优势项**' if zh else '**Strengths**'}:")
                for s in strength_sentences:
                    parts.append(f"  [OK] {s[:200]}")

        # Visual evidence
        screenshots = re.findall(r'screenshot|截图|filmstrip|capture', lower)
        if screenshots:
            parts.append(f"\n{'**视觉验证**' if zh else '**Visual Evidence**'}: {'包含截图/视觉验证' if zh else 'includes screenshots/visual verification'}")

        title = "### 审查结果\n" if zh else "### Review Findings\n"
        return title + "\n".join(parts) if parts else ""

    if role == "tester":
        parts = []

        # Extract check results with pass rate
        pass_count = len(re.findall(r'(?:[OK]|PASS|passed|通过)', raw, re.IGNORECASE))
        fail_count = len(re.findall(r'(?:[X]|FAIL|failed|失败)', raw, re.IGNORECASE))
        if pass_count or fail_count:
            total = pass_count + fail_count
            rate = int(pass_count / total * 100) if total > 0 else 0
            bar = "█" * (rate // 10) + "░" * (10 - rate // 10)
            parts.append(f"{'**检查点**' if zh else '**Checks**'}: {bar} {pass_count}/{total} {'通过' if zh else 'passed'} ({rate}%)" + (f" — {fail_count} {'失败' if zh else 'failed'}" if fail_count else ""))

        # Evidence types — comprehensive
        evidence = []
        if "screenshot" in lower or "截图" in lower:
            evidence.append("[截图] " + ("截图" if zh else "screenshots"))
        if "filmstrip" in lower or "胶片" in lower:
            evidence.append("[剪辑]️ " + ("滚动胶片" if zh else "scroll filmstrip"))
        if "gameplay" in lower or "游玩" in lower:
            evidence.append("[游玩] " + ("游玩测试" if zh else "gameplay test"))
        if "console" in lower:
            evidence.append("[控制台] " + ("控制台日志" if zh else "console logs"))
        if "network" in lower or "request" in lower:
            evidence.append("[网络] " + ("网络请求" if zh else "network requests"))
        if "responsive" in lower or "mobile" in lower:
            evidence.append("[移动] " + ("响应式验证" if zh else "responsive check"))
        if evidence:
            parts.append(f"\n{'**验证证据**' if zh else '**Evidence**'}: {', '.join(evidence)}")

        # Test categories
        categories = []
        if any(k in lower for k in ("functional", "功能", "feature")):
            categories.append("功能测试" if zh else "Functional")
        if any(k in lower for k in ("visual", "视觉", "ui", "layout")):
            categories.append("视觉/UI" if zh else "Visual/UI")
        if any(k in lower for k in ("interaction", "交互", "click", "input")):
            categories.append("交互测试" if zh else "Interaction")
        if any(k in lower for k in ("performance", "性能", "fps", "load")):
            categories.append("性能" if zh else "Performance")
        if any(k in lower for k in ("cross-browser", "兼容", "mobile", "responsive")):
            categories.append("兼容性" if zh else "Compatibility")
        if categories:
            parts.append(f"{'**测试类别**' if zh else '**Test Categories**'}: {', '.join(categories)}")

        # Specific failures with detail
        fail_sentences = _extract_key_sentences(raw, [
            "fail", "error", "broken", "missing", "not work", "crash",
            "失败", "错误", "损坏", "缺失", "崩溃",
        ], max_sentences=5)
        if fail_sentences:
            parts.append(f"\n{'**发现缺陷**' if zh else '**Defects Found**'}:")
            for f in fail_sentences:
                parts.append(f"  - 阻塞：{f[:200]}")

        # Performance signals with specifics
        perf_mentions = _extract_key_sentences(raw, [
            "fps", "performance", "load time", "性能", "加载", "帧率",
            "memory", "内存", "latency", "延迟",
        ], max_sentences=4)
        if perf_mentions:
            parts.append(f"\n{'**性能信号**' if zh else '**Performance Signals**'}:")
            for p in perf_mentions:
                parts.append(f"  - {p[:200]}")

        # Accessibility
        a11y_mentions = _extract_key_sentences(raw, ["accessibility", "a11y", "aria", "可访问", "无障碍", "contrast"], max_sentences=3)
        if a11y_mentions:
            parts.append(f"\n{'**可访问性**' if zh else '**Accessibility**'}:")
            for a in a11y_mentions:
                parts.append(f"  - {a[:200]}")

        title = "### 测试结果\n" if zh else "### Test Results\n"
        return title + "\n".join(parts) if parts else ""

    if role == "debugger":
        parts = []

        # Bug count / severity
        bug_patterns = re.findall(r'(?:bug|issue|defect|error|缺陷|问题)\s*#?\d*\s*[:：]?\s*(.{10,100})', raw, re.IGNORECASE)
        if bug_patterns:
            parts.append(f"{'**处理缺陷**' if zh else '**Bugs Addressed**'}: {len(bug_patterns)} {'个' if zh else ''}")

        # Root cause analysis — detailed
        root_causes = _extract_key_sentences(raw, [
            "root cause", "根因", "caused by", "原因", "because", "由于",
            "the issue", "问题在于", "the bug", "the error",
        ], max_sentences=5)
        if root_causes:
            parts.append(f"\n{'**根因分析**' if zh else '**Root Cause Analysis**'}:")
            for rc in root_causes:
                parts.append(f"  - 搜索：{rc[:250]}")

        # Fixes applied — with before/after context
        fix_sentences = _extract_key_sentences(raw, [
            "fix", "修复", "patch", "changed", "修改", "updated", "replaced",
            "added", "removed", "删除", "添加", "resolved", "解决",
        ], max_sentences=6)
        if fix_sentences:
            parts.append(f"\n{'**修复措施**' if zh else '**Fixes Applied**'} ({len(fix_sentences)}):")
            for f in fix_sentences:
                parts.append(f"  - 修复：{f[:250]}")

        # Code changes detail
        code_changes = re.findall(r'```(?:diff|js|html|css)?\n(.+?)```', raw, re.DOTALL)
        if code_changes:
            parts.append(f"\n{'**代码变更**' if zh else '**Code Changes**'}: {len(code_changes)} {'处修改' if zh else 'modifications'}")
            for change in code_changes[:2]:
                trimmed = change.strip()[:200]
                parts.append(f"```\n{trimmed}\n```")

        # Verification with evidence
        verify = _extract_key_sentences(raw, [
            "verified", "验证", "confirmed", "确认", "test", "测试",
            "screenshot", "截图", "works", "正常",
        ], max_sentences=3)
        if verify:
            parts.append(f"\n{'**验证结果**' if zh else '**Verification**'}:")
            for v in verify:
                parts.append(f"  [OK] {v[:200]}")

        # File count with detail
        if created:
            parts.append(f"\n{'**修改文件**' if zh else '**Files Modified**'}: {len(created)} {'个' if zh else 'files'}")
            for f in created[:6]:
                fname = f.rsplit('/', 1)[-1] if '/' in f else f
                parts.append(f"  - `{fname}`")

        title = "### 调试细节\n" if zh else "### Debug Details\n"
        return title + "\n".join(parts) if parts else ""

    if role == "polisher":
        parts = []

        # Performance optimizations
        opt_sentences = _extract_key_sentences(raw, [
            "optimiz", "优化", "performance", "性能", "minif", "压缩",
            "lazy", "cache", "缓存", "bundle", "reduce", "compress",
        ], max_sentences=5)
        if opt_sentences:
            parts.append(f"{'**性能优化**' if zh else '**Performance Optimizations**'} ({len(opt_sentences)}):")
            for o in opt_sentences:
                parts.append(f"  - 性能：{o[:250]}")

        # Visual refinements
        visual = _extract_key_sentences(raw, [
            "visual", "视觉", "color", "颜色", "font", "字体", "spacing",
            "animation", "动画", "transition", "过渡", "gradient", "shadow",
            "responsive", "响应式", "mobile",
        ], max_sentences=5)
        if visual:
            parts.append(f"\n{'**视觉微调**' if zh else '**Visual Refinements**'} ({len(visual)}):")
            for v in visual:
                parts.append(f"  - 视觉：{v[:250]}")

        # UX improvements
        ux = _extract_key_sentences(raw, [
            "user experience", "交互", "hover", "feedback", "smooth",
            "accessible", "usability", "touch", "gesture",
        ], max_sentences=3)
        if ux:
            parts.append(f"\n{'**交互体验**' if zh else '**UX Improvements**'}:")
            for u in ux:
                parts.append(f"  - {u[:200]}")

        # File changes
        if created:
            total_lines = 0
            for f in created:
                lc = _count_lines(result, f)
                if lc:
                    total_lines += int(lc)
            parts.append(f"\n{'**文件修改**' if zh else '**Files Modified**'}: {len(created)} {'个' if zh else 'files'}" + (f", ~{total_lines:,} {'行' if zh else 'lines'}" if total_lines else ""))

        title = "### 打磨细节\n" if zh else "### Polish Details\n"
        return title + "\n".join(parts) if parts else ""

    if role == "deployer":
        parts = []

        # Deployment target
        deploy_sentences = _extract_key_sentences(raw, [
            "deploy", "部署", "url", "link", "链接", "preview", "预览",
            "production", "生产",
        ], max_sentences=3)
        if deploy_sentences:
            parts.append(f"{'**部署信息**' if zh else '**Deployment Info**'}:")
            for d in deploy_sentences:
                parts.append(f"  - {d[:200]}")

        # Extract URLs
        deploy_urls = re.findall(r'https?://[^\s\)\"\'<>]+', raw)
        if deploy_urls:
            parts.append(f"\n{'**访问链接**' if zh else '**Access Links**'}:")
            for u in deploy_urls[:3]:
                parts.append(f"  - {u}")

        title = "### 部署详情\n" if zh else "### Deployment Details\n"
        return title + "\n".join(parts) if parts else ""

    if role == "imagegen":
        parts = []
        imgs = [f for f in created if any(f.lower().endswith(e) for e in (".png", ".jpg", ".svg", ".webp"))]
        docs = [f for f in created if any(f.lower().endswith(e) for e in (".md", ".json", ".txt"))]
        if imgs:
            categories = set()
            for f in imgs:
                fl = f.lower()
                for kw, cat in [("character", "角色"), ("hero", "英雄"), ("monster", "怪物"),
                                ("weapon", "武器"), ("environment", "环境"), ("scene", "场景"),
                                ("icon", "图标"), ("logo", "标志"), ("bg", "背景"), ("ui", "界面"),
                                ("enemy", "敌人"), ("boss", "Boss"), ("npc", "NPC"),
                                ("terrain", "地形"), ("effect", "特效"), ("particle", "粒子")]:
                    if kw in fl:
                        categories.add(cat if zh else kw.capitalize())
            parts.append(f"{'**视觉资产**' if zh else '**Visual Assets**'}: {len(imgs)} {'张' if zh else 'images'}")
            if categories:
                parts.append(f"{'**资产类别**' if zh else '**Asset Categories**'}: {', '.join(sorted(categories))}")
            # Show image filenames
            for f in imgs[:8]:
                fname = f.rsplit('/', 1)[-1] if '/' in f else f
                parts.append(f"  - [图片]️ `{fname}`")
        if docs:
            parts.append(f"\n{'**设计文档**' if zh else '**Design Docs**'}: {len(docs)} {'份' if zh else 'files'}")

        # Style/prompt details — more comprehensive
        style_sentences = _extract_key_sentences(raw, [
            "style", "风格", "prompt", "resolution", "分辨率", "palette", "配色",
            "pixel", "像素", "dimension", "尺寸", "format", "格式",
            "art direction", "美术方向",
        ], max_sentences=4)
        if style_sentences:
            parts.append(f"\n{'**生成参数**' if zh else '**Generation Parameters**'}:")
            for s in style_sentences:
                parts.append(f"  - {s[:200]}")

        # Generation method
        if "dall-e" in lower or "dalle" in lower:
            parts.append(f"\n{'**生成引擎**' if zh else '**Engine**'}: DALL-E")
        elif "stable diffusion" in lower or "sdxl" in lower:
            parts.append(f"\n{'**生成引擎**' if zh else '**Engine**'}: Stable Diffusion")
        elif "midjourney" in lower:
            parts.append(f"\n{'**生成引擎**' if zh else '**Engine**'}: Midjourney")
        elif "placeholder" in lower or "svg" in lower:
            parts.append(f"\n{'**生成方式**' if zh else '**Method**'}: SVG/{'占位符生成' if zh else 'Placeholder generation'}")

        title = "### 图像产出\n" if zh else "### Image Output\n"
        return title + "\n".join(parts) if parts else ""

    if role == "spritesheet":
        parts = []
        sprite_files = [f for f in created if any(f.lower().endswith(e) for e in (".png", ".jpg", ".webp"))]
        map_files = [f for f in created if any(f.lower().endswith(e) for e in (".json", ".css"))]
        js_files = [f for f in created if f.lower().endswith(".js")]
        if sprite_files:
            parts.append(f"{'**精灵表**' if zh else '**Spritesheets**'}: {len(sprite_files)} {'张' if zh else 'sheets'}")
            for f in sprite_files[:6]:
                fname = f.rsplit('/', 1)[-1] if '/' in f else f
                parts.append(f"  - `{fname}`")
        if map_files:
            parts.append(f"\n{'**映射文件**' if zh else '**Mapping Files**'}: {len(map_files)} {'份' if zh else 'files'}")
        if js_files:
            parts.append(f"\n{'**加载脚本**' if zh else '**Loader Scripts**'}: {len(js_files)} {'个' if zh else 'files'}")

        # Animation details
        anim_mentions = _extract_key_sentences(raw, [
            "animation", "frame", "动画", "帧", "sprite", "atlas", "sequence",
        ], max_sentences=3)
        if anim_mentions:
            parts.append(f"\n{'**动画配置**' if zh else '**Animation Config**'}:")
            for a in anim_mentions:
                parts.append(f"  - {a[:200]}")

        title = "### 精灵表产出\n" if zh else "### Spritesheet Output\n"
        return title + "\n".join(parts) if parts else ""

    return ""


def _count_lines(result: Dict[str, Any], file_path: str) -> str:
    """Try to count lines in a created file from tool results."""
    for tr in result.get("tool_results", []):
        args = tr.get("args") if isinstance(tr.get("args"), dict) else {}
        path = str(args.get("path") or args.get("file_path") or "")
        if path == file_path:
            content = str(args.get("content") or "")
            if content:
                return str(content.count("\n") + 1)
    return ""


def _format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"


def _format_tokens(count: int) -> str:
    if count <= 0:
        return "—"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)


def _format_time(ts: float) -> str:
    if not ts:
        return "--:--:--"
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _tool_icon(tool_name: str) -> str:
    # v5.8.6: emoji removed. Plain-text tags render consistently in logs,
    # terminals, and PDF exports without needing emoji font support.
    icons = {
        "file_read": "[read]", "file_write": "[write]", "file_edit": "[edit]",
        "file_ops": "[fs]", "grep_search": "[grep]", "web_search": "[search]",
        "web_fetch": "[fetch]", "bash": "[shell]", "browser": "[browser]",
        "browser_use": "[browser]", "source_crawler": "[crawl]",
        "context_compress": "[compact]", "sub_agent": "[agent]",
        "screenshot": "[shot]", "computer_use": "[cu]",
    }
    return icons.get(tool_name, "[tool]")


def _describe_tool_call(tr: Dict[str, Any], lang: str = "en") -> str:
    """Generate a human-readable description of a tool call.

    V4.2: Also handles entries without explicit 'tool' field by reading
    'path', 'written', 'url' etc.
    """
    tool = str(tr.get("tool") or "").strip()
    args = tr.get("args") if isinstance(tr.get("args"), dict) else {}
    zh = lang == "zh"

    # Fallback: if no tool name, infer from entry shape
    if not tool:
        if tr.get("written") or tr.get("path"):
            tool = "file_ops"
            if not args:
                path = str(tr.get("path") or "")
                args = {"action": "write", "path": path}
        elif tr.get("url"):
            tool = "web_fetch"
            if not args:
                args = {"url": str(tr.get("url") or "")}

    if tool in ("file_read", "file_ops"):
        action = str(args.get("action") or "read")
        path = str(args.get("path") or args.get("file_path") or "?")
        filename = path.rsplit("/", 1)[-1] if "/" in path else path
        if action == "write":
            return f"{'写入' if zh else 'Write'} {filename}"
        elif action == "list":
            return f"{'扫描目录' if zh else 'List'} {filename}"
        return f"{'读取' if zh else 'Read'} {filename}"
    elif tool in ("file_write", "write_file"):
        path = str(args.get("path") or args.get("file_path") or "?")
        filename = path.rsplit("/", 1)[-1] if "/" in path else path
        return f"{'写入' if zh else 'Write'} {filename}"
    elif tool == "file_edit":
        path = str(args.get("path") or args.get("file_path") or "?")
        filename = path.rsplit("/", 1)[-1] if "/" in path else path
        return f"{'编辑' if zh else 'Edit'} {filename}"
    elif tool == "grep_search":
        return f"{'搜索' if zh else 'Search'} \"{args.get('query', '?')}\""
    elif tool == "web_search":
        return f"{'网络搜索' if zh else 'Web search'} \"{args.get('query', '?')}\""
    elif tool == "web_fetch":
        url = str(args.get("url", "?"))
        domain = re.search(r'https?://([^/]+)', url)
        return f"{'抓取' if zh else 'Fetch'} {domain.group(1) if domain else url[:40]}"
    elif tool == "bash":
        cmd = str(args.get('command', ''))[:50]
        return f"{'执行' if zh else 'Run'} `{cmd}`"
    elif tool in ("browser", "browser_use"):
        action = str(args.get("action") or "navigate")
        return f"{'浏览器' if zh else 'Browser'} {action}"
    elif tool == "context_compress":
        return "Context compression" if not zh else "上下文压缩"
    return f"{tool}"


def _infer_file_purpose(path: str, ext: str, first_lines: str, lang: str = "en") -> str:
    """Infer a human-readable purpose for a file."""
    zh = lang == "zh"
    filename = path.rsplit("/", 1)[-1] if "/" in path else path
    lower_first = first_lines.lower()

    special = {
        "index.html": ("主入口页面", "Main entry page"),
        "style.css": ("全局样式", "Global styles"),
        "styles.css": ("全局样式", "Global styles"),
        "main.js": ("核心逻辑", "Core logic"),
        "app.js": ("应用逻辑", "App logic"),
        "game.js": ("游戏引擎", "Game engine"),
        "package.json": ("项目配置", "Project config"),
    }
    if filename.lower() in special:
        return special[filename.lower()][0 if zh else 1]

    if "three" in lower_first or "scene" in lower_first:
        return "3D 场景" if zh else "3D scene"
    if "canvas" in lower_first:
        return "Canvas 渲染" if zh else "Canvas rendering"

    ext_map = {
        "html": ("HTML 页面", "HTML page"), "css": ("样式表", "Stylesheet"),
        "js": ("JavaScript", "JavaScript"), "ts": ("TypeScript", "TypeScript"),
        "tsx": ("React 组件", "React component"), "py": ("Python 脚本", "Python script"),
        "json": ("JSON 数据", "JSON data"), "svg": ("SVG 图形", "SVG graphic"),
    }
    if ext in ext_map:
        return ext_map[ext][0 if zh else 1]
    return ""


def _ext_to_tech(ext: str) -> str:
    tech_map = {
        "html": "HTML5", "css": "CSS3", "js": "JavaScript", "ts": "TypeScript",
        "tsx": "React + TypeScript", "jsx": "React", "py": "Python",
        "json": "JSON", "glsl": "WebGL Shaders", "svg": "SVG",
    }
    return tech_map.get(ext, "")


def _ext_to_language(ext: str) -> str:
    lang_map = {
        "html": "html", "css": "css", "js": "javascript", "ts": "typescript",
        "tsx": "tsx", "jsx": "jsx", "py": "python", "json": "json",
        "glsl": "glsl", "md": "markdown",
    }
    return lang_map.get(ext, "")
