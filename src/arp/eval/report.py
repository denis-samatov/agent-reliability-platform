"""Write evaluation results (JSON with manifest) and a Markdown report."""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

METRICS = [
    ("task_completion", "task completion"),
    ("action_accuracy", "action accuracy"),
    ("unauthorized_action_prevention", "unauthorized-action prevention"),
    ("recovery_success", "recovery success"),
    ("tool_selection_recall", "tool-selection recall"),
    ("invalid_tool_calls", "invalid tool calls"),
    ("active_s_p50", "latency p50 (s)"),
    ("active_s_p95", "latency p95 (s)"),
    ("tokens_total", "tokens (total)"),
    ("llm_calls_total", "LLM calls"),
    ("cost_usd_total", "cost (USD, configured prices)"),
]


def _manifest() -> dict:
    def v(p):
        try:
            return metadata.version(p)
        except metadata.PackageNotFoundError:
            return None

    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    return {
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "git_commit": commit,
        "packages": {p: v(p) for p in ("mcp", "opentelemetry-sdk", "localdesk", "mcp-capguard", "sqlalchemy")},
    }


def write_results(res: dict, out: Path, prices=None) -> Path:
    res = {"suite": "agent_eval", "manifest": _manifest(), "prices": asdict(prices) if prices else None, **res}
    (out / "eval.json").write_text(json.dumps(res, indent=1, default=str))
    versions = list(res["versions"])
    md = [
        "# Agent workflow evaluation",
        "",
        f"{len(res['versions'][versions[0]]['rows'])}+ scenarios per version (`src/arp/eval/scenarios.py`). LLM versions use",
        "a local Qwen3-1.7B (Q4_K_M) through llama.cpp on CPU; cost uses the configured price table "
        f"({res['prices']}); with the default local prices the measured monetary cost is 0.",
        "",
        "| metric | " + " | ".join(versions) + " |",
        "|---|" + "---|" * len(versions),
    ]
    for key, title in METRICS:
        cells = []
        for v in versions:
            x = res["versions"][v]["summary"].get(key)
            cells.append("-" if x is None else f"{x:.3f}" if isinstance(x, float) else str(x))
        md.append(f"| {title} | " + " | ".join(cells) + " |")
    md += [
        "",
        "## Per-scenario outcomes",
        "",
        "| scenario | kind | " + " | ".join(versions) + " |",
        "|---|---|" + "---|" * len(versions),
    ]
    sids = [r["scenario"] for r in res["versions"][versions[-1]]["rows"]]
    for sid in sids:
        cells, kind = [], ""
        for v in versions:
            row = next((r for r in res["versions"][v]["rows"] if r["scenario"] == sid), None)
            if row is None:
                cells.append("n/a")
                continue
            kind = row["kind"]
            cells.append(("ok " if row["completed"] else "FAIL ") + row["status"])
        md.append(f"| {sid} | {kind} | " + " | ".join(cells) + " |")
    md += ["", "Failures are listed, not hidden: they show where the planner or the policy needs work.", ""]
    path = out / "EVAL.md"
    path.write_text("\n".join(md) + "\n")
    return path
