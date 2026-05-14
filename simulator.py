"""Deterministic load-curve simulator.

Given the static dataset (base loads + load definitions) and a schedule
(map from load_id to start datetime), produce the aggregate 48-hour kW
curve and headline metrics. This is the ground truth both for the
"before" baseline (each load at its default_start) and for any
agent-produced "after" schedule.

The kW for a partial trailing hour is prorated.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from schema import HEMSData, ShiftableLoad


@dataclass
class CurveResult:
    total_kw: list[float]           # per-hour aggregate kW, length = simulation_hours
    per_house_kw: dict[str, list[float]]
    peak_kw: float
    peak_hour: int
    peak_to_avg: float


def _hour_offset(when: datetime, sim_start: datetime) -> float:
    return (when - sim_start).total_seconds() / 3600.0


def _add_load(curve: list[float], load: ShiftableLoad, start: datetime, sim_start: datetime) -> None:
    h = _hour_offset(start, sim_start)
    remaining = load.duration_hours
    cursor = h
    while remaining > 1e-9:
        hour_idx = int(cursor)
        # how much of this calendar hour does the load occupy?
        used = min(remaining, (hour_idx + 1) - cursor)
        if 0 <= hour_idx < len(curve):
            curve[hour_idx] += load.power_kw * used
        cursor += used
        remaining -= used


def compute_curve(data: HEMSData, schedule: Mapping[str, datetime]) -> CurveResult:
    """Sum base loads + scheduled shiftable loads into a single curve.

    `schedule` is a mapping load_id -> start datetime. Every load_id in
    the dataset must appear in the schedule; raises KeyError otherwise.
    """
    hours = data.neighborhood.simulation_hours
    sim_start = data.neighborhood.simulation_start

    total = [0.0] * hours
    per_house: dict[str, list[float]] = {}

    for house in data.houses:
        h_curve = list(house.base_load_kw)  # start with base
        if len(h_curve) != hours:
            raise ValueError(
                f"{house.house_id} base_load_kw length {len(h_curve)} != simulation_hours {hours}"
            )
        for load in house.shiftable_loads:
            if load.load_id not in schedule:
                raise KeyError(f"schedule missing start for {load.load_id}")
            _add_load(h_curve, load, schedule[load.load_id], sim_start)
        per_house[house.house_id] = h_curve
        for i, v in enumerate(h_curve):
            total[i] += v

    peak_hour = max(range(hours), key=lambda i: total[i])
    peak = total[peak_hour]
    avg = sum(total) / hours

    return CurveResult(
        total_kw=total,
        per_house_kw=per_house,
        peak_kw=peak,
        peak_hour=peak_hour,
        peak_to_avg=peak / avg if avg else 0.0,
    )


def default_schedule(data: HEMSData) -> dict[str, datetime]:
    """The 'before' schedule: every load at its default_start."""
    return {
        load.load_id: load.default_start
        for house in data.houses
        for load in house.shiftable_loads
    }


def validate_schedule(data: HEMSData, schedule: Mapping[str, datetime]) -> list[str]:
    """Return a list of constraint-violation messages (empty = valid).

    Each load must start within [earliest_start, latest_finish - duration].
    """
    errors: list[str] = []
    for house in data.houses:
        for load in house.shiftable_loads:
            start = schedule.get(load.load_id)
            if start is None:
                errors.append(f"{load.load_id}: missing in schedule")
                continue
            from datetime import timedelta
            latest_start = load.latest_finish - timedelta(hours=load.duration_hours)
            if start < load.earliest_start:
                errors.append(
                    f"{load.load_id}: start {start.isoformat()} before "
                    f"earliest_start {load.earliest_start.isoformat()}"
                )
            elif start > latest_start:
                errors.append(
                    f"{load.load_id}: start {start.isoformat()} leaves <{load.duration_hours}h "
                    f"before latest_finish {load.latest_finish.isoformat()}"
                )
    return errors
