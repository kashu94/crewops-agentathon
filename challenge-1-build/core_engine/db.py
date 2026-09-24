"""Postgres-backed reads of the core reference dataset -- the real tables
this project's original system (dCortex Crew Ops Advisor) always kept this
data in, in the same instance `core_engine/ledger.py` and the vector-search
modules already use via `LEDGER_DATABASE_URL`.

Every function here returns exactly the shape the equivalent `data/*.json`
file used to parse into -- same keys, same string-formatted dates/times,
same nesting. That's deliberate: `world.py`'s `load_world()` and
`port.py`'s `_load()` do real transform/join work on those shapes (building
`DutyDay`s, joining rosters against flights, rolling up duty windows), and
none of that logic needed to change -- only where the raw rows come from.
A date or timestamp column is formatted back to the exact JSON string form
("2026-09-14", "2026-09-14T02:30:00Z") rather than left as a native Python
`date`/`datetime`, because plenty of code downstream compares these against
string values extracted from a controller's question (`entities.py`) --
returning native objects would make every one of those comparisons silently
fail instead of raising, the opposite of this system's own "fail loud"
design.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

_DATABASE_URL = os.getenv("LEDGER_DATABASE_URL")


def enabled() -> bool:
    return bool(_DATABASE_URL)


@contextmanager
def _conn() -> Iterator[Any]:
    import psycopg
    with psycopg.connect(_DATABASE_URL, autocommit=True) as conn:
        yield conn


def _iso_date(d: Any) -> str:
    return d.isoformat()


def _iso_ts(dt: Any) -> str:
    """A TIMESTAMPTZ read back as the same "…T…Z" string the JSON files
    used. The column is stored in UTC (Neon's default), so formatting the
    wall-clock value directly and appending "Z" reproduces it exactly --
    no timezone conversion needed, just dropping the offset notation."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _hhmm(t: Any) -> str:
    return t.strftime("%H:%M")


def fetch_crew() -> list[dict[str, Any]]:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT crew_id, name, rank, base, ratings, seniority, "
            "reachability_minutes, status FROM crew"
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_flights() -> list[dict[str, Any]]:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT flight_id, flight_no, date, dep_station, arr_station, "
            "dep_utc, arr_utc, block_hours, aircraft, aircraft_type, seats FROM flights"
        )
        rows = cur.fetchall()
    return [
        {
            "flight_id": r[0], "flight_no": r[1], "date": _iso_date(r[2]),
            "dep_station": r[3], "arr_station": r[4],
            "dep_utc": _iso_ts(r[5]), "arr_utc": _iso_ts(r[6]),
            "block_hours": float(r[7]), "aircraft": r[8],
            "aircraft_type": r[9], "seats": r[10],
        }
        for r in rows
    ]


def fetch_rosters() -> dict[str, Any]:
    """Rebuilds rosters.json's nested shape:
    {"pairings": [{"pairing_id", "aircraft", "days": [...], "crew": [...]}, ...],
     "flagged_exceptions": [...], "note": str}
    from the normalized pairings/pairing_days/pairing_day_flights/pairing_crew
    tables (+ roster_exceptions for flagged_exceptions).
    """
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT pairing_id, aircraft FROM pairings ORDER BY pairing_id")
        pairings = {pid: {"pairing_id": pid, "aircraft": ac, "days": [], "crew": []}
                    for pid, ac in cur.fetchall()}

        cur.execute(
            "SELECT pairing_id, date, report_utc, release_utc FROM pairing_days "
            "ORDER BY pairing_id, date"
        )
        days_by_key: dict[tuple[str, Any], dict[str, Any]] = {}
        for pid, d, report_utc, release_utc in cur.fetchall():
            day = {"date": _iso_date(d), "flights": [],
                   "report_utc": _iso_ts(report_utc), "release_utc": _iso_ts(release_utc)}
            days_by_key[(pid, d)] = day
            if pid in pairings:
                pairings[pid]["days"].append(day)

        cur.execute(
            "SELECT pairing_id, date, flight_id FROM pairing_day_flights "
            "ORDER BY pairing_id, date, leg_order"
        )
        for pid, d, flight_id in cur.fetchall():
            day = days_by_key.get((pid, d))
            if day is not None:
                day["flights"].append(flight_id)

        cur.execute("SELECT pairing_id, crew_id, role FROM pairing_crew ORDER BY pairing_id")
        for pid, crew_id, role in cur.fetchall():
            if pid in pairings:
                pairings[pid]["crew"].append({"crew_id": crew_id, "role": role})

        cur.execute("SELECT crew_id, date, rule, note FROM roster_exceptions")
        flagged = [{"crew_id": cid, "date": _iso_date(d), "rule": rule, "note": note}
                   for cid, d, rule, note in cur.fetchall()]

    return {
        "pairings": list(pairings.values()),
        "flagged_exceptions": flagged,
        "note": "Every assignment is legal under rules.json except the flagged exceptions listed here.",
    }


def fetch_duty_clocks() -> list[dict[str, Any]]:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT crew_id, as_of_utc, last_rest_ended FROM duty_clocks")
        clocks = {cid: {"crew_id": cid, "as_of_utc": _iso_ts(as_of),
                        "last_rest_ended": _iso_ts(rest) if rest else None, "daily_history": []}
                  for cid, as_of, rest in cur.fetchall()}
        cur.execute(
            "SELECT crew_id, date, duty_hours, flight_hours FROM duty_daily_history "
            "ORDER BY crew_id, date"
        )
        for cid, d, duty_hours, flight_hours in cur.fetchall():
            if cid in clocks:
                clocks[cid]["daily_history"].append({
                    "date": _iso_date(d), "duty_hours": float(duty_hours),
                    "flight_hours": float(flight_hours),
                })
    return list(clocks.values())


def fetch_certifications() -> list[dict[str, Any]]:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT crew_id, cert_type, valid_from, valid_to FROM certifications")
        rows = cur.fetchall()
    return [
        {"crew_id": r[0], "cert_type": r[1], "valid_from": _iso_date(r[2]), "valid_to": _iso_date(r[3])}
        for r in rows
    ]


def fetch_reserve_pool() -> list[dict[str, Any]]:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT crew_id, base, dates, oncall_start_utc, oncall_end_utc FROM reserve_pool"
        )
        rows = cur.fetchall()
    return [
        {
            "crew_id": r[0], "base": r[1],
            "dates": [_iso_date(d) for d in r[2]],
            "oncall_window_utc": {"start": _hhmm(r[3]), "end": _hhmm(r[4])},
        }
        for r in rows
    ]


def fetch_risk_signals() -> list[dict[str, Any]]:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT crew_id, as_of_utc, disruption_risk_score, drivers FROM risk_signals")
        rows = cur.fetchall()
    return [
        {"crew_id": r[0], "as_of_utc": _iso_ts(r[1]), "disruption_risk_score": float(r[2]),
         "drivers": list(r[3])}
        for r in rows
    ]


def fetch_costs() -> dict[str, Any]:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT currency, reserve_callout_pilot, reserve_callout_cabin, "
            "dayoff_callout_pilot, dayoff_callout_cabin, deadhead_positioning, "
            "delay_cost_per_duty_hour, cancellation_per_flight, hotel_overnight FROM costs"
        )
        r = cur.fetchone()
    return {
        "currency": r[0], "reserve_callout_pilot": r[1], "reserve_callout_cabin": r[2],
        "dayoff_callout_pilot": r[3], "dayoff_callout_cabin": r[4],
        "deadhead_positioning": r[5], "delay_cost_per_duty_hour": r[6],
        "cancellation_per_flight": r[7], "hotel_overnight": r[8],
    }


def fetch_rules() -> dict[str, Any]:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT time_convention, definitions FROM rules_meta WHERE id = 1")
        time_convention, definitions = cur.fetchone()
        cur.execute("SELECT rule_id, text, params FROM legality_rules ORDER BY rule_id")
        rules = [
            {"rule_id": rid, "text": text, **({"params": params} if params is not None else {})}
            for rid, text, params in cur.fetchall()
        ]
    return {"time_convention": time_convention, "definitions": definitions, "rules": rules}


def fetch_boarding_gates() -> list[dict[str, Any]]:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT boarding_gate_number, pairing_id, flight_id, date, "
            "boarding_start_time, boarding_end_time, aircraft_type FROM boarding_gates"
        )
        rows = cur.fetchall()
    return [
        {
            "boarding_gate_number": r[0], "pairing_id": r[1], "flight_id": r[2],
            "date": _iso_date(r[3]), "boarding_start_time": _iso_ts(r[4]),
            "boarding_end_time": _iso_ts(r[5]), "aircraft_type": r[6],
        }
        for r in rows
    ]
