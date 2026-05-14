"""Generate a 10-day HEMS dataset deterministically.

Unlike `generate_data.py` (which prompts the LLM for a single rich day),
this script builds 10 days of loads from per-archetype templates so we
don't have to ask the LLM to emit 50+ load objects in one shot. The
result still conforms to the same `HEMSData` schema, so the simulator
and the runtime agents need no changes.
"""
from __future__ import annotations

import json
import random
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from archetypes import generate_base_load
from schema import (
    Archetype,
    HEMSData,
    House,
    Neighborhood,
    PrivateContext,
    ShiftableLoad,
)

OUTPUT_PATH = Path(__file__).parent / "data" / "neighborhood_10day.json"

NEIGHBORHOOD_ID = "hackathon-10day"
TIMEZONE = "America/New_York"
TZ_OFFSET = timezone(timedelta(hours=-4))
SIMULATION_START = datetime(2026, 5, 11, 0, 0, tzinfo=TZ_OFFSET)   # Monday
SIMULATION_DAYS = 10
SIMULATION_HOURS = SIMULATION_DAYS * 24

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# --- per-archetype behavior templates ---

HOUSES_SPEC: list[tuple[str, Archetype, int]] = [
    ("H1", "retired_couple", 2),
    ("H2", "young_family", 4),
    ("H3", "wfh_single", 1),
]

# Laundry days of week (0=Mon, 6=Sun) per archetype.
LAUNDRY_DAYS: dict[Archetype, list[int]] = {
    "retired_couple": [0, 4],          # Mon, Fri
    "young_family": [1, 3, 5],         # Tue, Thu, Sat
    "wfh_single": [5],                 # Sat
    "dual_income_no_kids": [3, 5],     # Thu, Sat
}

# EV: which days does the household drive? (weekdays default; family also weekends)
EV_DAYS: dict[Archetype, list[int]] = {
    "retired_couple": [0, 2, 4],       # not every day
    "young_family": [0, 1, 2, 3, 4, 5],  # incl. weekend errands
    "wfh_single": [1, 4],              # works from home, drives twice/week
    "dual_income_no_kids": [0, 1, 2, 3, 4],
}

EV_REASON_TEMPLATES = {
    "retired_couple": [
        "Errand run on {day} morning",
        "Visit with the grandkids {day}",
        "Afternoon doctor's appointment",
        "Bridge club drive on {day}",
    ],
    "young_family": [
        "School run + soccer practice {day}",
        "Family trip Saturday morning",
        "Carpool to {day} ballet",
        "Grocery and errands {day}",
        "Friday night drive to grandparents",
    ],
    "wfh_single": [
        "Tuesday gym across town",
        "Friday client visit downtown",
    ],
    "dual_income_no_kids": [
        "Commute to office {day}",
        "Date night Friday",
    ],
}

LAUNDRY_REASON_TEMPLATES = {
    "retired_couple": [
        "Weekly laundry on {day}",
        "Bed linens change",
    ],
    "young_family": [
        "Kids' clothes from school week",
        "Soccer kit and towels",
        "Weekend wash {day}",
    ],
    "wfh_single": [
        "Weekend wash {day}",
    ],
    "dual_income_no_kids": [
        "Weeknight catch-up laundry",
        "Weekend wash {day}",
    ],
}

WH_REASON_TEMPLATES = [
    "Evening showers and dishes",
    "Dishwasher run after dinner",
    "Hot water for morning",
    "Cooking and cleanup {day}",
    "Bath night for the kids",
]


def _at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=TZ_OFFSET)


def _pick(seq, rng):
    return seq[rng.randrange(len(seq))]


def _generate_loads(
    house_id: str,
    archetype: Archetype,
    rng: random.Random,
) -> list[ShiftableLoad]:
    loads: list[ShiftableLoad] = []
    start_date = SIMULATION_START.date()

    for day_idx in range(SIMULATION_DAYS):
        d = start_date + timedelta(days=day_idx)
        dow = d.weekday()
        dow_name = WEEKDAY_NAMES[dow]

        # ---- EV charging ----
        if dow in EV_DAYS.get(archetype, []):
            # plug-in: late afternoon / early evening
            plug_in_hr = 17 + rng.choice([0, 1])
            plug_in_min = rng.choice([0, 15, 30, 45])
            plug_in = _at(d, plug_in_hr, plug_in_min)
            # departure next morning
            departure = _at(d + timedelta(days=1), 7, 30)
            duration = round(rng.uniform(3.0, 4.0), 1)
            # uncoordinated default = start ~30 min after plug-in
            default_start = plug_in + timedelta(minutes=rng.choice([0, 15, 30]))
            reason_tpl = _pick(EV_REASON_TEMPLATES[archetype], rng)
            loads.append(ShiftableLoad(
                load_id=f"{house_id}-EV-D{day_idx}",
                type="ev_charging",
                power_kw=7.2,
                duration_hours=duration,
                preemptible=False,
                earliest_start=plug_in,
                latest_finish=departure,
                default_start=default_start,
                private_context=PrivateContext(
                    reason=reason_tpl.format(day=dow_name),
                    flexibility_score=round(rng.uniform(0.55, 0.85), 2),
                ),
            ))

        # ---- Laundry ----
        if dow in LAUNDRY_DAYS.get(archetype, []):
            duration = round(rng.uniform(1.5, 2.0), 2)
            # default after-dinner laundry
            default_start = _at(d, 18 + rng.choice([0, 1]), rng.choice([0, 15, 30]))
            reason_tpl = _pick(LAUNDRY_REASON_TEMPLATES[archetype], rng)
            loads.append(ShiftableLoad(
                load_id=f"{house_id}-LAUNDRY-D{day_idx}",
                type="washer_dryer",
                power_kw=2.5,
                duration_hours=duration,
                preemptible=False,
                earliest_start=_at(d, 7, 0),
                latest_finish=_at(d, 22, 0),
                default_start=default_start,
                private_context=PrivateContext(
                    reason=reason_tpl.format(day=dow_name),
                    flexibility_score=round(rng.uniform(0.65, 0.9), 2),
                ),
            ))

        # ---- Water heater: every day ----
        duration = round(rng.uniform(1.0, 1.5), 2)
        # default early evening
        default_start = _at(d, 19 + rng.choice([0, 0, 1]), rng.choice([0, 15, 30]))
        reason_tpl = _pick(WH_REASON_TEMPLATES, rng)
        loads.append(ShiftableLoad(
            load_id=f"{house_id}-WH-D{day_idx}",
            type="water_heater",
            power_kw=1.5,
            duration_hours=duration,
            preemptible=False,
            earliest_start=_at(d, 5, 0),
            latest_finish=_at(d, 23, 0),
            default_start=default_start,
            private_context=PrivateContext(
                reason=reason_tpl.format(day=dow_name),
                flexibility_score=round(rng.uniform(0.5, 0.75), 2),
            ),
        ))

    return loads


def main() -> int:
    houses: list[House] = []
    for idx, (house_id, archetype, occupants) in enumerate(HOUSES_SPEC, start=1):
        rng = random.Random(idx * 1009 + 7)
        base = generate_base_load(
            archetype=archetype,
            occupants=occupants,
            simulation_hours=SIMULATION_HOURS,
            jitter=0.10,
            seed=idx,
        )
        loads = _generate_loads(house_id=house_id, archetype=archetype, rng=rng)
        houses.append(House(
            house_id=house_id,
            archetype=archetype,
            occupants=occupants,
            base_load_kw=base,
            shiftable_loads=loads,
        ))

    data = HEMSData(
        neighborhood=Neighborhood(
            id=NEIGHBORHOOD_ID,
            timezone=TIMEZONE,
            simulation_start=SIMULATION_START,
            simulation_hours=SIMULATION_HOURS,
        ),
        houses=houses,
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(data.model_dump_json(indent=2))
    print(f"Wrote {OUTPUT_PATH}")
    for h in data.houses:
        n_ev = sum(1 for ld in h.shiftable_loads if ld.type == "ev_charging")
        n_wd = sum(1 for ld in h.shiftable_loads if ld.type == "washer_dryer")
        n_wh = sum(1 for ld in h.shiftable_loads if ld.type == "water_heater")
        print(
            f"  {h.house_id} {h.archetype:18s} occ={h.occupants}  "
            f"loads: {len(h.shiftable_loads)} ({n_ev} EV, {n_wd} laundry, {n_wh} water-heater)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
