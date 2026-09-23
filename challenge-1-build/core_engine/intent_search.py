"""Hybrid (BM25 + semantic) search over the 38 gold questions, for a
question phrased in a way none of router.py's ~20 regex rules anticipated
-- "is captain A Nair available?" rather than "who is qualified as
captain" or "is C-1042 legal to cover P-2291". Every regex added for one
new phrasing just gets followed by the next one; matching against real,
already-answered example questions generalises instead of enumerating.

Knows nothing about `Intent`/`Route`/router.py on purpose, to avoid a
circular import (router.py needs to call this; this must not need
router.py) -- it only finds the closest example question. The caller
(router.py) is responsible for turning "this most resembles Q07" into an
actual intent, since only it has the deterministic classifications to map
question ids onto.

Backed by `intent_example_vec`, a real table (38 rows, one per gold
question) this project's original system already populated with both a
`search_tsv` column and a 384-dim `embedding` column.
"""

from __future__ import annotations

import os
from typing import Any

from core_engine import embeddings


def enabled() -> bool:
    return embeddings.enabled()


def best_matches(query: str, top_k: int = 3, alpha: float = 0.5) -> list[dict[str, Any]]:
    """The `top_k` gold questions closest to `query`, each with its own
    `question_id`, `tier`, and `blended_score`. `[]` if unconfigured --
    the caller treats that exactly like "no match found", not an error."""
    if not enabled():
        return []

    import psycopg

    query_vector = embeddings.embed_query(query)

    with psycopg.connect(
        os.getenv("LEDGER_DATABASE_URL"), autocommit=True, connect_timeout=10
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT question_id, prompt, tier,
                          ts_rank_cd(search_tsv, plainto_tsquery('english', %s)) AS bm25,
                          1 - (embedding <=> %s::vector) AS semantic
                   FROM intent_example_vec""",
                (query, str(query_vector)),
            )
            rows = cur.fetchall()

    if not rows:
        return []

    blended = embeddings.blend([r[3] for r in rows], [r[4] for r in rows], alpha)
    scored = [
        {"question_id": r[0], "prompt": r[1], "tier": r[2], "blended_score": round(blended[i], 3)}
        for i, r in enumerate(rows)
    ]
    scored.sort(key=lambda s: s["blended_score"], reverse=True)
    return scored[:top_k]
