"""Hybrid (BM25 + semantic) search over the 6 engineered scenarios, to
classify what *kind* of disruption a freeform description is -- several
call sites elsewhere in this pipeline hardcode `event_type="SICK_CREW"`
regardless of what actually happened, which is wrong the moment a
controller describes a station closure or a tech delay instead.

Hard boundary, not a suggestion: `answer_key` in `scenario_precedent_vec`
is that PAST scenario's own frozen answer -- specific crew ids and costs
that were correct for that scenario's data state, not this one. This
module returns only `event_type` (and `scenario_id`/`difficulty` for
context) and deliberately never returns `answer_key` at all, so nothing
downstream can accidentally surface a stale precedent's numbers as if
they were a fresh, current answer. A real answer always still comes from
calling find_options()/ripple() fresh against live data.
"""

from __future__ import annotations

import os
from typing import Any

from core_engine import embeddings


def enabled() -> bool:
    return embeddings.enabled()


def classify_event(text: str, top_k: int = 1) -> list[dict[str, Any]]:
    """The `top_k` engineered scenarios closest to `text`, each with
    `scenario_id`, `event_type`, `difficulty`, and `blended_score` --
    `answer_key` is intentionally excluded from what this returns."""
    if not enabled():
        return []

    import psycopg

    query_vector = embeddings.embed_query(text)

    with psycopg.connect(
        os.getenv("LEDGER_DATABASE_URL"), autocommit=True, connect_timeout=10
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT scenario_id, event_type, difficulty,
                          ts_rank_cd(search_tsv, plainto_tsquery('english', %s)) AS bm25,
                          1 - (embedding <=> %s::vector) AS semantic
                   FROM scenario_precedent_vec""",
                (text, str(query_vector)),
            )
            rows = cur.fetchall()

    if not rows:
        return []

    blended = embeddings.blend([r[3] for r in rows], [r[4] for r in rows], 0.5)
    scored = [
        {"scenario_id": r[0], "event_type": r[1], "difficulty": r[2],
         "blended_score": round(blended[i], 3)}
        for i, r in enumerate(rows)
    ]
    scored.sort(key=lambda s: s["blended_score"], reverse=True)
    return scored[:top_k]
