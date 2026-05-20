"""Excel reporter — round-trip and structural tests.

The reporter writes a valid OOXML zip with stdlib only. We verify:
  * round-trip: build → write → re-read JSON sidecar matches input
  * XML parts parse cleanly
  * cell values present
"""
from __future__ import annotations

import json
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from app.reports.excel_reporter import build_daily_report, write_daily_report_xlsx


def _sample_report():
    return build_daily_report(
        starting_nav=1_000_000.0,
        nav=1_005_500.0,
        benchmark_pct=0.45,
        holdings=[
            {"ticker": "MSFT", "qty": 100, "avg_cost": 410.0, "last_price": 415.5},
            {"ticker": "NVDA", "qty": 50, "avg_cost": 880.0, "last_price": 892.3},
        ],
        trades=[
            {"ticker": "MSFT", "side": "buy", "qty": 100, "price": 410.0, "hitl": False, "citations": ["a"]},
            {"ticker": "MSFT", "side": "sell", "qty": 100, "price": 415.5, "realized_pnl": 550.0, "hitl": False, "citations": ["plan:x"]},
        ],
        window="2026-05-19",
    )


def test_build_daily_report_math():
    rep = _sample_report()
    assert rep.pnl_absolute == 5500.0
    assert abs(rep.pnl_pct - 0.55) < 1e-9
    assert rep.window == "2026-05-19"
    assert len(rep.holdings) == 2
    assert len(rep.trades) == 2


def test_write_xlsx_produces_valid_zip(tmp_path: Path):
    rep = _sample_report()
    out = tmp_path / "daily.xlsx"
    written = write_daily_report_xlsx(rep, out)
    assert written == out
    assert written.exists()
    assert written.stat().st_size > 0

    with zipfile.ZipFile(written) as zf:
        names = set(zf.namelist())
        # required OOXML parts
        for required in [
            "[Content_Types].xml",
            "_rels/.rels",
            "xl/workbook.xml",
            "xl/_rels/workbook.xml.rels",
            "xl/worksheets/sheet1.xml",
            "xl/worksheets/sheet2.xml",
            "xl/worksheets/sheet3.xml",
        ]:
            assert required in names, f"missing OOXML part: {required}"

        # XML well-formed
        for n in names:
            if n.endswith(".xml") or n.endswith(".rels"):
                with zf.open(n) as fh:
                    ET.parse(fh)


def test_write_xlsx_sidecar_json(tmp_path: Path):
    rep = _sample_report()
    out = tmp_path / "daily.xlsx"
    write_daily_report_xlsx(rep, out)
    sidecar = out.with_suffix(".json")
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    assert data["pnl_absolute"] == 5500.0
    assert len(data["holdings"]) == 2
    assert len(data["trades"]) == 2


def test_write_xlsx_has_ticker_values(tmp_path: Path):
    """Spot-check that ticker symbols are actually written into the XML."""
    rep = _sample_report()
    out = tmp_path / "daily.xlsx"
    write_daily_report_xlsx(rep, out)

    with zipfile.ZipFile(out) as zf:
        holdings_sheet = zf.read("xl/worksheets/sheet2.xml").decode("utf-8")
        assert "MSFT" in holdings_sheet
        assert "NVDA" in holdings_sheet


def test_leading_zero_strings_preserved_as_text(tmp_path: Path):
    """Regression: '00123' must stay text, not become a number that
    Excel displays as 123 (losing the leading zeros). Important for
    SEC accession numbers, padded IDs, etc."""
    from app.reports.excel_reporter import _cell_xml
    cell = _cell_xml("A", 1, "00123")
    assert "inlineStr" in cell
    assert "00123" in cell
    # Plain integer strings are still allowed as numeric.
    cell2 = _cell_xml("A", 1, "123")
    assert "<v>123</v>" in cell2


def test_nan_and_inf_dont_break_xlsx(tmp_path: Path):
    """NaN / Inf must not produce invalid XML."""
    from app.reports.excel_reporter import _cell_xml
    nan_cell = _cell_xml("A", 1, float("nan"))
    assert "<v>" not in nan_cell  # NaN can't be a numeric <v>
    inf_cell = _cell_xml("A", 1, float("inf"))
    assert "<v>" not in inf_cell


def test_default_output_path(tmp_path, monkeypatch):
    """When out_path is None the writer picks artifacts_dir."""
    # patch artifacts_dir to point at the tmp_path
    from app.reports import excel_reporter as er

    class _Cfg:
        def artifacts_dir(self):
            return tmp_path

    monkeypatch.setattr(er, "get_settings", lambda: _Cfg())
    rep = _sample_report()
    written = write_daily_report_xlsx(rep)
    assert written.parent.parent == tmp_path / "reports"
    assert written.name == "daily.xlsx"
