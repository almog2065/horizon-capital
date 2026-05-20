from app import config, ops_alerts, ops_db


def _use_tmp_ops(tmp_path, monkeypatch):
    db_path = tmp_path / "artifacts" / "ops.sqlite"
    monkeypatch.setattr(config, "OPS_DB", db_path)
    monkeypatch.setattr(config, "ARTIFACTS", db_path.parent)
    ops_db.init_db()


def test_get_alert(tmp_path, monkeypatch):
    _use_tmp_ops(tmp_path, monkeypatch)
    a = ops_alerts.record(code="x", message="m", severity="warning")
    assert ops_alerts.get_alert(a["alert_id"])["code"] == "x"
    assert ops_alerts.get_alert("missing") is None


def test_record_and_ack(tmp_path, monkeypatch):
    _use_tmp_ops(tmp_path, monkeypatch)
    a = ops_alerts.record(code="test_error", message="something failed", severity="error")
    assert a["alert_id"]
    listed = ops_alerts.list_alerts(unacked_only=True)
    assert any(x["alert_id"] == a["alert_id"] for x in listed)
    assert ops_alerts.acknowledge(a["alert_id"])
    assert not any(
        x["alert_id"] == a["alert_id"] and not x.get("acknowledged")
        for x in ops_alerts.list_alerts(unacked_only=True)
    )


def test_acknowledge_all(tmp_path, monkeypatch):
    _use_tmp_ops(tmp_path, monkeypatch)
    ops_alerts.record(code="a", message="one", severity="warning")
    ops_alerts.record(code="b", message="two", severity="error")
    assert ops_alerts.summary()["open"] == 2
    closed = ops_alerts.acknowledge_all()
    assert closed == 2
    assert ops_alerts.summary()["open"] == 0
