"""Runtime fallback to mock LLM when OpenAI calls fail."""
from unittest.mock import MagicMock, patch

from app import config, llm, ops_alerts, ops_db


def _reset_llm_state(monkeypatch):
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.setattr(llm, "_runtime_mock", False)
    monkeypatch.setattr(llm, "_runtime_mock_reason", None)
    monkeypatch.setattr(llm, "_degraded_alert_recorded", False)


def _tmp_ops(tmp_path, monkeypatch):
    base = tmp_path / "artifacts"
    monkeypatch.setattr(config, "OPS_DB", base / "ops.sqlite")
    monkeypatch.setattr(config, "ARTIFACTS", base)
    ops_db.init_db()


def test_startup_probe_switches_to_mock(tmp_path, monkeypatch):
    _tmp_ops(tmp_path, monkeypatch)
    _reset_llm_state(monkeypatch)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-bad")
    monkeypatch.setattr(config, "USE_MOCK_LLM", False)

    mock_client = MagicMock()
    mock_client.models.list.side_effect = Exception("401 Incorrect API key")

    with patch.object(llm, "_get_client", return_value=mock_client):
        assert llm.probe_at_startup() is False

    assert llm.is_mock()
    alerts = ops_alerts.list_alerts()
    assert len(alerts) == 1
    assert alerts[0]["code"] == "llm_degraded_to_mock"
    assert "401" in alerts[0]["message"]


def test_chat_json_error_single_alert_and_mock(tmp_path, monkeypatch):
    _tmp_ops(tmp_path, monkeypatch)
    _reset_llm_state(monkeypatch)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-bad")
    monkeypatch.setattr(config, "USE_MOCK_LLM", False)

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = Exception("401 invalid key")
    fallback = {"score": 0.5, "_mock": True}

    with patch.object(llm, "_get_client", return_value=mock_client):
        out1 = llm.chat_json("sys", "user", mock_fallback=fallback, purpose="t1")
        out2 = llm.chat_json("sys", "user", mock_fallback=fallback, purpose="t2")

    assert out1["score"] == 0.5
    assert "_llm_error" in out1
    assert out2["score"] == 0.5
    assert llm.is_mock()
    assert mock_client.chat.completions.create.call_count == 1
    assert len(ops_alerts.list_alerts()) == 1
