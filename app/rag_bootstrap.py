"""Startup RAG readiness: verify corpora and seed if needed."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import config, rag

CORPORA = ("policy", "news", "filings", "past_plans")

# Minimum chunks required for the demo to function
MIN_CHUNKS: dict[str, int] = {
    "policy": 1,
    "news": 1,
    "filings": 1,
    "past_plans": 0,  # optional if data/past_plans is empty
}


def _policy_files_on_disk() -> list[Path]:
    if not config.POLICIES_DIR.exists():
        return []
    return sorted(config.POLICIES_DIR.glob("*.md"))


def _indexed_policy_docs() -> set[str]:
    docs: set[str] = set()
    for ch in rag.list_chunks("policy"):
        doc = (ch.get("metadata") or {}).get("doc")
        if doc:
            docs.add(doc)
    return docs


def _past_plan_files_on_disk() -> int:
    if not config.PAST_PLANS_DIR.exists():
        return 0
    return len(list(config.PAST_PLANS_DIR.glob("*.json")))


def status() -> dict[str, Any]:
    """Snapshot of vector store vs source files on disk."""
    counts = {c: rag.count(c) for c in CORPORA}
    on_disk = [p.stem for p in _policy_files_on_disk()]
    indexed = _indexed_policy_docs()
    missing_policies = sorted(set(on_disk) - indexed)
    past_files = _past_plan_files_on_disk()

    issues: list[str] = []
    for c in CORPORA:
        if counts[c] < MIN_CHUNKS[c]:
            issues.append(f"{c}: {counts[c]} chunks (need ≥{MIN_CHUNKS[c]})")
    if missing_policies:
        issues.append(f"policy files not indexed: {', '.join(missing_policies)}")
    if past_files and counts["past_plans"] < past_files:
        issues.append(
            f"past_plans: {counts['past_plans']} chunks vs {past_files} files on disk"
        )

    ready = len(issues) == 0
    return {
        "ready": ready,
        "vector_db": str(config.VECTOR_DB),
        "counts": counts,
        "policy_files_on_disk": on_disk,
        "policy_docs_indexed": sorted(indexed),
        "missing_policy_docs": missing_policies,
        "past_plan_files": past_files,
        "issues": issues,
    }


def ensure_ready() -> dict[str, Any]:
    """Initialize vector DB and seed any missing / stale corpora."""
    from . import seed

    rag.init_db()
    before = status()
    if before["ready"]:
        print("[startup] RAG ready — no seed needed")
        for c in CORPORA:
            print(f"[startup]   {c}: {before['counts'][c]} chunks")
        return {
            "ready": True,
            "seeded": False,
            "actions": [],
            "before": before["counts"],
            "after": before["counts"],
        }

    actions: list[str] = []
    total = sum(before["counts"].values())

    if total == 0:
        print("[startup] RAG vector store empty — running full seed...")
        seed.seed_all()
        actions.append("seed_all")
    else:
        if (
            before["counts"]["policy"] < MIN_CHUNKS["policy"]
            or before["missing_policy_docs"]
        ):
            print(
                "[startup] RAG (re)seeding policies"
                + (f" — missing: {before['missing_policy_docs']}" if before["missing_policy_docs"] else "")
                + "...",
            )
            rag.delete_corpus("policy")
            seed.seed_policies()
            actions.append("seed_policies")

        if before["counts"]["news"] < MIN_CHUNKS["news"]:
            print("[startup] RAG seeding news corpus...")
            seed.seed_news()
            actions.append("seed_news")

        if before["counts"]["filings"] < MIN_CHUNKS["filings"]:
            print("[startup] RAG seeding filings corpus...")
            seed.seed_filings()
            actions.append("seed_filings")

        past_files = before["past_plan_files"]
        if past_files and before["counts"]["past_plans"] < past_files:
            print("[startup] RAG seeding past_plans corpus...")
            rag.delete_corpus("past_plans")
            seed.seed_past_plans()
            actions.append("seed_past_plans")

    after = status()
    if not after["ready"]:
        print(f"[startup] RAG warning — still not ready: {after['issues']}")
    else:
        print("[startup] RAG ready after seed")
        for c in CORPORA:
            print(f"[startup]   {c}: {after['counts'][c]} chunks")

    return {
        "ready": after["ready"],
        "seeded": bool(actions),
        "actions": actions,
        "before": before["counts"],
        "after": after["counts"],
        "issues": after["issues"],
    }
