"""Excel daily report — the firm's "second channel" of record.

Pure standard library: emits an XLSX as a ZIP archive of XML parts.
No openpyxl / xlsxwriter dependency, so the report can be generated
inside the worker container without inflating the image.

Schema (one workbook, three sheets):
  Summary     — header KPIs (NAV, P&L, vs SPY, max DD, n trades)
  Holdings    — current positions (ticker, qty, avg cost, mkt value, P&L)
  Trades      — recent trade history with citations + HITL flag

We deliberately keep formulas out of the file: the report is a
read-only snapshot for ops, not an editable model. Excel still opens
it natively, and any spreadsheet engine can ingest it.

Generated artifacts land under ARTIFACTS/reports/<date>/daily.xlsx.
"""
from __future__ import annotations

import io
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..core.logging import get_logger
from ..core.settings import get_settings

log = get_logger("horizon.reports.excel")

# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------
@dataclass
class DailyReport:
    """All numbers a daily report needs, plain Python types only."""

    generated_at: str
    window: str
    nav: float
    starting_nav: float
    pnl_absolute: float
    pnl_pct: float
    benchmark_pct: float
    holdings: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "window": self.window,
            "nav": self.nav,
            "starting_nav": self.starting_nav,
            "pnl_absolute": self.pnl_absolute,
            "pnl_pct": self.pnl_pct,
            "benchmark_pct": self.benchmark_pct,
            "holdings": self.holdings,
            "trades": self.trades,
        }


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
def build_daily_report(
    *,
    starting_nav: float,
    nav: float,
    benchmark_pct: float = 0.0,
    holdings: Iterable[dict[str, Any]] | None = None,
    trades: Iterable[dict[str, Any]] | None = None,
    window: str | None = None,
) -> DailyReport:
    """Pure constructor — same shape regardless of where the data came from."""
    pnl = nav - starting_nav
    pnl_pct = (pnl / starting_nav * 100.0) if starting_nav else 0.0
    return DailyReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        window=window or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        nav=float(nav),
        starting_nav=float(starting_nav),
        pnl_absolute=float(pnl),
        pnl_pct=float(pnl_pct),
        benchmark_pct=float(benchmark_pct),
        holdings=list(holdings or []),
        trades=list(trades or []),
    )


# ---------------------------------------------------------------------------
# XLSX writer — stdlib only.
# ---------------------------------------------------------------------------
_SHEET_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>{rows}</sheetData>
</worksheet>"""

_WORKBOOK_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>{sheets}</sheets>
</workbook>"""

_WB_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>"""

_ROOT_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  {sheet_overrides}
</Types>"""


def _col(idx: int) -> str:
    """0-based column index → Excel letter (A, B, ..., Z, AA, ...)."""
    s = ""
    n = idx
    while True:
        s = chr(ord("A") + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s


# Match numeric strings, but reject strings with a leading zero (which
# would be displayed as a plain integer by Excel, losing the format).
# Examples that match:    "12", "12.5", "-12.5", "0", "0.5"
# Examples that DON'T:    "00123", "007.5", "1e5", "NaN"
_NUM_RE = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")


def _cell_xml(col: str, row: int, value: Any) -> str:
    """Render one <c> element. Inline strings keep the writer tiny."""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        text = "TRUE" if value else "FALSE"
        return f'<c r="{col}{row}" t="inlineStr"><is><t>{text}</t></is></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Guard against NaN/Inf which Excel cannot represent.
        import math
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return f'<c r="{col}{row}" t="inlineStr"><is><t>{value}</t></is></c>'
        return f'<c r="{col}{row}"><v>{value}</v></c>'
    s = str(value)
    if _NUM_RE.match(s):
        return f'<c r="{col}{row}"><v>{s}</v></c>'
    return f'<c r="{col}{row}" t="inlineStr"><is><t>{escape(s)}</t></is></c>'


def _rows_xml(rows: Sequence[Sequence[Any]]) -> str:
    out: list[str] = []
    for r_idx, row in enumerate(rows, start=1):
        cells = "".join(_cell_xml(_col(c_idx), r_idx, v) for c_idx, v in enumerate(row))
        out.append(f'<row r="{r_idx}">{cells}</row>')
    return "".join(out)


def _summary_rows(rep: DailyReport) -> list[list[Any]]:
    return [
        ["Horizon Capital — Daily Report"],
        ["Generated", rep.generated_at],
        ["Window", rep.window],
        [],
        ["Metric", "Value"],
        ["Starting NAV", rep.starting_nav],
        ["Ending NAV", rep.nav],
        ["P&L ($)", rep.pnl_absolute],
        ["P&L (%)", rep.pnl_pct],
        ["Benchmark (%)", rep.benchmark_pct],
        ["Excess return (%)", rep.pnl_pct - rep.benchmark_pct],
        ["# Holdings", len(rep.holdings)],
        ["# Trades", len(rep.trades)],
    ]


def _holdings_rows(rep: DailyReport) -> list[list[Any]]:
    rows: list[list[Any]] = [["Ticker", "Qty", "Avg Cost", "Last Price", "Market Value", "Unrealized P&L"]]
    for h in rep.holdings:
        qty = float(h.get("qty", 0))
        avg = float(h.get("avg_cost", 0))
        last = float(h.get("last_price", avg))
        mv = qty * last
        upnl = (last - avg) * qty
        rows.append([h.get("ticker", ""), qty, avg, last, mv, upnl])
    return rows


def _trades_rows(rep: DailyReport) -> list[list[Any]]:
    rows: list[list[Any]] = [
        ["Timestamp", "Ticker", "Side", "Qty", "Price", "Realized P&L", "HITL", "Citations"],
    ]
    for t in rep.trades:
        cits = t.get("citations") or []
        rows.append([
            t.get("ts", ""),
            t.get("ticker", ""),
            t.get("side", ""),
            float(t.get("qty", 0)),
            float(t.get("price", 0)),
            t.get("realized_pnl"),
            bool(t.get("hitl")),
            ", ".join(map(str, cits)) if cits else "",
        ])
    return rows


def write_daily_report_xlsx(rep: DailyReport, out_path: str | Path | None = None) -> Path:
    """Write the report as an XLSX file. Returns the written path."""
    cfg = get_settings()
    if out_path is None:
        day = rep.window or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out_path = cfg.artifacts_dir() / "reports" / day / "daily.xlsx"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sheets = {
        "Summary": _summary_rows(rep),
        "Holdings": _holdings_rows(rep),
        "Trades": _trades_rows(rep),
    }

    workbook_sheets_xml = "".join(
        f'<sheet name="{name}" sheetId="{i+1}" r:id="rId{i+1}"/>'
        for i, name in enumerate(sheets)
    )
    wb_rels_xml = "".join(
        f'<Relationship Id="rId{i+1}" '
        f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{i+1}.xml"/>'
        for i in range(len(sheets))
    )
    sheet_overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i+1}.xml" '
        f'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(len(sheets))
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML.format(sheet_overrides=sheet_overrides))
        zf.writestr("_rels/.rels", _ROOT_RELS_XML)
        zf.writestr("xl/workbook.xml", _WORKBOOK_XML.format(sheets=workbook_sheets_xml))
        zf.writestr("xl/_rels/workbook.xml.rels", _WB_RELS_XML.format(rels=wb_rels_xml))
        for i, (_, rows) in enumerate(sheets.items()):
            zf.writestr(
                f"xl/worksheets/sheet{i+1}.xml",
                _SHEET_XML.format(rows=_rows_xml(rows)),
            )

    out_path.write_bytes(buf.getvalue())
    # Sibling JSON so machines have a clean view of the same data.
    sidecar = out_path.with_suffix(".json")
    sidecar.write_text(json.dumps(rep.as_dict(), indent=2, default=str), encoding="utf-8")
    log.info(
        "daily-report-written path=%s bytes=%d window=%s",
        out_path, out_path.stat().st_size, rep.window,
    )
    return out_path
