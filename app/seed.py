"""Seed the RAG corpora and any starter state."""
from __future__ import annotations
import json
import re
from pathlib import Path
from . import bootstrap_data, config, rag, db


def _chunk_text(text: str, max_chars: int = 600) -> list[str]:
    """Naive paragraph chunker."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\n##|\n#", text) if p.strip()]
    chunks = []
    for p in paragraphs:
        if len(p) <= max_chars:
            chunks.append(p)
        else:
            for i in range(0, len(p), max_chars):
                chunks.append(p[i:i + max_chars])
    return chunks


def seed_policies():
    for p in sorted(config.POLICIES_DIR.glob("*.md")):
        text = p.read_text()
        chunks = _chunk_text(text)
        for i, chunk in enumerate(chunks):
            # Try to extract a section ref
            m = re.search(r"§\d+", chunk)
            section_ref = (p.stem + " " + m.group(0)) if m else p.stem
            rag.add(
                corpus="policy",
                chunk_id=f"{p.stem}::chunk_{i}",
                text=chunk,
                metadata={
                    "doc": p.stem,
                    "section_ref": section_ref,
                    "version": "1.0",
                },
            )


def seed_past_plans():
    for p in sorted(config.PAST_PLANS_DIR.glob("*.json")):
        plan = json.loads(p.read_text())
        text = (
            f"{plan['ticker']} ({plan['sector']}) plan {plan['plan_id']}: "
            f"{plan['thesis_summary']} "
            f"Outcome: realized_return={plan['outcome']['realized_return_pct']:.2%}, "
            f"holding_period={plan['outcome']['holding_period_days']}d, "
            f"thesis_validated={plan['outcome']['thesis_validated']}. "
            f"Lesson: {plan['lesson_learned']}"
        )
        rag.add(
            corpus="past_plans",
            chunk_id=plan["plan_id"],
            text=text,
            metadata={
                "ticker": plan["ticker"],
                "sector": plan["sector"],
                "outcome": plan["outcome"]["thesis_validated"],
            },
        )


def seed_filings():
    """Seed synthetic filing chunks from ``data/bootstrap/synthetic_filings.json``."""
    synthetic = bootstrap_data.load_synthetic_filings()
    for ticker, chunks in synthetic.items():
        for i, text in enumerate(chunks):
            rag.add(
                corpus="filings",
                chunk_id=f"{ticker}_filing_{i}",
                text=text,
                metadata={"ticker": ticker, "filing_type": "synthetic"},
            )


def seed_news():
    """Pre-seed news chunks from ``data/bootstrap/news_seeds.json``."""
    for row in bootstrap_data.load_news_seeds():
        rag.add("news", row["chunk_id"], row["text"], row.get("metadata") or {})


def seed_all():
    seed_policies()
    seed_past_plans()
    seed_filings()
    seed_news()


if __name__ == "__main__":
    db.init_db()
    rag.init_db()
    seed_all()
    print("Seeded:")
    for c in ("policy", "news", "filings", "past_plans"):
        print(f"  {c}: {rag.count(c)} chunks")
