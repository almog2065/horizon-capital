"""Runtime dossier writes must not touch read-only seed mount."""
from __future__ import annotations

from app import config, dossier_paths, ops_db


def _use_tmp_ops(tmp_path, monkeypatch):
    db_path = tmp_path / "artifacts" / "ops.sqlite"
    monkeypatch.setattr(config, "OPS_DB", db_path)
    monkeypatch.setattr(config, "ARTIFACTS", db_path.parent)
    ops_db.init_db()


def test_save_writes_db_not_seed(tmp_path, monkeypatch):
    seed = tmp_path / "data" / "dossiers"
    seed.mkdir(parents=True)
    (seed / "BTC.json").write_text('{"ticker":"BTC","sector":"Digital Assets"}')

    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(config, "DOSSIERS_SEED_DIR", seed)
    _use_tmp_ops(tmp_path, monkeypatch)

    dossier_paths.save("BTC", {"ticker": "BTC", "sector": "Digital Assets", "updated": True})
    hit = ops_db.get_dossier("BTC")
    assert hit is not None
    assert hit["dossier"]["updated"] is True
    assert "updated" not in (seed / "BTC.json").read_text()


def test_runtime_overrides_seed(tmp_path, monkeypatch):
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "X.json").write_text('{"ticker":"X","name":"seed"}')

    monkeypatch.setattr(config, "DOSSIERS_SEED_DIR", seed)
    _use_tmp_ops(tmp_path, monkeypatch)
    ops_db.upsert_dossier("X", {"ticker": "X", "name": "runtime"}, source="runtime")

    hit = dossier_paths.load("X")
    assert hit["dossier"]["name"] == "runtime"
