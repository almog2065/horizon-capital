"""LLM client wrapper. Returns structured JSON.

Records every call as a local trace event AND nested LangSmith llm span.
Falls back to deterministic mock when no API key, or after any live API
failure (invalid key, rate limit, etc.) for the rest of the process.
"""
from __future__ import annotations
import json
import re
import time
from typing import Any, Optional
from . import config, traces

_client = None
_runtime_mock = False
_runtime_mock_reason: Optional[str] = None
_degraded_alert_recorded = False


def _live_enabled() -> bool:
    return (
        not config.USE_MOCK_LLM
        and not _runtime_mock
        and bool(config.OPENAI_API_KEY.strip())
    )


def _switch_to_runtime_mock(reason: str, error: Optional[str] = None) -> None:
    """Disable live LLM for this process; emit a single ops alert."""
    global _client, _runtime_mock, _runtime_mock_reason, _degraded_alert_recorded
    if _runtime_mock:
        return
    _runtime_mock = True
    _runtime_mock_reason = reason
    _client = None
    config.USE_MOCK_LLM = True
    try:
        from . import rag
        rag.reset_openai_client()
    except Exception:
        pass
    print(
        f"[llm] Live API unavailable ({reason}); "
        f"using mock for this session."
        + (f" Error: {error}" if error else "")
    )
    if not _degraded_alert_recorded:
        from . import ops_alerts
        msg = (
            "OpenAI call failed — switched to deterministic mock for this session. "
            "Fix OPENAI_API_KEY or set USE_MOCK_LLM=1 intentionally."
        )
        if error:
            msg += f" ({error[:200]})"
        ops_alerts.record(
            code="llm_degraded_to_mock",
            message=msg,
            severity="warning",
            source="llm",
            context={"reason": reason},
        )
        _degraded_alert_recorded = True


def _get_client():
    global _client
    if not _live_enabled():
        return None
    if _client is None:
        try:
            from openai import OpenAI
            _client = OpenAI(api_key=config.OPENAI_API_KEY)
        except Exception as e:
            print(f"[llm] OpenAI init failed: {e}")
            _switch_to_runtime_mock("client_init_failed", str(e))
            _client = None
    return _client


def probe_at_startup() -> bool:
    """Verify the configured API key once at boot. Returns True if live."""
    if not _live_enabled():
        return False
    client = _get_client()
    if client is None:
        return False
    try:
        client.models.list()
        return True
    except Exception as e:
        _switch_to_runtime_mock("startup_probe_failed", str(e))
        return False


def _strip_code_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s


def chat_json(system: str, user: str,
              expected_schema_hint: Optional[str] = None,
              mock_fallback: Optional[dict] = None,
              model: Optional[str] = None,
              purpose: str = "agent_call") -> dict:
    """Call LLM, get structured JSON back.

    Model resolution is **cost-aware**: see `app/model_routing.py`. The
    `purpose` argument drives routing — different agents can target
    different models. Override per-deployment with
    `MODEL_FOR_<PURPOSE>=...` env vars.
    """
    t0 = time.time()
    # Cost-aware routing (brief bonus). Falls back to OPENAI_MODEL.
    from . import model_routing
    used_model = model_routing.model_for(purpose, explicit=model)

    if not _live_enabled() or _get_client() is None:
        out = mock_fallback if mock_fallback is not None else {
            "_mock": True, "_error": "no_fallback_supplied"}
        duration_ms = int((time.time() - t0) * 1000)
        traces.record_llm_call(
            purpose=purpose,
            model=used_model,
            mode="mock",
            system=system,
            user=user,
            response=out,
            duration_ms=duration_ms,
            tokens=None,
        )
        return out

    client = _get_client()
    try:
        resp = client.chat.completions.create(
            model=used_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        text = resp.choices[0].message.content or "{}"
        text = _strip_code_fence(text)
        out = json.loads(text)

        usage = getattr(resp, "usage", None)
        tokens = {
            "prompt": getattr(usage, "prompt_tokens", None) if usage else None,
            "completion": getattr(usage, "completion_tokens", None) if usage else None,
            "total": getattr(usage, "total_tokens", None) if usage else None,
        }
        duration_ms = int((time.time() - t0) * 1000)
        traces.record_llm_call(
            purpose=purpose,
            model=used_model,
            mode="live",
            system=system,
            user=user,
            response=out,
            duration_ms=duration_ms,
            tokens=tokens,
        )
        return out

    except Exception as e:
        _switch_to_runtime_mock(f"llm.{purpose}", str(e))
        duration_ms = int((time.time() - t0) * 1000)
        out = dict(mock_fallback) if mock_fallback is not None else {
            "_mock": True, "_llm_error": str(e)}
        if mock_fallback is not None:
            out["_llm_error"] = str(e)
        traces.record_llm_call(
            purpose=purpose,
            model=used_model,
            mode="mock",
            system=system,
            user=user,
            response=out,
            duration_ms=duration_ms,
            error=str(e),
        )
        return out


def is_mock() -> bool:
    return not _live_enabled() or _get_client() is None


def env_status() -> dict:
    return {
        "openai_api_key_set": bool(config.OPENAI_API_KEY),
        "use_mock_llm_flag": config.USE_MOCK_LLM,
        "runtime_mock": _runtime_mock,
        "runtime_mock_reason": _runtime_mock_reason,
        "client_ok": _get_client() is not None,
        "model": config.OPENAI_MODEL,
        "embedding_model": config.OPENAI_EMBEDDING_MODEL,
        "mock_active": is_mock(),
    }
