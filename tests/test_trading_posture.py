from app import trading_posture


def test_deploy_posture_when_under_invested():
    firm = {
        "invested_pct": 0.62,
        "cash_pct": 0.38,
        "positions_count": 8,
        "policy": {
            "min_invested_pct": 0.70,
            "max_invested_pct": 0.92,
            "cash_ceiling_pct": 0.20,
            "cash_floor_pct": 0.05,
            "min_position_count": 10,
        },
        "deployment_needs": {
            "active": True,
            "need_deploy": True,
            "need_diversify": True,
        },
        "sectors": [],
        "concentration": [],
        "positions": [],
    }
    p = trading_posture.derive_posture(firm)
    assert p["mode"] in ("deploy", "diversify")
    assert p["knobs"]["risk_favor_auto_addon"] is True
    assert "risk_officer" in p["agent_guidance"]


def test_merge_scan_directives_boosts_deploy():
    posture = {"mode": "deploy", "knobs": {"max_new_openings_boost": 3}}
    sd = trading_posture.merge_scan_directives({"max_new_openings": 2}, posture)
    assert sd["max_new_openings"] == 5
    assert sd.get("deploy_urgency") == "high"
