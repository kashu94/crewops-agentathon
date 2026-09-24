"""The seven rules.

Each returns a `RuleVerdict` carrying the numbers, never a bare boolean.
A controller acting on "illegal" needs to know *by how much*, because
"exceeds by 1h20m" is actionable and "illegal" is not.

Rules are evaluated against a `CrewSnapshot`, which is whatever the backend
already knows about one crew member. Keeping the rules pure functions of a
snapshot means they're testable without a database, and behave the same
whichever port supplies the data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from schemas import RuleVerdict, Verdict
from core_engine.duty import (
    MAX_DUTY_HOURS_7D,
    MAX_FLIGHT_HOURS_28D,
    MIN_REST_HOURS,
    DUTY_WINDOW_DAYS,
    FLIGHT_WINDOW_DAYS,
    DutyDay,
    calendar_window,
    format_hours,
    hours_between,
)


@dataclass(slots=True)
class CrewSnapshot:
    """Everything the rules need about one crew member."""

    crew_id: str
    rank: str
    base: str
    ratings: tuple[str, ...]
    status: str
    reachability_minutes: int
    last_rest_ended: datetime | None
    daily_duty: dict[date, float] = field(default_factory=dict)
    daily_flight: dict[date, float] = field(default_factory=dict)
    certs: tuple[tuple[str, date, date], ...] = ()   # (type, valid_from, valid_to)
    assigned: tuple[tuple[str, datetime, datetime], ...] = ()  # (pairing, report, release)
    name: str = ""
    """Who they are. No rule reads it — but a controller phoning someone at
    05:00 does, and "Assign D. Reddy (C-3310)" is what they need to hear."""
    seniority: int | None = None
    """Years of service. Reported, never used to sort: see `find_options`."""

    def duty_in_window(self, end: date, days: int = DUTY_WINDOW_DAYS) -> float:
        start, stop = calendar_window(end, days)
        return round(sum(
            h for d, h in self.daily_duty.items() if start <= d <= stop
        ), 2)

    def flight_in_window(self, end: date, days: int = FLIGHT_WINDOW_DAYS) -> float:
        start, stop = calendar_window(end, days)
        return round(sum(
            h for d, h in self.daily_flight.items() if start <= d <= stop
        ), 2)


def _pass(rule_id: str, detail: str = "", **kw) -> RuleVerdict:
    return RuleVerdict(rule_id=rule_id, status=Verdict.PASS, detail=detail, **kw)


def _fail(rule_id: str, detail: str, **kw) -> RuleVerdict:
    return RuleVerdict(rule_id=rule_id, status=Verdict.FAIL, detail=detail, **kw)


# --------------------------------------------------------------------------
# RULE-FDP-01
# --------------------------------------------------------------------------


def check_fdp(day: DutyDay) -> RuleVerdict:
    """Max flight duty period, reduced half an hour per sector beyond the 2nd."""
    used, limit = day.fdp_hours, day.fdp_limit
    if used > limit:
        return _fail(
            "RULE-FDP-01",
            f"FDP {used}h > {limit}h limit ({day.n_sectors} sectors) on {day.date}",
            used=used, limit=limit, headroom=round(limit - used, 2),
            date=day.date.isoformat(),
        )
    return _pass(
        "RULE-FDP-01",
        f"{used}h of {limit}h ({day.n_sectors} sectors)",
        used=used, limit=limit, headroom=round(limit - used, 2),
        date=day.date.isoformat(),
    )


# --------------------------------------------------------------------------
# RULE-DUTY-02 / RULE-FLT-03 — calendar-day windows
# --------------------------------------------------------------------------


def check_duty_window(
    crew: CrewSnapshot, day: DutyDay, prior: list[DutyDay] | None = None
) -> RuleVerdict:
    """60 duty hours in any 7 consecutive calendar days, inclusive.

    The prospective duty counts, and so do the *earlier days of the same
    pairing*. Assigning someone a two-day trip adds both days to the window,
    so day 2 must be checked against a window that already contains day 1.

    Miss that, and a cover can look legal on day 1 and legal again on day 2,
    even though together they breach.
    """
    start, stop = calendar_window(day.date, DUTY_WINDOW_DAYS)
    already = crew.duty_in_window(day.date)
    added = sum(d.fdp_hours for d in (prior or []) if start <= d.date <= stop)
    total = round(already + added + day.fdp_hours, 2)

    if total > MAX_DUTY_HOURS_7D:
        over = round(total - MAX_DUTY_HOURS_7D, 2)
        return _fail(
            "RULE-DUTY-02",
            f"would exceed 60h/7d by {format_hours(over)} on {day.date} (total {total}h)",
            used=total, limit=MAX_DUTY_HOURS_7D, headroom=-over,
            date=day.date.isoformat(),
        )
    return _pass(
        "RULE-DUTY-02", f"{total}h of {MAX_DUTY_HOURS_7D}h over 7 calendar days",
        used=total, limit=MAX_DUTY_HOURS_7D,
        headroom=round(MAX_DUTY_HOURS_7D - total, 2), date=day.date.isoformat(),
    )


def check_flight_window(
    crew: CrewSnapshot, day: DutyDay, prior: list[DutyDay] | None = None
) -> RuleVerdict:
    """100 block hours in any 28 consecutive calendar days, inclusive.

    Earlier days of the same pairing count, for the same reason as DUTY-02.
    """
    start, stop = calendar_window(day.date, FLIGHT_WINDOW_DAYS)
    already = crew.flight_in_window(day.date)
    added = sum(d.block_hours for d in (prior or []) if start <= d.date <= stop)
    total = round(already + added + day.block_hours, 2)

    if total > MAX_FLIGHT_HOURS_28D:
        over = round(total - MAX_FLIGHT_HOURS_28D, 2)
        return _fail(
            "RULE-FLT-03",
            f"would exceed 100h/28d by {format_hours(over)} on {day.date} (total {total}h)",
            used=total, limit=MAX_FLIGHT_HOURS_28D, headroom=-over,
            date=day.date.isoformat(),
        )
    return _pass(
        "RULE-FLT-03", f"{total}h of {MAX_FLIGHT_HOURS_28D}h over 28 calendar days",
        used=total, limit=MAX_FLIGHT_HOURS_28D,
        headroom=round(MAX_FLIGHT_HOURS_28D - total, 2), date=day.date.isoformat(),
    )


# --------------------------------------------------------------------------
# RULE-REST-04
# --------------------------------------------------------------------------


def check_rest(crew: CrewSnapshot, day: DutyDay, exclude_pairing: str | None = None) -> RuleVerdict:
    """12h rest between release and next report — checked on both sides.

    Rest before the new duty is the obvious half. The half that gets missed
    is rest before whatever the candidate is *already* rostered for next:
    cover them tonight and you can strand tomorrow's departure instead.
    """
    # `last_rest_ended` marks when their rest *finished* — the earliest
    # they're legal again — not when their last duty released. Subtracting
    # 12h from it would double-count rest they've already taken and reject
    # crew who are demonstrably available.
    if crew.last_rest_ended is not None and day.report_utc < crew.last_rest_ended:
        short = hours_between(day.report_utc, crew.last_rest_ended)
        return _fail(
            "RULE-REST-04",
            f"still resting until {crew.last_rest_ended:%Y-%m-%d %H:%M}Z, "
            f"{format_hours(short)} before report on {day.date}",
            used=short, limit=MIN_REST_HOURS, date=day.date.isoformat(),
        )

    for pairing_id, report, release in crew.assigned:
        if pairing_id == exclude_pairing:
            continue
        # Downstream: the new duty ends, and their own next duty starts too soon.
        if report >= day.release_utc:
            rest = hours_between(day.release_utc, report)
            if rest < MIN_REST_HOURS:
                return _fail(
                    "RULE-REST-04",
                    f"only {rest}h rest before {pairing_id} (downstream conflict)",
                    used=rest, limit=MIN_REST_HOURS,
                    headroom=round(rest - MIN_REST_HOURS, 2),
                )
        # Overlap: they cannot be in two places at once.
        elif release > day.report_utc:
            return _fail(
                "RULE-REST-04",
                f"double-booked: {pairing_id} overlaps this duty on {day.date}",
                date=day.date.isoformat(),
            )

    return _pass("RULE-REST-04", f"at least {MIN_REST_HOURS}h either side")


# --------------------------------------------------------------------------
# RULE-QUAL-05 / RULE-CERT-06 / RULE-BASE-07
# --------------------------------------------------------------------------


def check_qualification(crew: CrewSnapshot, day: DutyDay) -> RuleVerdict:
    """Valid rating for the aircraft type, and actually available to fly."""
    if day.aircraft_type not in crew.ratings:
        return _fail("RULE-QUAL-05", f"no {day.aircraft_type} rating")
    if crew.status != "active":
        return _fail("RULE-QUAL-05", f"status is {crew.status}, not active")
    return _pass("RULE-QUAL-05", f"{day.aircraft_type} rated, active")


def check_certifications(crew: CrewSnapshot, day: DutyDay) -> RuleVerdict:
    """Every certification still valid on every duty date of the pairing.

    **Expiry only — `valid_from` is deliberately not checked.** In this
    dataset, all 150 licence records carry a `valid_from` years in the
    future, and some are incoherent (a start date after their own end date).
    Enforcing it would exclude every pilot in the airline.

    `valid_to` is sound: no certification has already expired at the
    snapshot, and the one engineered case (a recurrent-training cert expiring
    the day before a scheduled duty) is an expiry. The answer keys only ever
    cite CERT-06 for expiry, so expiry is the rule.
    """
    for cert_type, _valid_from, valid_to in crew.certs:
        if day.date > valid_to:
            return _fail(
                "RULE-CERT-06",
                f"{cert_type} expired {valid_to}, invalid on {day.date}",
                date=day.date.isoformat(),
            )
    return _pass("RULE-CERT-06", f"all {len(crew.certs)} certifications valid")


def check_base(crew: CrewSnapshot, day: DutyDay, deadhead: bool = False) -> RuleVerdict:
    """Own-base callout, or another base with deadhead positioning."""
    if crew.base == day.dep_station:
        return _pass("RULE-BASE-07", f"own base ({crew.base})")
    if deadhead:
        return _pass(
            "RULE-BASE-07",
            f"positioning from {crew.base} to {day.dep_station}",
        )
    return _fail(
        "RULE-BASE-07",
        f"based at {crew.base}, duty starts {day.dep_station} — needs positioning",
    )


# --------------------------------------------------------------------------
# All seven
# --------------------------------------------------------------------------

ALL_RULES = (
    "RULE-FDP-01", "RULE-DUTY-02", "RULE-FLT-03", "RULE-REST-04",
    "RULE-QUAL-05", "RULE-CERT-06", "RULE-BASE-07",
)


def evaluate(
    crew: CrewSnapshot,
    days: list[DutyDay],
    exclude_pairing: str | None = None,
    deadhead: bool = False,
) -> list[RuleVerdict]:
    """Every rule against every day of the pairing.

    Multi-day matters: a cover can be legal on day 1 and breach on day 2, so
    checking only the leg in front of you isn't enough.
    """
    verdicts: list[RuleVerdict] = []
    for i, day in enumerate(days):
        verdicts += [
            check_qualification(crew, day),
            check_certifications(crew, day),
            check_fdp(day),
            check_duty_window(crew, day, days[:i]),
            check_flight_window(crew, day, days[:i]),
            check_rest(crew, day, exclude_pairing),
        ]
        # Base applies to the first day only — that's where the crew has to
        # physically get to. Later days start wherever the pairing overnighted,
        # so checking them would reject every crew member on any pairing
        # that sleeps away from base.
        if i == 0:
            verdicts.append(check_base(crew, day, deadhead))
    return verdicts


def is_legal(verdicts: list[RuleVerdict]) -> bool:
    return not any(v.failed for v in verdicts)


def blocking(verdicts: list[RuleVerdict]) -> list[RuleVerdict]:
    """Only the failures, deduplicated by rule and detail."""
    seen: set[tuple[str, str]] = set()
    out = []
    for v in verdicts:
        if v.failed and (v.rule_id, v.detail) not in seen:
            seen.add((v.rule_id, v.detail))
            out.append(v)
    return out
