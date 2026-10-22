"""Kill a worker process right after a side effect, before it is recorded; a second worker
must resume the run and must not repeat the effect (idempotency key replay)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from arp.platform import Platform, PlatformConfig

ROOT = Path(__file__).resolve().parents[1]


def _env(tmp_path, **extra):
    return {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "ARP_DATABASE_URL": f"sqlite:///{tmp_path}/runs.db",
        "ARP_ITSM_PATH": str(tmp_path / "itsm.db"),
        "ARP_TRACE_DIR": str(tmp_path / "traces"),
        "ARP_LEASE_S": "1",
        **extra,
    }


def _advance(tmp_path, rid, **extra):
    return subprocess.run(
        [sys.executable, "-m", "arp.cli", "advance", rid],
        env=_env(tmp_path, **extra),
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_worker_crash_after_side_effect_is_recovered_exactly_once(tmp_path):
    cfg = PlatformConfig(
        db_url=f"sqlite:///{tmp_path}/runs.db",
        itsm_path=str(tmp_path / "itsm.db"),
        trace_dir=tmp_path / "traces",
        versions=("v0-rules",),
    )
    p = Platform(cfg)
    rid = p.submit("acme", "u102", "Please install the latest version of Slack.")
    marker = tmp_path / "crashed"
    crash = _advance(tmp_path, rid, ARP_CRASH_AFTER_STEP="execute", ARP_CRASH_ONCE_FILE=str(marker))
    assert crash.returncode == 17 and marker.exists(), crash.stderr[-2000:]
    p2 = Platform(cfg)
    assert p2.itsm.count("tickets", user_id="u102") == 1  # the effect happened before the crash
    started = [e.step for e in p2.store.events(rid) if e.type == "step_started"]
    completed = [e.step for e in p2.store.events(rid) if e.type == "step_completed"]
    assert "execute" in started and "execute" not in completed  # ...but was never recorded
    import time

    time.sleep(1.2)  # lease of the dead worker expires
    ok = _advance(tmp_path, rid)
    assert ok.returncode == 0 and ok.stdout.strip().endswith("succeeded"), ok.stderr[-2000:]
    p3 = Platform(cfg)
    assert p3.itsm.count("tickets", user_id="u102") == 1  # not duplicated
    replays = [e for e in p3.store.events(rid) if e.type == "tool_call" and e.payload.get("replayed")]
    assert [e.payload["tool"] for e in replays] == ["create_ticket"]
    assert any(e.type == "run_resumed" for e in p3.store.events(rid))
