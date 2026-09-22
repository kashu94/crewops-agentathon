"""The reasoning core — candidate search, cost ranking and cascade.

Loads the whole operation from the vendored JSON dataset once, then answers
from memory. The dataset is under 700 KB, so a per-question round trip buys
nothing; `JsonToolPort` (in `core_engine/port.py`) holds one immutable `World`
and forks it for what-ifs.

This is a JSON-backed port of dCortex Crew Ops Advisor's `core/engine.py`,
which loaded the same shapes out of Postgres. The rules, the cost model and
the candidate search are unchanged — only `load_world` differs.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from schemas import RuleVerdict
from tools import ToolError
from core_engine import rules
from core_engine.duty import DutyDay, hours_between
from core_engine.rules import ALL_RULES, CrewSnapshot

PILOT_ROLES = ("Captain", "First Officer")

# RULE-BASE-07: the DEL->BLR positioning flights, from the dataset README.
# New report is arrival + 15 min.
POSITIONING = {
    ("DEL", "BLR"): (
        ("DX402", time(8, 45), "odd"),
        ("DX589", time(7, 45), "even"),
    )
}
POSITIONING_REPORT_BUFFER = timedelta(minutes=15)


@dataclass(slots=True)
class Costs:
    reserve_pilot: int
    reserve_cabin: int
    dayoff_pilot: int
    dayoff_cabin: int
    deadhead: int
    delay_per_hour: int
    cancellation_per_flight: int

    def callout(self, role: str, on_reserve: bool) -> int:
        pilot = role in PILOT_ROLES
        if on_reserve:
            return self.reserve_pilot if pilot else self.reserve_cabin
        return self.dayoff_pilot if pilot else self.dayoff_cabin


@dataclass
class World:
    """The whole operation, in memory."""

    crew: dict[str, CrewSnapshot]
    pairing_days: dict[str, list[DutyDay]]
    pairing_crew: dict[str, list[tuple[str, str]]]      # pairing -> [(crew, role)]
    pairing_flights: dict[str, list[dict[str, Any]]]
    reserves: dict[str, list[tuple[date, time, time]]]  # crew -> [(date, start, end)]
    costs: Costs

    def duty_days(self, pairing_id: str) -> list[DutyDay]:
        if pairing_id not in self.pairing_days:
            raise ToolError("UNRESOLVED_ENTITY", f"no pairing {pairing_id!r}")
        return self.pairing_days[pairing_id]

    def on_reserve(self, crew_id: str, when: date, report: datetime) -> bool:
        """A reserve is usable only if the required report falls in the window.

        Not the disruption time and not the departure — the report time,
        after any positioning. Getting this wrong makes unavailable reserves
        look available, which is the expensive direction to be wrong in.
        """
        for day, start, end in self.reserves.get(crew_id, ()):
            if day == when and start <= report.time().replace(tzinfo=None) <= end:
                return True
        return False

    def reserve_pool_size(self, rank: str, base: str, when: date) -> int:
        """How many crew of this rank at this base are on call on `when`,
        before any of them are used -- the denominator for `resilience`."""
        return sum(
            1 for crew_id, windows in self.reserves.items()
            if any(d == when for d, _, _ in windows)
            and (c := self.crew.get(crew_id)) is not None
            and c.rank == rank and c.base == base
        )


# --------------------------------------------------------------------------
# Loading — from the vendored dataset JSON, not Postgres
# --------------------------------------------------------------------------


def _parse_dt(value: str) -> datetime:
    """A dataset timestamp ("2026-09-14T02:30:00Z") as a naive UTC datetime."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_time(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def _read_json(data_dir: Path, name: str) -> Any:
    return json.loads((data_dir / f"{name}.json").read_text(encoding="utf-8"))


def load_world(data_dir: Path) -> World:
    """Build the in-memory `World` straight from `data/*.json`."""
    crew_rows = _read_json(data_dir, "crew")
    flight_rows = _read_json(data_dir, "flights")
    roster = _read_json(data_dir, "rosters")
    duty_clock_rows = _read_json(data_dir, "duty_clocks")
    cert_rows = _read_json(data_dir, "certifications")
    reserve_rows = _read_json(data_dir, "reserve_pool")
    costs_row = _read_json(data_dir, "costs")

    daily_duty: dict[str, dict[date, float]] = defaultdict(dict)
    daily_flight: dict[str, dict[date, float]] = defaultdict(dict)
    last_rest_ended: dict[str, datetime] = {}
    for r in duty_clock_rows:
        cid = r["crew_id"]
        last_rest_ended[cid] = _parse_dt(r["last_rest_ended"])
        for day in r.get("daily_history", []):
            d = _parse_date(day["date"])
            daily_duty[cid][d] = float(day["duty_hours"])
            daily_flight[cid][d] = float(day["flight_hours"])

    certs: dict[str, list[tuple[str, date, date]]] = defaultdict(list)
    for r in cert_rows:
        certs[r["crew_id"]].append((
            r["cert_type"], _parse_date(r["valid_from"]), _parse_date(r["valid_to"]),
        ))

    flights = {f["flight_id"]: f for f in flight_rows}

    pairing_days: dict[str, list[DutyDay]] = defaultdict(list)
    pairing_flights: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pairing_crew: dict[str, list[tuple[str, str]]] = defaultdict(list)
    assigned: dict[str, list[tuple[str, datetime, datetime]]] = defaultdict(list)

    for pairing in roster["pairings"]:
        pid = pairing["pairing_id"]
        for day in pairing.get("days", []):
            legs = [flights[fid] for fid in day.get("flights", []) if fid in flights]
            if not legs:
                continue
            duty_day = DutyDay(
                date=_parse_date(day["date"]),
                report_utc=_parse_dt(day["report_utc"]),
                release_utc=_parse_dt(day["release_utc"]),
                n_sectors=len(legs),
                block_hours=round(sum(float(f["block_hours"]) for f in legs), 2),
                aircraft_type=legs[0]["aircraft_type"],
                dep_station=legs[0]["dep_station"],
            )
            pairing_days[pid].append(duty_day)
            pairing_flights[pid].extend(legs)

        for member in pairing.get("crew", []):
            cid, role = member["crew_id"], member["role"]
            pairing_crew[pid].append((cid, role))
            for duty_day in pairing_days.get(pid, []):
                assigned[cid].append((pid, duty_day.report_utc, duty_day.release_utc))

    crew: dict[str, CrewSnapshot] = {}
    for r in crew_rows:
        cid = r["crew_id"]
        crew[cid] = CrewSnapshot(
            crew_id=cid, rank=r["rank"], base=r["base"],
            name=r.get("name") or "", seniority=r.get("seniority"),
            ratings=tuple(r.get("ratings") or ()), status=r["status"],
            reachability_minutes=r["reachability_minutes"],
            last_rest_ended=last_rest_ended.get(cid),
            daily_duty=daily_duty.get(cid, {}), daily_flight=daily_flight.get(cid, {}),
            certs=tuple(certs.get(cid, ())), assigned=tuple(assigned.get(cid, ())),
        )

    reserves: dict[str, list[tuple[date, time, time]]] = defaultdict(list)
    for r in reserve_rows:
        window = r["oncall_window_utc"]
        start, end = _parse_time(window["start"]), _parse_time(window["end"])
        for d in r.get("dates", []):
            reserves[r["crew_id"]].append((_parse_date(d), start, end))

    return World(
        crew=crew, pairing_days=dict(pairing_days), pairing_crew=dict(pairing_crew),
        pairing_flights=dict(pairing_flights), reserves=dict(reserves),
        costs=Costs(
            reserve_pilot=costs_row["reserve_callout_pilot"],
            reserve_cabin=costs_row["reserve_callout_cabin"],
            dayoff_pilot=costs_row["dayoff_callout_pilot"],
            dayoff_cabin=costs_row["dayoff_callout_cabin"],
            deadhead=costs_row["deadhead_positioning"],
            delay_per_hour=costs_row["delay_cost_per_duty_hour"],
            cancellation_per_flight=costs_row["cancellation_per_flight"],
        ),
    )


# --------------------------------------------------------------------------
# Candidate search
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Candidate:
    crew_id: str
    verdicts: list[RuleVerdict]
    on_reserve: bool
    deadhead: bool
    delay_hours: float
    cost_inr: int
    cost_breakdown: dict[str, int]

    @property
    def legal(self) -> bool:
        return rules.is_legal(self.verdicts)

    def action(self, rank_name: str, name: str = "") -> str:
        """The instruction a controller carries out.

        The id stays in it — the id is what goes into the roster system, and
        two people can share a surname. But a person is who they phone, so
        the name leads when we have one.
        """
        kind = "reserve callout" if self.on_reserve else "day-off callout"
        if self.deadhead:
            kind += (f" + deadhead (first departure delayed "
                     f"~{self.delay_hours}h)")
        who = f"{rank_name} {name} ({self.crew_id})" if name else f"{rank_name} {self.crew_id}"
        return f"Assign {who} ({kind})"

    @property
    def strategy(self) -> str:
        """Which of the four lines of defense this candidate belongs to.

        Deadhead is its own strategy regardless of whether the crew member
        was on reserve or a day off before positioning -- a controller
        thinking "who's already here" vs. "who do I bring in" cares about
        that distinction more than the reserve/day-off split underneath it.
        """
        if self.deadhead:
            return "deadhead_reposition"
        return "reserve_callout" if self.on_reserve else "day_off_callout"


STRATEGY_LABELS: dict[str, str] = {
    "reserve_callout": "Reserve callout",
    "day_off_callout": "Day-off callout",
    "deadhead_reposition": "Deadhead / reposition",
    "cancel": "Cancel",
}
"""The four strategies `find_options` ranks across, in the fixed order a
controller should read them: two ways to staff from people already at or near
base, one way to bring someone in from elsewhere, and the fallback none of
the other three should usually beat."""


def resilience_score(world: World, candidate: Candidate, role: str, base: str,
                      when: date) -> float:
    """0-100: how much of today's reserve buffer for this rank/base survives
    picking this candidate.

    A day-off callout never touches the on-call reserve pool, so it scores
    100 regardless of how thin that pool is. A reserve callout consumes one
    of `n` currently-on-call crew of this rank at this base, scoring
    100*(n-1)/n -- thinner pools cost more resilience per reserve used, which
    is the point: using the only reserve captain at a station should read as
    a bigger hit than using one of twelve.
    """
    if not candidate.on_reserve:
        return 100.0
    pool = world.reserve_pool_size(role, base, when)
    if pool <= 0:
        return 100.0
    return round(100.0 * (pool - 1) / pool, 1)


def positioning_delay(world: World, crew: CrewSnapshot, day: DutyDay) -> float | None:
    """Hours the departure must slip so a positioned crew can report.

    Returns None when no same-day positioning flight exists, which is the
    RULE-BASE-07 exclusion.
    """
    options = POSITIONING.get((crew.base, day.dep_station))
    if not options:
        return None

    best: float | None = None
    for _, arrival, parity in options:
        if (day.date.day % 2 == 1) != (parity == "odd"):
            continue
        arrives = datetime.combine(day.date, arrival)
        new_report = arrives + POSITIONING_REPORT_BUFFER
        slip = max(0.0, hours_between(day.report_utc, new_report))
        if best is None or slip < best:
            best = slip
    return best


def assess(world: World, crew_id: str, pairing_id: str,
           delay_hours: float = 0.0,
           extra_assigned: tuple[tuple[str, datetime, datetime], ...] = ()) -> Candidate:
    """Evaluate one crew member against one pairing.

    `extra_assigned` is for commitments made *after* the vendored dataset
    was generated -- a crew member committed live, via `commit_decision`, to
    a different pairing (see `core_engine/ledger.py`). It is merged into a
    *copy* of the snapshot for this call only, so a decision made in one
    disruption is honoured by `RULE-REST-04`'s existing overlap/double-
    booking check for every other pairing evaluated afterwards, without a
    second, parallel exclusivity check to keep in sync with the first.
    """
    crew = world.crew.get(crew_id)
    if crew is None:
        raise ToolError("UNRESOLVED_ENTITY", f"no crew {crew_id!r}")
    if extra_assigned:
        crew = replace(crew, assigned=crew.assigned + extra_assigned)

    days = world.duty_days(pairing_id)
    first = days[0]

    deadhead = crew.base != first.dep_station
    slip = delay_hours
    if deadhead:
        needed = positioning_delay(world, crew, first)
        if needed is None:
            verdicts = [rules.check_base(crew, first, deadhead=False)]
            return Candidate(crew_id, verdicts, False, True, 0.0, 0, {})
        slip = max(slip, needed)

    shifted = [d.delayed(slip) if slip else d for d in days]
    on_reserve = world.on_reserve(crew_id, first.date, shifted[0].report_utc)
    verdicts = rules.evaluate(crew, shifted, exclude_pairing=pairing_id, deadhead=deadhead)

    # Rostered on reserve that day, but the window does not cover the required
    # report: they are unavailable, not a day-off callout. Someone on reserve
    # duty is not on a day off, so falling back to day-off pricing invents an
    # option the desk does not actually have.
    rostered_reserve = any(d == first.date for d, _, _ in world.reserves.get(crew_id, ()))
    if rostered_reserve and not on_reserve:
        window = next(
            (f"{s:%H:%M}-{e:%H:%M}Z" for d, s, e in world.reserves[crew_id]
             if d == first.date), "")
        verdicts.append(rules._fail(
            "RULE-BASE-07",
            f"reserve on-call window {window} does not cover required report "
            f"{shifted[0].report_utc:%H:%M}Z",
            date=first.date.isoformat(),
        ))

    if deadhead and not on_reserve:
        # Positioning is only offered to reserves in this dataset.
        on_reserve = any(d == first.date for d, _, _ in world.reserves.get(crew_id, ()))

    breakdown = {"callout": world.costs.callout(crew.rank, on_reserve)}
    if deadhead:
        breakdown["positioning"] = world.costs.deadhead
    if slip:
        breakdown["delay"] = int(round(slip * world.costs.delay_per_hour))

    return Candidate(
        crew_id=crew_id, verdicts=verdicts, on_reserve=on_reserve,
        deadhead=deadhead, delay_hours=round(slip, 2),
        cost_inr=sum(breakdown.values()), cost_breakdown=breakdown,
    )


def drop_stage(verdicts: list[RuleVerdict]) -> tuple[str, str]:
    """Which funnel stage a candidate fell out at, and why."""
    for v in verdicts:
        if not v.failed:
            continue
        return {
            "RULE-QUAL-05": ("qualified", "no rating / not active"),
            "RULE-CERT-06": ("certified", "certification invalid on a duty date"),
            "RULE-BASE-07": ("in position", "no same-day positioning from base"),
            "RULE-REST-04": ("available", "rest conflict or double-booked"),
            "RULE-FDP-01": ("within limits", "flight duty period exceeded"),
            "RULE-DUTY-02": ("within limits", "duty-hour limit exceeded"),
            "RULE-FLT-03": ("within limits", "block-hour limit exceeded"),
        }.get(v.rule_id, ("other", v.detail))
    return ("legal", "")
