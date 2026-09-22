"""The callout notification.

Drafting the message to the crew member is the one job in this system where a
language model is unambiguously the right tool. Everything else it might do,
deterministic code does better: the rules are arithmetic, the ranking is a
sort, the legality check is a set of predicates. Prose is not.

So this module draws the trust-boundary line in the same place as everywhere
else, just with the halves the other way round. `assemble()` gathers the
facts — every report time, station and flight number read from the roster,
and an acknowledgement deadline computed from the crew member's own
reachability. `render()` turns them into a message a controller could send
unedited. The Explainer Agent may then rewrite that message, and the verifier
checks it afterwards: it may change the wording, it may not change a time.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from tools import ToolError


def _as_datetime(value: Any) -> dt.datetime:
    """Accept either an ISO string or a plain `datetime`."""
    if isinstance(value, dt.datetime):
        return value
    text = str(value).replace("Z", "+00:00").replace(" ", "T", 1)
    return dt.datetime.fromisoformat(text)


def _hhmmz(value: Any) -> str:
    return _as_datetime(value).strftime("%H:%MZ")


def _day_label(value: Any) -> str:
    """"15 Sep" — how a roster is read aloud."""
    moment = _as_datetime(value) if not isinstance(value, dt.date) else value
    return f"{moment.day} {moment.strftime('%b')}"


def assemble(
    crew: dict[str, Any],
    pairing_id: str,
    aircraft: str | None,
    days: list[dict[str, Any]],
    role: str | None = None,
) -> dict[str, Any]:
    """The facts a callout message needs, and nothing that had to be invented.

    `days` is normalised by the tool port: each entry carries the roster's own
    `report_utc` and `release_utc` plus the day's legs in order, each leg with
    a flight number and its two stations. Report station and overnight
    station are read off the legs rather than stored anywhere — a day reports
    where its first leg departs, and overnights where its last leg lands
    whenever another day follows.

    The acknowledgement deadline is `report − reachability_minutes`: the last
    moment a "no" is still actionable, given how long this particular crew
    member takes to reach the airport. It is a derived number, so it is
    returned as data from here rather than worked out in prose later.
    """
    if not days:
        raise ToolError("UNRESOLVED_ENTITY", f"{pairing_id} has no rostered days")

    out_days = []
    for index, day in enumerate(days):
        legs = day.get("flights") or []
        out_days.append({
            "date": str(day["date"]),
            "day_label": _day_label(day["date"]),
            "report_utc": _hhmmz(day["report_utc"]),
            "release_utc": _hhmmz(day["release_utc"]),
            "report_station": legs[0]["dep_station"] if legs else None,
            "flights": [leg["flight_no"] for leg in legs],
            "overnight_station": (
                legs[-1]["arr_station"] if legs and index < len(days) - 1 else None
            ),
        })

    reach = crew.get("reachability_minutes")
    report = _as_datetime(days[0]["report_utc"])
    acknowledge_by = report - dt.timedelta(minutes=int(reach)) if reach else None

    return {
        "crew_id": crew["crew_id"],
        "name": crew.get("name"),
        "rank": crew.get("rank"),
        "role": role or crew.get("rank"),
        "reachability_minutes": reach,
        "pairing_id": pairing_id,
        "aircraft": aircraft,
        "days": out_days,
        "report_utc": _hhmmz(days[0]["report_utc"]),
        "report_date": _day_label(days[0]["date"]),
        "acknowledge_by_utc": _hhmmz(acknowledge_by) if acknowledge_by else None,
    }


def render(brief: dict[str, Any]) -> str:
    """The message, from the brief. No model, no network, still sendable.

    Deliberately without a phone number or a named duty controller: the
    dataset has neither, and a plausible-looking contact number is precisely
    the kind of invention this system exists not to make. The controller
    sending it knows their own extension.
    """
    who = brief.get("name") or brief["crew_id"]
    first = brief["days"][0]
    place = first["report_station"] or "your base"

    lines = [
        f"To: {who} ({brief['crew_id']})",
        f"Subject: Callout — pairing {brief['pairing_id']}, report "
        f"{brief['report_date']} {brief['report_utc']}",
        "",
        f"You are called out to operate pairing {brief['pairing_id']}"
        + (f" on {brief['aircraft']}" if brief.get("aircraft") else "")
        + f" as {brief['role']}.",
        "",
        f"Report {brief['report_date']} at {brief['report_utc']}, "
        f"{place} crew room.",
        "",
    ]

    for index, day in enumerate(brief["days"], start=1):
        flights = "/".join(day["flights"]) or "no legs rostered"
        line = (f"  Day {index} ({day['day_label']}) — report {day['report_utc']} "
                f"at {day['report_station']}: {flights}, release {day['release_utc']}")
        if day["overnight_station"]:
            line += f"; overnight {day['overnight_station']} (hotel arranged)"
        lines.append(line)

    lines.append("")
    if brief.get("acknowledge_by_utc"):
        lines.append(
            f"Please acknowledge by {brief['acknowledge_by_utc']} — that is "
            f"{brief['reachability_minutes']} minutes before report, your stated "
            f"time to reach the airport. If you cannot accept, say so now so "
            f"cover can be arranged."
        )
    else:
        lines.append("Please acknowledge as soon as you receive this. If you "
                     "cannot accept, say so now so cover can be arranged.")
    lines.append("")
    lines.append("Any questions, come back to Crew Control on this thread.")
    return "\n".join(lines)
