"""Cross-disruption state: which disruptions are open, who they're
considering, and what's been committed. This is the one piece of this
system that needs to be shared across controllers and processes, since
every other tool call is a pure function of the static roster.

Feature-gated on `LEDGER_DATABASE_URL`. Left unset, every function below is
a no-op, and `find_options`/`commit_decision` behave exactly as they did
before this file existed — so every existing test keeps passing with no
database configured.

Two new tables (`open_disruptions`, `open_disruption_candidates`) hold what
this repo's own dataset has no place for: the live set of disruptions being
worked right now, and who's a legal candidate for each, as of when that was
last computed. A committed decision does NOT go into a third table — it's
written straight into the same `pairing_crew` table the rest of this schema
already uses to mean "this crew member is assigned to this pairing", and
logged to the existing `controller_decisions` audit table. That's
deliberate: it means a commitment is honoured by `RULE-REST-04`'s existing
double-booking check for every pairing evaluated afterwards (see
`core_engine.world.assess`'s `extra_assigned` parameter), instead of by a
second, bespoke exclusivity rule that could drift out of sync with the
first.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator

_DATABASE_URL = os.getenv("LEDGER_DATABASE_URL")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS open_disruptions (
    disruption_id   TEXT PRIMARY KEY,
    pairing_id      TEXT,
    role            TEXT,
    event_type      TEXT NOT NULL,
    narrative       TEXT,
    opened_by       TEXT,
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS open_disruption_candidates (
    disruption_id   TEXT NOT NULL REFERENCES open_disruptions(disruption_id) ON DELETE CASCADE,
    crew_id         TEXT NOT NULL,
    rank            INT NOT NULL,
    cost_inr        INT NOT NULL,
    resilience      NUMERIC,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (disruption_id, crew_id)
);
CREATE TABLE IF NOT EXISTS disruption_commitments (
    pairing_id      TEXT PRIMARY KEY,
    disruption_id   TEXT NOT NULL,
    crew_id         TEXT NOT NULL,
    committed_by    TEXT NOT NULL,
    committed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def enabled() -> bool:
    return bool(_DATABASE_URL)


@contextmanager
def _conn() -> Iterator[Any]:
    import psycopg
    with psycopg.connect(_DATABASE_URL, autocommit=True, connect_timeout=10) as conn:
        yield conn


def ensure_schema() -> None:
    if not enabled():
        return
    with _conn() as conn:
        conn.execute(_SCHEMA)


# --------------------------------------------------------------------------
# Opening / refreshing a disruption's candidate pool
# --------------------------------------------------------------------------


def register_and_check_contention(
    disruption_id: str, pairing_id: str | None, role: str | None, event_type: str,
    narrative: str, opened_by: str, candidates: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """`open_disruption()` then `contention_for()`, on one connection instead
    of two. Same cross-region-latency reasoning as `live_assignments_bulk`:
    every `_conn()` call is a fresh TLS handshake to Neon, and without this,
    `find_options` would pay for three of them on every single page load."""
    if not enabled():
        return {}
    crew_ids = [c["crew_id"] for c in candidates]

    with _conn() as conn:
        # `opened_by` refreshes on every view like the other columns do.
        # The contention message ("also wanted by X's desk") should name
        # whoever most recently has eyes on it, not whoever happened to
        # view it first. The `CASE` guards against the one way that could
        # regress: server.py's own `"unknown"` fallback for a request with
        # no `?controller=` (a raw API call, not real UI navigation) must
        # never overwrite an already-known real name.
        conn.execute(
            """INSERT INTO open_disruptions
                   (disruption_id, pairing_id, role, event_type, narrative, opened_by)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (disruption_id) DO UPDATE SET
                   pairing_id = EXCLUDED.pairing_id, role = EXCLUDED.role,
                   event_type = EXCLUDED.event_type, narrative = EXCLUDED.narrative,
                   opened_by = CASE WHEN EXCLUDED.opened_by = 'unknown'
                                     THEN open_disruptions.opened_by
                                     ELSE EXCLUDED.opened_by END""",
            (disruption_id, pairing_id, role, event_type, narrative, opened_by),
        )
        conn.execute(
            "DELETE FROM open_disruption_candidates WHERE disruption_id = %s",
            (disruption_id,),
        )
        if candidates:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO open_disruption_candidates
                           (disruption_id, crew_id, rank, cost_inr, resilience)
                       VALUES (%s, %s, %s, %s, %s)""",
                    [(disruption_id, c["crew_id"], c["rank"], c["cost_inr"],
                      c.get("resilience")) for c in candidates],
                )

        if not crew_ids:
            return {}
        with conn.cursor() as cur:
            cur.execute(
                """SELECT dc.crew_id, dc.disruption_id, d.pairing_id, dc.rank, d.opened_by
                   FROM open_disruption_candidates dc
                   JOIN open_disruptions d ON d.disruption_id = dc.disruption_id
                   WHERE dc.crew_id = ANY(%s) AND dc.disruption_id != %s
                         AND d.status = 'open'""",
                (crew_ids, disruption_id),
            )
            rows = cur.fetchall()

    out: dict[str, list[dict[str, Any]]] = {}
    for crew_id, other_disruption, other_pairing, rank, other_opened_by in rows:
        out.setdefault(crew_id, []).append(
            {"disruption_id": other_disruption, "pairing_id": other_pairing, "rank": rank,
             "opened_by": other_opened_by})
    return out


def recent_decisions(limit: int = 50) -> list[dict[str, Any]]:
    """The last `limit` rows of the schema's own `controller_decisions`
    audit table. Every commit this console makes lands here, alongside
    anything the reference app itself has ever written to it."""
    if not enabled():
        return []
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT request_id, decided_at_utc, decision_type, accepted_rank,
                          custom_crew_id, committed_by
                   FROM controller_decisions
                   ORDER BY decided_at_utc DESC LIMIT %s""",
                (limit,),
            )
            rows = cur.fetchall()
    return [
        {"disruption_id": r[0], "decided_at_utc": r[1].isoformat(), "decision_type": r[2],
         "accepted_rank": r[3], "custom_crew_id": r[4], "committed_by": r[5]}
        for r in rows
    ]


def list_open_disruptions() -> list[dict[str, Any]]:
    if not enabled():
        return []
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT disruption_id, pairing_id, role, event_type, narrative,
                          opened_by, opened_at, status
                   FROM open_disruptions ORDER BY opened_at DESC"""
            )
            rows = cur.fetchall()
    return [
        {"disruption_id": r[0], "pairing_id": r[1], "role": r[2], "event_type": r[3],
         "narrative": r[4], "opened_by": r[5], "opened_at": r[6].isoformat(), "status": r[7]}
        for r in rows
    ]


# --------------------------------------------------------------------------
# Live commitments -- read side (feeds `assess`'s extra_assigned)
# --------------------------------------------------------------------------


def live_assignments_for(crew_id: str, exclude_pairing: str) -> list[str]:
    """Every OTHER pairing this crew member is live-committed to in
    `pairing_crew`, beyond the exclude_pairing. The caller then checks real
    duty-window overlap using the local dataset's own `duty_days`, since the
    two share the identical dataset.

    This includes both the ~206 rows the vendored dataset shipped with and
    any row a console commit has since added. `commitment_for()` is what
    tells the two apart, for a caller that needs to know whether a conflict
    is "this is their base schedule" or "another controller just took
    them"."""
    if not enabled():
        return []
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pairing_id FROM pairing_crew WHERE crew_id = %s AND pairing_id != %s",
                (crew_id, exclude_pairing),
            )
            return [r[0] for r in cur.fetchall()]


def live_assignments_bulk(crew_ids: list[str], exclude_pairing: str) -> dict[str, list[str]]:
    """The same thing as `live_assignments_for`, for every crew_id in
    `find_options`'s candidate pool in one round trip instead of one per
    candidate.

    Avoiding that N+1 pattern is not a micro-optimisation to skip: with
    ~20-30 candidates per role, one fresh TLS connection per candidate to a
    cross-region Postgres instance measured at 10+ seconds for a single page
    load — long enough that a controller clicking "Approve" would reasonably
    conclude the button does nothing. This is the one query that matters.
    """
    if not enabled() or not crew_ids:
        return {}
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT crew_id, pairing_id FROM pairing_crew WHERE crew_id = ANY(%s) AND pairing_id != %s",
                (crew_ids, exclude_pairing),
            )
            rows = cur.fetchall()
    out: dict[str, list[str]] = {}
    for crew_id, pairing_id in rows:
        out.setdefault(crew_id, []).append(pairing_id)
    return out


def commitment_for(pairing_id: str) -> dict[str, Any] | None:
    """Who committed which crew member to `pairing_id` via this console, if
    anyone. Returns `None` for a pairing whose crew is only ever the
    vendored dataset's own original assignment, which nobody "took"."""
    if not enabled():
        return None
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.disruption_id, c.crew_id, c.committed_by, c.committed_at,
                          d.accepted_rank, d.override_reason
                   FROM disruption_commitments c
                   LEFT JOIN controller_decisions d
                          ON d.request_id = c.disruption_id
                         AND d.decided_at_utc = (
                             SELECT max(decided_at_utc) FROM controller_decisions
                             WHERE request_id = c.disruption_id)
                   WHERE c.pairing_id = %s""",
                (pairing_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "pairing_id": pairing_id, "disruption_id": row[0], "crew_id": row[1],
        "committed_by": row[2], "committed_at": row[3].isoformat(),
        "accepted_rank": row[4], "override_reason": row[5],
    }


def commitments_bulk(pairing_ids: list[str]) -> dict[str, dict[str, Any]]:
    """`commitment_for()` for every pairing_id in `find_options`'s excluded
    list, in one round trip. The single-pairing version above is for the
    one genuinely single-lookup case (`commit_decision`'s own re-check).
    Calling it once per excluded candidate would repeat the same N+1
    mistake `live_assignments_bulk` exists to avoid, just at a different
    call site."""
    if not enabled() or not pairing_ids:
        return {}
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.pairing_id, c.disruption_id, c.crew_id, c.committed_by, c.committed_at,
                          d.accepted_rank, d.override_reason
                   FROM disruption_commitments c
                   LEFT JOIN controller_decisions d
                          ON d.request_id = c.disruption_id
                         AND d.decided_at_utc = (
                             SELECT max(decided_at_utc) FROM controller_decisions
                             WHERE request_id = c.disruption_id)
                   WHERE c.pairing_id = ANY(%s)""",
                (pairing_ids,),
            )
            rows = cur.fetchall()
    return {
        r[0]: {"pairing_id": r[0], "disruption_id": r[1], "crew_id": r[2],
               "committed_by": r[3], "committed_at": r[4].isoformat(),
               "accepted_rank": r[5], "override_reason": r[6]}
        for r in rows
    }


# --------------------------------------------------------------------------
# Commit
# --------------------------------------------------------------------------


class CommitError(RuntimeError):
    pass


def _json_default(o: Any) -> Any:
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(f"not JSON serialisable: {o!r}")


def _write_commitment(cur: Any, disruption_id: str, pairing_id: str, crew_id: str,
                       role: str, committed_by: str, accepted_rank: int | None,
                       presented_options: list[dict[str, Any]],
                       override_reason: str | None = None) -> None:
    """One assignment's writes, using a cursor the caller already opened a
    transaction on. Shared by `commit_decision` (one assignment, one
    transaction) and `commit_joint` (several assignments, one transaction),
    so the two can never drift into writing different things for the same
    kind of commitment.

    `override_reason` is only ever set when the controller picked something
    other than the Balanced recommendation (see tools.py's `commit_decision`).
    It goes into `controller_decisions.override_reason`, a column this
    schema already had before this console existed, so this reuses it
    instead of inventing a parallel place to say the same thing."""
    cur.execute(
        """INSERT INTO pairing_crew (pairing_id, crew_id, role)
           VALUES (%s, %s, %s)
           ON CONFLICT (pairing_id, crew_id) DO NOTHING""",
        (pairing_id, crew_id, role),
    )
    cur.execute(
        """INSERT INTO controller_decisions
               (request_id, decided_at_utc, decision_type, accepted_rank,
                presented_options, override_reason)
           VALUES (%s, now(), %s, %s, %s, %s)""",
        (disruption_id, "accepted_option", accepted_rank,
         json.dumps(presented_options, default=_json_default), override_reason),
    )
    # Upsert, not a bare UPDATE. If this disruption was never independently
    # viewed (so `open_disruption()` never registered it — e.g. it was only
    # ever seen as one leg of a joint plan, keyed internally by pairing_id;
    # see `core_engine.port.JsonToolPort.joint_plan`), an UPDATE alone would
    # silently match zero rows here, and a *later* view of it would then
    # INSERT a fresh row defaulting to 'open', permanently forgetting the
    # resolution. This guarantees the row exists as 'resolved' the moment
    # the commit happens, so that later view instead hits the ON CONFLICT
    # branch of `open_disruption()`'s own upsert and leaves status alone.
    cur.execute(
        """INSERT INTO open_disruptions (disruption_id, event_type, status)
           VALUES (%s, 'unknown', 'resolved')
           ON CONFLICT (disruption_id) DO UPDATE SET status = 'resolved'""",
        (disruption_id,),
    )
    cur.execute(
        """INSERT INTO disruption_commitments
               (pairing_id, disruption_id, crew_id, committed_by)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (pairing_id) DO UPDATE SET
               disruption_id = EXCLUDED.disruption_id, crew_id = EXCLUDED.crew_id,
               committed_by = EXCLUDED.committed_by, committed_at = now()""",
        (pairing_id, disruption_id, crew_id, committed_by),
    )


def commit_decision(disruption_id: str, pairing_id: str, crew_id: str, role: str,
                     committed_by: str, accepted_rank: int | None,
                     presented_options: list[dict[str, Any]],
                     override_reason: str | None = None) -> None:
    """Write a real roster assignment (`pairing_crew`) and a real audit row
    (`controller_decisions`, the schema's own existing table for this), then
    close the disruption. The caller (`tools.py::commit_decision`) has
    already re-checked legality against the live state right before calling
    this — this function doesn't re-check, it just commits."""
    if not enabled():
        raise CommitError("LEDGER_DATABASE_URL is not set; nothing to commit to.")

    with _conn() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                _write_commitment(cur, disruption_id, pairing_id, crew_id, role,
                                   committed_by, accepted_rank, presented_options,
                                   override_reason)


def commit_joint(assignments: list[dict[str, Any]], committed_by: str) -> None:
    """Every assignment in a joint plan, in one transaction: all of them
    land or none do. "One approval, one transaction" is the whole point of
    coordinating a joint plan instead of letting each pairing commit
    independently. A plan where two of three legs succeeded and the third
    lost a race to another desk isn't a plan, it's a new problem. Legality
    for every assignment must already be re-checked by the caller before
    this is called (see `core_engine.port.JsonToolPort.commit_joint_decisions`).
    This function only writes."""
    if not enabled():
        raise CommitError("LEDGER_DATABASE_URL is not set; nothing to commit to.")

    with _conn() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                for a in assignments:
                    _write_commitment(
                        cur, a["disruption_id"], a["pairing_id"], a["crew_id"], a["role"],
                        committed_by, a.get("accepted_rank"), a.get("presented_options", []),
                        a.get("override_reason"),
                    )
