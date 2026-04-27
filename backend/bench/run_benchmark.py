#!/usr/bin/env python3
"""Evermind HTML5 App Generation Micro-Benchmark runner.

v6.1.6 — drives the 10 prompts in prompts.json through Evermind's own
pipeline, saves each artifact, and optionally runs an LLM-as-judge pass
to score against a competitor set (Bolt.new / v0 / Lovable outputs that
the user has manually captured under competitors/).

Usage::

    # Generate Evermind outputs only
    python3 run_benchmark.py generate --out ./runs/evermind_$(date +%s)

    # Run LLM judge (requires competitor dir + strong judge model env)
    python3 run_benchmark.py judge --ours ./runs/evermind_latest \
            --theirs ./runs/bolt --judge gpt-5 --out ./runs/report.json

Limits on purpose: this is a "are we on par with Bolt.new" probe, not a
full academic benchmark. Expect wall-clock 10-40 min per "generate" pass.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


BENCH_DIR = Path(__file__).parent
PROMPTS = json.loads((BENCH_DIR / "prompts.json").read_text("utf-8"))


def cmd_generate(args) -> int:
    """Generate Evermind output for each benchmark prompt.

    Submits each prompt to the LOCAL Evermind HTTP API. Requires backend
    running on 127.0.0.1:8765. Polls run until complete, then copies
    /tmp/evermind_output to the benchmark folder.
    """
    import urllib.request
    import urllib.error
    import shutil

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    results: List[Dict[str, Any]] = []
    for item in PROMPTS["prompts"][: args.limit or None]:
        pid = item["id"]
        task_dir = out_dir / pid
        task_dir.mkdir(exist_ok=True)
        print(f"[bench] submitting {pid}")
        t0 = time.time()
        body = json.dumps({
            "goal": item["text"],
            "difficulty": args.difficulty,
            "trigger_source": "benchmark",
        }).encode("utf-8")
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:8765/api/runs",
                method="POST",
                headers={"Content-Type": "application/json"},
                data=body,
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                run = json.loads(resp.read().decode("utf-8"))
            run_id = run.get("id") or run.get("run_id") or ""
            if not run_id:
                raise RuntimeError(f"no run id returned: {run}")
        except Exception as exc:
            print(f"[bench] {pid} submit failed: {exc}")
            results.append({"id": pid, "status": "submit_failed", "error": str(exc)})
            continue

        # Poll until completion or timeout
        deadline = time.time() + (args.timeout or 2400)
        while time.time() < deadline:
            time.sleep(15)
            try:
                req = urllib.request.Request(f"http://127.0.0.1:8765/api/runs/{run_id}")
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read().decode("utf-8"))
                status = str(data.get("status") or "").lower()
                if status in ("completed", "failed", "cancelled"):
                    break
            except Exception:
                continue

        # Copy /tmp/evermind_output to task_dir
        src = Path("/tmp/evermind_output")
        if src.exists():
            for f in src.rglob("*"):
                if f.is_file():
                    rel = f.relative_to(src)
                    dst = task_dir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dst)

        duration = int(time.time() - t0)
        results.append({
            "id": pid,
            "status": status if 'status' in dir() else "timeout",
            "run_id": run_id,
            "duration_s": duration,
            "artifact_dir": str(task_dir),
        })
        print(f"[bench] {pid} done: {results[-1]['status']} in {duration}s")

    (out_dir / "summary.json").write_text(
        json.dumps({"version": PROMPTS["version"],
                    "difficulty": args.difficulty,
                    "results": results,
                    "completed_at": int(time.time())},
                   indent=2, ensure_ascii=False),
        "utf-8",
    )
    print(f"[bench] wrote {out_dir / 'summary.json'}")
    return 0


def _read_index_html(dir_path: Path) -> str:
    candidates = list(dir_path.rglob("index.html"))
    if not candidates:
        return ""
    # Prefer top-level
    candidates.sort(key=lambda p: len(p.parts))
    try:
        return candidates[0].read_text("utf-8", errors="replace")[:40_000]
    except Exception:
        return ""


def cmd_judge(args) -> int:
    """LLM-as-judge A/B comparison between two artifact directories.

    Uses a strong external judge model (default gpt-5) via litellm. For each
    prompt, sends both index.html bodies + prompt, asks judge to score each
    on the 6-dim rubric and pick a winner.
    """
    try:
        import litellm
    except ImportError:
        print("litellm not installed; pip install litellm", file=sys.stderr)
        return 2

    ours = Path(args.ours).expanduser().resolve()
    theirs = Path(args.theirs).expanduser().resolve() if args.theirs else None

    rubric = PROMPTS["rubric"]
    judge_model = args.judge or "gpt-5"

    report: List[Dict[str, Any]] = []
    for item in PROMPTS["prompts"]:
        pid = item["id"]
        a_html = _read_index_html(ours / pid)
        b_html = _read_index_html(theirs / pid) if theirs else ""
        if not a_html:
            report.append({"id": pid, "skipped": "no Evermind artifact"})
            continue

        msg = (
            "You are a strict front-end code reviewer. Score TWO HTML apps "
            "against the same prompt. Return JSON only.\n\n"
            f"PROMPT:\n{item['text']}\n\n"
            f"RUBRIC (weights):\n{json.dumps(rubric, indent=2)}\n\n"
            f"=== A (Evermind) ===\n{a_html[:15000]}\n\n"
            f"=== B (competitor) ===\n{b_html[:15000] if b_html else '[NO COMPETITOR SUBMITTED]'}\n\n"
            "Output JSON: {\"a_scores\":{...6 dims...},\"b_scores\":{...},"
            "\"a_total\":0-100,\"b_total\":0-100,\"winner\":\"A\"|\"B\"|\"tie\","
            "\"why\":\"one sentence\"}"
        )
        try:
            resp = litellm.completion(
                model=judge_model,
                messages=[{"role": "user", "content": msg}],
                max_tokens=1200,
                temperature=0.0,
            )
            raw = resp.choices[0].message.content or "{}"
            # strip code-fences
            for fence in ("```json", "```"):
                raw = raw.replace(fence, "")
            data = json.loads(raw.strip())
        except Exception as exc:
            data = {"error": str(exc)}
        data["id"] = pid
        data["category"] = item.get("category", "")
        report.append(data)
        print(f"[judge] {pid}: "
              f"A={data.get('a_total', '?')} B={data.get('b_total', '?')} "
              f"winner={data.get('winner', '?')}")

    out = Path(args.out or (BENCH_DIR / f"judge_report_{int(time.time())}.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "judge": judge_model,
        "ours": str(ours),
        "theirs": str(theirs) if theirs else None,
        "reports": report,
        "wins": sum(1 for r in report if r.get("winner") == "A"),
        "losses": sum(1 for r in report if r.get("winner") == "B"),
        "ties": sum(1 for r in report if r.get("winner") == "tie"),
        "generated_at": int(time.time()),
    }
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False), "utf-8")
    print(f"[judge] wrote {out}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="evermind_bench")
    sub = parser.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="submit each prompt to running Evermind backend")
    g.add_argument("--out", required=True, help="output dir (will contain per-prompt subdirs)")
    g.add_argument("--difficulty", default="pro", choices=("simple", "pro"))
    g.add_argument("--limit", type=int, default=0, help="only run first N prompts")
    g.add_argument("--timeout", type=int, default=2400, help="per-prompt timeout seconds")

    j = sub.add_parser("judge", help="LLM-as-judge A/B between two artifact dirs")
    j.add_argument("--ours", required=True)
    j.add_argument("--theirs", default="", help="competitor dir (optional — absent means solo scoring)")
    j.add_argument("--judge", default="gpt-5", help="judge model (default gpt-5; claude-opus-4-7 also works)")
    j.add_argument("--out", default="", help="report output path")

    args = parser.parse_args(argv)
    if args.cmd == "generate":
        return cmd_generate(args)
    if args.cmd == "judge":
        return cmd_judge(args)
    parser.error(f"unknown cmd: {args.cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
