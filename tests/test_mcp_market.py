from app.mcp_market import bridge, registry


def test_registry_lists_free_providers():
    providers = registry.list_native_providers()
    ids = {p.id for p in providers}
    assert "yfinance" in ids
    assert "sec_edgar" in ids
    assert "coingecko" in ids
    assert all(not p.requires_key for p in providers)


def test_provider_status_shape():
    st = bridge.provider_status()
    assert "providers" in st
    assert "enabled" in st
    assert len(st["providers"]) >= 5


def test_list_providers_api():
    rows = bridge.list_providers()
    assert any(r["id"] == "frankfurter" for r in rows)
