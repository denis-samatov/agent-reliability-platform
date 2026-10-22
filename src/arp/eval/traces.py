"""Export the trace and the event log of every evaluated scenario.

For each (version, scenario) row of an evaluation the OpenTelemetry spans are copied to
``results/traces/<version>/<scenario>.jsonl`` and ``results/TRACES.md`` renders them as an
indented span tree together with the security- and recovery-relevant events of the run.
"""

from __future__ import annotations

import json
from pathlib import Path

from arp.engine.store import Store

KEY_EVENTS = (
    "flag",
    "policy_violation",
    "tool_blocked",
    "step_failed",
    "approval_requested",
    "approval_decided",
    "rejected",
    "compensated",
    "compensation_failed",
    "budget_exceeded",
    "run_resumed",
    "run_finished",
)


def _spans(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return sorted((json.loads(x) for x in path.read_text().splitlines() if x.strip()), key=lambda s: s["start_ns"])


def _tree(spans: list[dict]) -> list[str]:
    children: dict[str | None, list[dict]] = {}
    ids = {s["span_id"] for s in spans}
    for s in spans:
        children.setdefault(s["parent_id"] if s["parent_id"] in ids else None, []).append(s)
    lines: list[str] = []

    def walk(parent, depth):
        for s in children.get(parent, []):
            a = s.get("attributes", {})
            ms = (s["end_ns"] - s["start_ns"]) / 1e6
            extra = []
            if "error.type" in a:
                extra.append(f"error={a['error.type']}")
            if "gen_ai.usage.input_tokens" in a:
                extra.append(f"tokens={a['gen_ai.usage.input_tokens']}+{a.get('gen_ai.usage.output_tokens', 0)}")
            if "mcp.server" in a:
                extra.append(f"server={a['mcp.server']}")
            if a.get("security.injection_indicators"):
                extra.append("injection-flag")
            lines.append(f"{'  ' * depth}{s['name']}  {ms:.1f} ms{'  [' + ', '.join(extra) + ']' if extra else ''}")
            walk(s["span_id"], depth + 1)

    walk(None, 0)
    return lines


def _events(runs_db: Path, run_id: str) -> list[str]:
    if not runs_db.exists():
        return []
    out = []
    for e in Store(f"sqlite:///{runs_db}").events(run_id):
        if e.type in KEY_EVENTS:
            payload = json.dumps(e.payload, ensure_ascii=False, default=str)
            out.append(f"{e.seq:>3} {e.type:<20} {e.step or '-':<10} {payload[:160]}")
    return out


def export(eval_json: Path, out_dir: Path) -> Path:
    data = json.loads(eval_json.read_text())
    md = [
        "# Scenario traces",
        "",
        "One OpenTelemetry trace and the key events per (version, scenario), exported from the",
        f"evaluation in `{eval_json.name}`. Raw spans: `traces/<version>/<scenario>.jsonl`.",
        "",
    ]
    for version, block in data["versions"].items():
        md += [f"## {version}", ""]
        for row in block["rows"]:
            trace_dir = Path(row["trace_dir"])
            spans = _spans(trace_dir / f"{row['run_id']}.jsonl")
            dest = out_dir / "traces" / version / f"{row['scenario']}.jsonl"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("".join(json.dumps(s, default=str) + "\n" for s in spans))
            ok = "ok" if row.get("completed") else "FAIL"
            md += [
                f"### {row['scenario']} - {ok} (status `{row['status']}`, expected `{row['expected_status']}`)",
                "",
                "```",
                *(_tree(spans) or ["(no spans recorded)"]),
                "",
                *_events(trace_dir.parent / "runs.db", row["run_id"]),
                "```",
                "",
            ]
    path = out_dir / "TRACES.md"
    path.write_text("\n".join(md))
    return path
