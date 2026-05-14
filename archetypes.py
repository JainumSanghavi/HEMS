"""Archetypal 24-hour base load curves, in kW, scaled by occupancy.

The simulator multiplies hour-of-day values by an occupancy factor and
adds small day-to-day jitter to produce the simulation_hours-long array
each house needs. Curves capture the *non-shiftable* load only — fridge,
lights, HVAC, electronics — not EV / laundry / water heater.
"""
from __future__ import annotations

import random
from typing import Sequence

# Index = hour of day (0..23). Values are kW for a "1.0 occupancy factor"
# household. Multiply by occupancy_factor to scale.
ARCHETYPE_BASE_24H: dict[str, Sequence[float]] = {
    # Two retirees: low overall, morning rise, modest evening peak.
    "retired_couple": (
        0.35, 0.35, 0.35, 0.35, 0.35, 0.40,   # 00..05
        0.55, 0.85, 0.95, 0.80, 0.70, 0.70,   # 06..11
        0.75, 0.75, 0.70, 0.80, 1.10, 1.55,   # 12..17
        1.70, 1.55, 1.25, 0.90, 0.60, 0.40,   # 18..23
    ),
    # Family with kids: kids home after school, big evening peak.
    "young_family": (
        0.40, 0.40, 0.40, 0.40, 0.40, 0.50,
        0.85, 1.45, 1.20, 0.80, 0.70, 0.75,
        0.85, 0.85, 0.90, 1.10, 1.65, 2.25,
        2.45, 2.05, 1.60, 1.20, 0.80, 0.50,
    ),
    # WFH single: smoother daytime baseline, light evening.
    "wfh_single": (
        0.30, 0.30, 0.30, 0.30, 0.30, 0.35,
        0.55, 0.75, 0.95, 1.05, 1.10, 1.10,
        1.05, 1.10, 1.10, 1.05, 1.10, 1.25,
        1.30, 1.15, 0.95, 0.75, 0.55, 0.40,
    ),
    # Two earners, no kids: low daytime (away), sharp evening peak.
    "dual_income_no_kids": (
        0.30, 0.30, 0.30, 0.30, 0.30, 0.40,
        0.65, 0.85, 0.55, 0.40, 0.40, 0.40,
        0.40, 0.40, 0.40, 0.55, 0.95, 1.55,
        1.85, 1.65, 1.25, 0.85, 0.55, 0.35,
    ),
}

# Occupancy factor: how much non-shiftable load scales with people.
# Not linear — fridge/HVAC don't double with one more person.
OCCUPANCY_FACTOR = {1: 0.85, 2: 1.00, 3: 1.20, 4: 1.40}


def generate_base_load(
    archetype: str,
    occupants: int,
    simulation_hours: int,
    *,
    jitter: float = 0.08,
    seed: int | None = None,
) -> list[float]:
    """Return a `simulation_hours`-long kW series for a house's base load.

    Tiles the 24-hour archetype curve, scales by occupancy, and applies
    multiplicative jitter so the two days aren't identical.
    """
    if archetype not in ARCHETYPE_BASE_24H:
        raise ValueError(f"unknown archetype: {archetype}")
    if occupants not in OCCUPANCY_FACTOR:
        raise ValueError(f"occupants must be 1..4, got {occupants}")

    rng = random.Random(seed)
    curve = ARCHETYPE_BASE_24H[archetype]
    factor = OCCUPANCY_FACTOR[occupants]

    out: list[float] = []
    for h in range(simulation_hours):
        base = curve[h % 24] * factor
        noise = 1.0 + rng.uniform(-jitter, jitter)
        out.append(round(base * noise, 3))
    return out
