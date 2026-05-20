"""Report sinks (output channels).

The brief requires the firm to deliver reports through **at least two
channels**. We expose three:

  1. **Web UI**  — `app/main.py` + `app/templates/*.html`. Real-time.
  2. **Excel**   — `app/reports/excel_reporter.py`. Daily snapshot, ops-friendly.
  3. **Structured logs (JSON lines)** — `app/core/logging.py`. Anything that
     ingests a logs stream (Slack via webhook, email, S3, Loki, CloudWatch).

Channels are *pull-able* (the Excel reporter renders on demand) and
*push-able* (logs stream continuously). New channels (Slack webhook,
email digest) plug in by adding a module here and a Makefile target.
"""

from .excel_reporter import (
    DailyReport,
    build_daily_report,
    write_daily_report_xlsx,
)

__all__ = [
    "DailyReport",
    "build_daily_report",
    "write_daily_report_xlsx",
]
