from fastapi.testclient import TestClient

from app import config, ops_alerts, ops_db
from app.api.app_factory import app


def test_alerts_list_and_detail(tmp_path, monkeypatch):
    base = tmp_path / "artifacts"
    monkeypatch.setattr(config, "OPS_DB", base / "ops.sqlite")
    monkeypatch.setattr(config, "ARTIFACTS", base)
    ops_db.init_db()
    a = ops_alerts.record(
        code="unhandled_exception",
        message="boom",
        severity="critical",
        source="/",
        context={"request_id": "abc123", "traceback": "Traceback...\nValueError: boom"},
    )
    client = TestClient(app)
    r = client.get("/alerts")
    assert r.status_code == 200
    assert a["alert_id"] in r.text
    detail = client.get(f"/alerts/{a['alert_id']}")
    assert detail.status_code == 200
    assert "Stack trace" in detail.text
    assert "abc123" in detail.text


def test_ack_all_button(tmp_path, monkeypatch):
    base = tmp_path / "artifacts"
    monkeypatch.setattr(config, "OPS_DB", base / "ops.sqlite")
    monkeypatch.setattr(config, "ARTIFACTS", base)
    ops_db.init_db()
    ops_alerts.record(code="x", message="one", severity="warning")
    ops_alerts.record(code="y", message="two", severity="error")
    client = TestClient(app)
    r = client.post("/alerts/ack-all", data={"return_to": "/alerts"}, follow_redirects=False)
    assert r.status_code == 303
    assert "unacked=true" in r.headers["location"]
    assert ops_alerts.summary()["open"] == 0
    page = client.get("/alerts")
    assert page.status_code == 200
    assert "No alerts match" in page.text or "one" not in page.text
