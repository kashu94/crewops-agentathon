"""Boarding-gate lookups, backed by the vendored `boarding_gates.json`.

Layered on top of the real flight schedule the same way `core_engine/world.py`
layers rules over dataset facts: loaded once, held in memory, no per-question
round trip. This data is itself fabricated (only the gate NUMBER is invented;
everything else — the occupancy window, aircraft type, which flight — is
derived from the real schedule). It's the one bonus tool in the lab (Tool
10 of 10): `check_gate`.

The window is departure-side only: it ends at a flight's own `dep_utc` and
represents the gate held before *that* departure. There's no separate
arrival/deplaning record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


def parse(value: Any) -> datetime:
    """A boarding-gate timestamp as a naive UTC datetime."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")


def iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(slots=True)
class GateWorld:
    by_flight: dict[str, dict[str, Any]]
    by_gate: dict[str, list[dict[str, Any]]]  # each list sorted by boarding_start_time


def load_gates(rows: list[dict[str, Any]]) -> GateWorld:
    """Build a `GateWorld` from the rows loaded out of `boarding_gates.json`."""
    by_flight = {r["flight_id"]: r for r in rows}

    by_gate: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_gate.setdefault(r["boarding_gate_number"], []).append(r)
    for recs in by_gate.values():
        recs.sort(key=lambda r: parse(r["boarding_start_time"]))

    return GateWorld(by_flight=by_flight, by_gate=by_gate)


def occupant_at(world: GateWorld, gate: str, instant: datetime) -> dict[str, Any] | None:
    """Which flight (if any) holds `gate` at `instant`. None means it is free."""
    return next(
        (r for r in world.by_gate.get(gate, ())
         if parse(r["boarding_start_time"]) <= instant <= parse(r["boarding_end_time"])),
        None,
    )


def next_at_gate(world: GateWorld, gate: str, flight_id: str) -> dict[str, Any] | None:
    """The record chronologically after `flight_id`'s own window at `gate`.

    None when this is the last flight scheduled into that gate — there's
    nothing to collide with, no matter how long a delay runs.
    """
    siblings = world.by_gate.get(gate, ())
    idx = next((i for i, r in enumerate(siblings) if r["flight_id"] == flight_id), None)
    if idx is None or idx + 1 >= len(siblings):
        return None
    return siblings[idx + 1]
