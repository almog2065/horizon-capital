"""Simple vector store on SQLite + numpy. Works with OpenAI embeddings or hash-based fallback."""
from __future__ import annotations
import sqlite3
import json
import hashlib
import struct
import math
import time
from pathlib import Path
from typing import Optional
import numpy as np
from . import config

EMBED_DIM_OPENAI = 1536  # text-embedding-3-small
EMBED_DIM_MOCK = 256

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    corpus TEXT,
    text TEXT,
    metadata TEXT,
    embedding BLOB,
    embed_dim INTEGER,
    ts REAL
);
CREATE INDEX IF NOT EXISTS idx_chunks_corpus ON chunks(corpus);
"""


def init_db():
    config.VECTOR_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.VECTOR_DB) as c:
        c.executescript(SCHEMA)


def _embed_mock(text: str) -> np.ndarray:
    """Deterministic hash-based embedding. Useful when no API key.
    Quality is poor but functional for demo."""
    # Token-based bag of features into a fixed dim
    vec = np.zeros(EMBED_DIM_MOCK, dtype=np.float32)
    tokens = [t.lower() for t in text.split() if t.strip()]
    for tok in tokens:
        h = int(hashlib.sha256(tok.encode()).hexdigest()[:8], 16)
        idx = h % EMBED_DIM_MOCK
        vec[idx] += 1.0
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec


_openai_client = None


def reset_openai_client() -> None:
    global _openai_client
    _openai_client = None


def _get_openai_client():
    global _openai_client
    from . import llm
    if _openai_client is None and config.OPENAI_API_KEY and not llm.is_mock():
        try:
            from openai import OpenAI
            _openai_client = OpenAI(api_key=config.OPENAI_API_KEY)
        except Exception:
            _openai_client = None
    return _openai_client


def _embed_openai(text: str) -> Optional[np.ndarray]:
    client = _get_openai_client()
    if client is None:
        return None
    try:
        resp = client.embeddings.create(
            model=config.OPENAI_EMBEDDING_MODEL,
            input=text,
        )
        return np.array(resp.data[0].embedding, dtype=np.float32)
    except Exception:
        return None


def embed(text: str) -> tuple[np.ndarray, int]:
    """Return (vector, dim). Falls back to mock if OpenAI unavailable."""
    from . import llm
    if not llm.is_mock():
        vec = _embed_openai(text)
        if vec is not None:
            return vec, len(vec)
    return _embed_mock(text), EMBED_DIM_MOCK


def add(corpus: str, chunk_id: str, text: str, metadata: dict):
    vec, dim = embed(text)
    blob = vec.tobytes()
    with sqlite3.connect(config.VECTOR_DB) as c:
        c.execute(
            "INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?,?)",
            (chunk_id, corpus, text, json.dumps(metadata), blob, dim, time.time()),
        )


def search(corpus: str, query: str, top_k: int = 5,
           metadata_filter: Optional[dict] = None) -> list[dict]:
    from . import traces

    t0 = time.time()
    from . import llm
    embed_mode = "openai" if not llm.is_mock() else "mock_hash"

    qvec, qdim = embed(query)

    with sqlite3.connect(config.VECTOR_DB) as c:
        rows = c.execute(
            "SELECT chunk_id, text, metadata, embedding, embed_dim FROM chunks "
            "WHERE corpus=? AND embed_dim=?", (corpus, qdim)
        ).fetchall()

    if not rows:
        traces.record_rag_search(
            corpus, query, [], top_k,
            int((time.time() - t0) * 1000),
            metadata_filter, embed_mode,
        )
        return []

    results = []
    for chunk_id, text, meta_json, blob, dim in rows:
        vec = np.frombuffer(blob, dtype=np.float32)
        meta = json.loads(meta_json) if meta_json else {}
        if metadata_filter:
            skip = False
            for k, v in metadata_filter.items():
                if isinstance(v, dict):
                    if "gte" in v and meta.get(k, 0) < v["gte"]:
                        skip = True; break
                    if "lte" in v and meta.get(k, 1e18) > v["lte"]:
                        skip = True; break
                else:
                    if meta.get(k) != v:
                        skip = True; break
            if skip:
                continue
        denom = (np.linalg.norm(qvec) * np.linalg.norm(vec)) or 1e-9
        score = float(np.dot(qvec, vec) / denom)
        results.append({
            "chunk_id": chunk_id,
            "score": score,
            "text": text,
            "metadata": meta,
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    top = results[:top_k]
    traces.record_rag_search(
        corpus, query, top, top_k,
        int((time.time() - t0) * 1000),
        metadata_filter, embed_mode,
    )
    return top


def list_chunks(corpus: str) -> list[dict]:
    with sqlite3.connect(config.VECTOR_DB) as c:
        rows = c.execute(
            "SELECT chunk_id, metadata FROM chunks WHERE corpus=?", (corpus,)
        ).fetchall()
        return [{"chunk_id": cid, "metadata": json.loads(m or "{}")} for cid, m in rows]


def count(corpus: str) -> int:
    with sqlite3.connect(config.VECTOR_DB) as c:
        row = c.execute("SELECT COUNT(*) FROM chunks WHERE corpus=?", (corpus,)).fetchone()
        return row[0] if row else 0


def delete_corpus(corpus: str) -> int:
    """Remove all chunks in a corpus (e.g. before re-seeding policies)."""
    with sqlite3.connect(config.VECTOR_DB) as c:
        cur = c.execute("DELETE FROM chunks WHERE corpus=?", (corpus,))
        c.commit()
        return cur.rowcount
