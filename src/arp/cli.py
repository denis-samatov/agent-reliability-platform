"""Command line interface.

arp submit --tenant acme --requester u100 --version v2-llm "ticket text"
arp advance RUN_ID            # advance one run (used by tests to run in a separate process)
arp worker                    # poll runnable runs forever
arp eval --versions v0-rules,v1-llm,v2-llm
arp serve                     # dashboard + API
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

import anyio


def _platform(versions=None):
    from arp.platform import Platform, PlatformConfig

    cfg = PlatformConfig()
    if versions:
        cfg.versions = tuple(versions)
    if os.environ.get("ARP_LEASE_S"):
        cfg.lease_s = float(os.environ["ARP_LEASE_S"])
    return Platform(cfg)


def cmd_submit(a):
    p = _platform([a.version])
    print(p.submit(a.tenant, a.requester, a.ticket, a.version))


def cmd_advance(a):
    p = _platform()
    run = p.store.get_run(a.run_id)
    p = _platform([run.version])
    print(anyio.run(p.advance, a.run_id))


def cmd_worker(a):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = _platform(a.versions.split(","))
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("flag", True))
    while not stop["flag"]:
        runnable = [r for r in p.store.list_runs(500) if r.status in ("pending", "running")]
        for r in runnable:
            if f"{r.workflow}@{r.version}" in p.workflows:
                status = anyio.run(p.advance, r.id)
                logging.info(json.dumps({"event": "advanced", "run": r.id, "status": status}))
        time.sleep(a.poll)


def cmd_eval(a):
    from arp.engine.budget import Prices
    from arp.eval.runner import run_suite

    prices = Prices(a.price_in, a.price_out)
    res = anyio.run(run_suite, a.versions.split(","), os.environ.get("ARP_LLM_BASE_URL"), Path(a.workdir), None, prices)
    out = Path("results")
    out.mkdir(exist_ok=True)
    from arp.eval.report import write_results

    print(f"wrote {write_results(res, out, prices)}")


def cmd_serve(a):
    import uvicorn

    from arp.app.api import create_app

    uvicorn.run(create_app(_platform(a.versions.split(","))), host=a.host, port=a.port)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="arp")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit")
    s.add_argument("ticket")
    s.add_argument("--tenant", default="acme")
    s.add_argument("--requester", default="u100")
    s.add_argument("--version", default="v0-rules")
    s.set_defaults(fn=cmd_submit)
    s = sub.add_parser("advance")
    s.add_argument("run_id")
    s.set_defaults(fn=cmd_advance)
    s = sub.add_parser("worker")
    s.add_argument("--versions", default="v0-rules,v1-llm,v2-llm")
    s.add_argument("--poll", type=float, default=1.0)
    s.set_defaults(fn=cmd_worker)
    s = sub.add_parser("eval")
    s.add_argument("--versions", default="v0-rules,v1-llm,v2-llm")
    s.add_argument("--workdir", default="eval-work")
    s.add_argument("--price-in", type=float, default=0.0, help="USD per 1M input tokens (0 = local model)")
    s.add_argument("--price-out", type=float, default=0.0)
    s.set_defaults(fn=cmd_eval)
    s = sub.add_parser("serve")
    s.add_argument("--versions", default="v0-rules,v1-llm,v2-llm")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8095)
    s.set_defaults(fn=cmd_serve)
    a = p.parse_args(argv)
    a.fn(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
