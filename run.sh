#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# Pick a python
PY=${PYTHON:-python3}
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "ERROR: $PY not found. Set PYTHON to your python binary, e.g. PYTHON=python3.12 ./run.sh"
  exit 1
fi

# Create venv if missing
if [ ! -d ".venv" ]; then
  echo "[run] Creating venv with $PY ..."
  "$PY" -m venv .venv
fi

# Activate
# shellcheck source=/dev/null
source .venv/bin/activate

# Install deps (idempotent — pip skips already-satisfied packages)
echo "[run] Verifying dependencies..."
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

# Sanity check imports up front so we fail fast if anything is wrong.
# Note: python-multipart's import name is `multipart`.
python - <<'PY'
import sys
# Required: server can't start without these.
required = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("openai", "openai"),
    ("langgraph", "langgraph"),
    ("pydantic", "pydantic"),
    ("jinja2", "jinja2"),
    ("numpy", "numpy"),
    ("dotenv", "python-dotenv"),
    ("multipart", "python-multipart"),
]
# Optional: API discovery still works without them (falls back to mock),
# but we warn loudly so the user knows real data isn't flowing.
optional = [
    ("yfinance", "yfinance"),
]
missing_required = []
missing_optional = []
for mod, pkg in required:
    try:
        __import__(mod)
    except ImportError:
        missing_required.append(pkg)
for mod, pkg in optional:
    try:
        __import__(mod)
    except ImportError:
        missing_optional.append(pkg)
if missing_required:
    print(f"[run] ERROR — missing required deps: {missing_required}")
    print(f"[run] Install with: pip install {' '.join(missing_required)}")
    sys.exit(1)
if missing_optional:
    print(f"[run] WARNING — optional deps missing: {missing_optional}")
    print(f"[run]   Idea Generator will fall back to mock data.")
    print(f"[run]   Enable real market data: pip install {' '.join(missing_optional)}")
else:
    print("[run] All imports OK (including yfinance for real market data)")
PY

# RAG bootstrap (idempotent — seeds only if corpora missing or policy files changed)
python -c "from app.rag_bootstrap import ensure_ready; ensure_ready()" || true

# Run server
echo ""
echo "============================================================"
echo "  Horizon Capital — http://127.0.0.1:8000"
echo "  Diagnostics:      http://127.0.0.1:8000/diagnostics"
echo "  Trigger news:     http://127.0.0.1:8000/trigger"
echo "============================================================"
if [ -z "$OPENAI_API_KEY" ] && [ -z "$(grep -E '^OPENAI_API_KEY=.+$' .env 2>/dev/null)" ]; then
  echo "  LLM mode: MOCK (no OpenAI key). Copy .env.example to .env and add"
  echo "  OPENAI_API_KEY to switch to LIVE."
else
  echo "  LLM mode: LIVE"
fi
echo ""

exec uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
