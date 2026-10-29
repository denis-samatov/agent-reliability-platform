"""Identity-bound keys cannot assert another requester's subject."""

from types import SimpleNamespace

from fastapi.testclient import TestClient

from arp.app.api import _keys, create_app


def test_bound_requester_key_rejects_impersonation():
    submitted = []
    platform = SimpleNamespace(workflows={"it-ticket@v0-rules": object()})
    platform.submit = lambda *args: submitted.append(args) or "run"
    client = TestClient(create_app(platform, {"key": ("acme", "requester", "u100")}), raise_server_exceptions=False)
    headers = {"X-API-Key": "key"}
    assert client.post("/v1/runs", headers=headers, json={"requester": "u101", "ticket": "help"}).status_code == 403
    assert submitted == []
    assert client.post("/v1/runs", headers=headers, json={"requester": "u100", "ticket": "help"}).status_code == 202
    assert submitted[0][:2] == ("acme", "u100")


def test_environment_keys_can_bind_an_individual_subject(monkeypatch):
    monkeypatch.setenv("ARP_API_KEYS", "bound:acme:requester:u100")
    assert _keys() == {"bound": ("acme", "requester", "u100")}


def test_empty_key_configuration_is_rejected():
    import pytest

    with pytest.raises(ValueError, match="empty"):
        create_app(SimpleNamespace(), {"": ("acme", "approver")})


def test_cancel_cannot_resurrect_a_concurrently_completed_run(tmp_path, monkeypatch):
    from arp.engine.store import Store

    store = Store(f"sqlite:///{tmp_path}/runs.db")
    run = store.create_run(
        tenant="acme", requester="u", workflow="test", version="v1", version_hash="hash", input={}, budget={}
    )
    store.update_run(run.id, status="running")
    original = store.get_run

    def observed_then_completed(rid):
        observed = original(rid)
        store.update_run(rid, status="succeeded", output={"ok": True})
        return observed

    monkeypatch.setattr(store, "get_run", observed_then_completed)
    client = TestClient(create_app(SimpleNamespace(store=store), {"key": ("acme", "user")}))
    response = client.post(f"/v1/runs/{run.id}/cancel", headers={"X-API-Key": "key"})
    assert response.status_code == 200
    final = original(run.id)
    assert final.status == "succeeded" and final.output == {"ok": True}
    assert not final.cancel_requested
