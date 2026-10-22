from __future__ import annotations

from fastapi.testclient import TestClient

from arp.app.api import create_app
from arp.app.ratelimit import TokenBucketLimiter, client_id
from arp.platform import Platform, PlatformConfig


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_bucket_allows_burst_then_refills_at_the_configured_rate():
    clock = Clock()
    lim = TokenBucketLimiter(per_minute=60, burst=3, clock=clock)
    assert [lim.take("a") for _ in range(3)] == [0.0, 0.0, 0.0]
    assert lim.take("a") > 0  # burst exhausted
    assert lim.take("b") == 0.0  # other clients are independent
    clock.t += 1.0  # one token per second at 60/min
    assert lim.take("a") == 0.0


def test_client_id_never_contains_the_key():
    cid = client_id("acme-req-secret", "1.2.3.4")
    assert "acme-req-secret" not in cid and cid.startswith("key:")
    assert client_id(None, "1.2.3.4") == "addr:1.2.3.4"


def test_api_returns_429_with_retry_after_and_keeps_probes_open(tmp_path):
    p = Platform(
        PlatformConfig(
            db_url=f"sqlite:///{tmp_path}/r.db",
            itsm_path=str(tmp_path / "i.db"),
            trace_dir=tmp_path / "t",
            versions=("v0-rules",),
        )
    )
    keys = {"acme-req": ("acme", "requester"), "globex-req": ("globex", "requester")}
    c = TestClient(create_app(p, keys, tmp_path, limiter=TokenBucketLimiter(per_minute=2, clock=Clock())))
    h = {"X-API-Key": "acme-req"}
    assert [c.get("/v1/runs", headers=h).status_code for _ in range(2)] == [200, 200]
    r = c.get("/v1/runs", headers=h)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert c.get("/v1/runs", headers={"X-API-Key": "globex-req"}).status_code == 200  # per-key buckets
    assert all(c.get("/health/live").status_code == 200 for _ in range(5))
