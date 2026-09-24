"""Hybrid (BM25 + semantic) search over the 7 legality rules, for a
paraphrased legality question that never names a rule id — "can duty run
long on a short day" rather than "what does RULE-FDP-01 say". `explain_rule`
in `port.py` already handles the exact-id case perfectly and stays
untouched; this is additive, for the one case it can't cover.

Backed by `rules_vec` in the shared Postgres ledger, a real table this
project's original system already populated (7 rows, one per rule, each
with both a `search_tsv` full-text column and a 384-dim `embedding`
column), not something built fresh for this feature.
"""

from __future__ import annotations

from typing import Any

from core_engine import embeddings


def enabled() -> bool:
    return embeddings.enabled()


def search_rules(query: str, top_k: int = 3, alpha: float = 0.5) -> list[dict[str, Any]]:
    """The `top_k` rules best matching `query`, ranked by
    `alpha * BM25 + (1 - alpha) * semantic`. Returns `[]` if the ledger or
    the embedding model isn't configured. Missing config is a no-op here,
    not an error, same as every other Postgres-backed feature in this repo."""
    if not enabled():
        return []

    import psycopg

    query_vector = embeddings.embed_query(query)

    with psycopg.connect(
        __import__("os").getenv("LEDGER_DATABASE_URL"), autocommit=True, connect_timeout=10
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT rule_id, text, params,
                          ts_rank_cd(search_tsv, plainto_tsquery('english', %s)) AS bm25,
                          1 - (embedding <=> %s::vector) AS semantic
                   FROM rules_vec""",
                (query, str(query_vector)),
            )
            rows = cur.fetchall()

    if not rows:
        return []

    blended = embeddings.blend([r[3] for r in rows], [r[4] for r in rows], alpha)
    scored = [
        {"rule_id": r[0], "text": r[1], "params": r[2], "blended_score": round(blended[i], 3)}
        for i, r in enumerate(rows)
    ]
    scored.sort(key=lambda s: s["blended_score"], reverse=True)
    return scored[:top_k]
