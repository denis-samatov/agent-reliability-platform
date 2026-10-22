from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from arp.app.api import create_app
from arp.platform import Platform, PlatformConfig

KEYS = {"acme-req": ("acme", "requester"), "acme-appr": ("acme", "approver"), "globex-appr": ("globex", "approver")}


@pytest.fixture()
def client(tmp_path):
    p = Platform(
        PlatformConfig(
            db_url=f"sqlite:///{tmp_path}/r.db",
            itsm_path=str(tmp_path / "i.db"),
            trace_dir=tmp_path / "t",
            versions=("v0-rules",),
        )
    )
    return TestClient(create_app(p, KEYS, tmp_path)), p


def test_end_to_end_with_approval_and_tenant_isolation(client):
    c, p = client
    h = {"X-API-Key": "acme-req"}
    assert c.post("/v1/runs", json={"requester": "u100", "ticket": "x" * 5}).status_code == 401
    rid = c.post(
        "/v1/runs", headers=h, json={"requester": "u100", "ticket": "I need access to the finance-reports folder."}
    ).json()["run_id"]
    assert c.post(f"/v1/runs/{rid}/advance", headers=h).json()["status"] == "waiting_approval"
    appr = c.get("/v1/approvals", headers={"X-API-Key": "acme-appr"}).json()
    assert len(appr) == 1
    # requester role cannot approve; other tenant cannot see or decide
    assert c.post(f"/v1/approvals/{appr[0]['approval_id']}/decision", headers=h, json={"approve": True}).status_code == 403
    assert c.get("/v1/approvals", headers={"X-API-Key": "globex-appr"}).json() == []
    assert (
        c.post(
            f"/v1/approvals/{appr[0]['approval_id']}/decision", headers={"X-API-Key": "globex-appr"}, json={"approve": True}
        ).status_code
        == 404
    )
    assert c.get(f"/v1/runs/{rid}", headers={"X-API-Key": "globex-appr"}).status_code == 404
    r = c.post(
        f"/v1/approvals/{appr[0]['approval_id']}/decision", headers={"X-API-Key": "acme-appr"}, json={"approve": True}
    )
    assert r.json()["status"] == "succeeded"
    d = c.get(f"/v1/runs/{rid}", headers=h).json()
    assert d["output"]["executed"]["action"] == "grant_folder_access" and d["trace"]
    assert 'arp_runs{status="succeeded"} 1' in c.get("/metrics").text


def test_validation_and_unknown_version(client):
    c, _ = client
    h = {"X-API-Key": "acme-req"}
    assert c.post("/v1/runs", headers=h, json={"requester": "admin", "ticket": "hello"}).status_code == 422
    assert c.post("/v1/runs", headers=h, json={"requester": "u100", "ticket": "hello", "version": "v9"}).status_code == 422
