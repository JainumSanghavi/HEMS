"""Generate a 10-day rich HEMS dataset.

Produces 3 distinct houses with heterogeneous systems (one with no
solar, one with solar + battery, one with solar only) plus
neighborhood-level exogenous signals: weather, solar irradiance,
carbon intensity, time-of-use tariff, and one demand-response event.

All numeric arrays are produced deterministically in Python (the LLM
is poor at long numeric series). Per-load reasons come from short
template libraries so the agents have realistic narrative to reason
about at runtime. The result conforms to the rich HEMSData schema.
"""
from __future__ import annotations

import math
import random
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from archetypes import generate_base_load
from schema import (
    Archetype,
    Battery,
    DREvent,
    HEMSData,
    House,
    HouseholdPreferences,
    Neighborhood,
    PrivateContext,
    ShiftableLoad,
    Solar,
    Tariff,
)


OUTPUT_PATH = Path(__file__).parent / "data" / "neighborhood_rich.json"

NEIGHBORHOOD_ID = "hackathon-rich-10day"
TIMEZONE = "America/New_York"
TZ_OFFSET = timezone(timedelta(hours=-4))
SIMULATION_START = datetime(2026, 5, 11, 0, 0, tzinfo=TZ_OFFSET)   # Monday
SIMULATION_DAYS = 10
SIMULATION_HOURS = SIMULATION_DAYS * 24

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# -----------------------------------------------------------------
# House definitions — heterogeneous systems on purpose so agents
# have meaningfully different problems to solve.
# -----------------------------------------------------------------

HOUSES_SPEC: list[dict] = [
    {
        "house_id": "H1",
        "archetype": "retired_couple",
        "occupants": 2,
        "solar": None,                              # never installed
        "battery": None,
        "preferences": HouseholdPreferences(
            cost_weight=0.55, comfort_weight=0.25, carbon_weight=0.15, reliability_weight=0.05,
            personality=(
                "Frugal retired engineer. Watches the utility meter daily. "
                "Will gladly shift loads to save a dollar but does not want to "
                "wake up to a cold shower."
            ),
        ),
    },
    {
        "house_id": "H2",
        "archetype": "young_family",
        "occupants": 4,
        "solar":   Solar(panel_kw=7.5),             # full roof
        "battery": Battery(capacity_kwh=13.5, max_charge_kw=5.0, max_discharge_kw=5.0, initial_soc=0.4),
        "preferences": HouseholdPreferences(
            cost_weight=0.30, comfort_weight=0.50, carbon_weight=0.15, reliability_weight=0.05,
            personality=(
                "Busy parents of two kids. Comfort and predictable routines "
                "beat squeezing every cent. Two cars, soccer practice, the "
                "occasional Saturday trip. Quietly proud of the panels."
            ),
        ),
    },
    {
        "house_id": "H3",
        "archetype": "wfh_single",
        "occupants": 1,
        "solar":   Solar(panel_kw=4.0),             # modest array
        "battery": None,                            # can't afford one yet
        "preferences": HouseholdPreferences(
            cost_weight=0.30, comfort_weight=0.20, carbon_weight=0.45, reliability_weight=0.05,
            personality=(
                "Eco-conscious WFH consultant. Reads the grid's hourly carbon "
                "intensity for fun. Happy to defer laundry to a windy afternoon. "
                "Solar pays the morning coffee."
            ),
        ),
    },
]


# Load templates -------------------------------------------------------------

LAUNDRY_DAYS: dict[Archetype, list[int]] = {
    "retired_couple": [0, 4],
    "young_family":   [1, 3, 5],
    "wfh_single":     [5],
    "dual_income_no_kids": [3, 5],
}
EV_DAYS: dict[Archetype, list[int]] = {
    "retired_couple": [0, 2, 4],
    "young_family":   [0, 1, 2, 3, 4, 5],
    "wfh_single":     [1, 4],
    "dual_income_no_kids": [0, 1, 2, 3, 4],
}
EV_REASONS = {
    "retired_couple": [
        "Errand run on {day} morning",
        "Visit with the grandkids {day}",
        "Bridge club drive on {day}",
        "Afternoon clinic appointment",
    ],
    "young_family": [
        "School run + soccer practice {day}",
        "Saturday family beach trip",
        "Carpool to ballet on {day}",
        "Friday night drive to grandparents",
        "Groceries and errands {day}",
    ],
    "wfh_single": [
        "Tuesday gym across town",
        "Friday client visit downtown",
    ],
}
LAUNDRY_REASONS = {
    "retired_couple": ["Weekly laundry on {day}", "Bed linens change"],
    "young_family":   ["Kids' clothes from the school week", "Soccer kit and towels", "Weekend wash {day}"],
    "wfh_single":     ["Weekend wash {day}"],
}
WH_REASONS = [
    "Evening showers and dishes",
    "Dishwasher run after dinner",
    "Hot water for the morning",
    "Cooking and cleanup {day}",
    "Bath night for the kids",
]


def _at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=TZ_OFFSET)


def _pick(seq, rng):
    return seq[rng.randrange(len(seq))]


def _generate_loads(house_id: str, archetype: Archetype, rng: random.Random) -> list[ShiftableLoad]:
    loads: list[ShiftableLoad] = []
    start_date = SIMULATION_START.date()

    for day_idx in range(SIMULATION_DAYS):
        d = start_date + timedelta(days=day_idx)
        dow = d.weekday()
        dow_name = WEEKDAY_NAMES[dow]

        if dow in EV_DAYS.get(archetype, []):
            plug_in_hr  = 17 + rng.choice([0, 1])
            plug_in_min = rng.choice([0, 15, 30, 45])
            plug_in   = _at(d, plug_in_hr, plug_in_min)
            departure = _at(d + timedelta(days=1), 7, 30)
            duration  = round(rng.uniform(3.0, 4.0), 1)
            default_start = plug_in + timedelta(minutes=rng.choice([0, 15, 30]))
            loads.append(ShiftableLoad(
                load_id=f"{house_id}-EV-D{day_idx}",
                type="ev_charging", power_kw=7.2, duration_hours=duration,
                earliest_start=plug_in, latest_finish=departure, default_start=default_start,
                private_context=PrivateContext(
                    reason=_pick(EV_REASONS[archetype], rng).format(day=dow_name),
                    flexibility_score=round(rng.uniform(0.55, 0.85), 2),
                ),
            ))

        if dow in LAUNDRY_DAYS.get(archetype, []):
            duration = round(rng.uniform(1.5, 2.0), 2)
            default_start = _at(d, 18 + rng.choice([0, 1]), rng.choice([0, 15, 30]))
            loads.append(ShiftableLoad(
                load_id=f"{house_id}-LAUNDRY-D{day_idx}",
                type="washer_dryer", power_kw=2.5, duration_hours=duration,
                earliest_start=_at(d, 7, 0), latest_finish=_at(d, 22, 0), default_start=default_start,
                private_context=PrivateContext(
                    reason=_pick(LAUNDRY_REASONS[archetype], rng).format(day=dow_name),
                    flexibility_score=round(rng.uniform(0.65, 0.90), 2),
                ),
            ))

        duration = round(rng.uniform(1.0, 1.5), 2)
        default_start = _at(d, 19 + rng.choice([0, 0, 1]), rng.choice([0, 15, 30]))
        loads.append(ShiftableLoad(
            load_id=f"{house_id}-WH-D{day_idx}",
            type="water_heater", power_kw=1.5, duration_hours=duration,
            earliest_start=_at(d, 5, 0), latest_finish=_at(d, 23, 0), default_start=default_start,
            private_context=PrivateContext(
                reason=_pick(WH_REASONS, rng).format(day=dow_name),
                flexibility_score=round(rng.uniform(0.5, 0.75), 2),
            ),
        ))

    return loads


# Neighborhood signals -------------------------------------------------------

def _solar_irradiance(hours: int, rng: random.Random) -> tuple[list[float], list[float]]:
    """Per-hour solar irradiance (W/m²) and cloud cover %.

    Curve: clear-sky sinusoid from 06:00 to 20:00, peak ~950 W/m² at
    13:00, modulated by a per-day cloud factor (rainy day = mostly
    clouds, clear day = thin clouds).
    """
    irr: list[float] = []
    cloud: list[float] = []
    day_cloud_factors = [rng.uniform(0.05, 0.95) for _ in range(hours // 24 + 1)]
    # bias day 2 and day 6 to be cloudy on purpose so the visual story has shape
    day_cloud_factors[2] = max(day_cloud_factors[2], 0.75)
    day_cloud_factors[6] = max(day_cloud_factors[6], 0.6)

    for h in range(hours):
        hour_of_day = h % 24
        day_idx     = h // 24
        cf = day_cloud_factors[day_idx]
        # base curve: 0 outside [6, 20]; sin between
        if 6 <= hour_of_day <= 20:
            theta = math.pi * (hour_of_day - 6) / 14.0
            clear_sky = 950.0 * math.sin(theta)
        else:
            clear_sky = 0.0
        # cloud attenuation
        irr_h = clear_sky * (1.0 - 0.75 * cf) * (1.0 + rng.uniform(-0.05, 0.05))
        irr.append(round(max(0.0, irr_h), 1))
        cloud.append(round(cf * 100, 1))
    return irr, cloud


def _temperature(hours: int, rng: random.Random) -> list[float]:
    """Spring-week temperatures, F. Cycles between night low and afternoon high
    with day-to-day variation."""
    out = []
    day_means = [rng.uniform(58, 72) for _ in range(hours // 24 + 1)]
    for h in range(hours):
        hour_of_day = h % 24
        day_idx     = h // 24
        # daily swing ~16F amplitude, peak at 15:00, trough at 04:00
        swing = 8.0 * math.cos(math.pi * (hour_of_day - 15) / 12.0)
        out.append(round(day_means[day_idx] + swing + rng.uniform(-1, 1), 1))
    return out


def _carbon_intensity(hours: int, rng: random.Random) -> list[float]:
    """Grid carbon intensity (gCO₂/kWh). Higher during demand peaks (peaker
    plants), lower overnight when more baseload+wind."""
    out = []
    for h in range(hours):
        hour_of_day = h % 24
        # overnight clean baseline 180; rises to ~500 at evening peak
        if 17 <= hour_of_day <= 21:
            base = 460.0
        elif 0 <= hour_of_day <= 5 or hour_of_day == 23:
            base = 190.0
        elif 10 <= hour_of_day <= 15:
            # midday solar dampens
            base = 270.0
        else:
            base = 340.0
        out.append(round(base + rng.uniform(-25, 25), 1))
    return out


def main() -> int:
    houses: list[House] = []
    for spec_idx, spec in enumerate(HOUSES_SPEC, start=1):
        rng = random.Random(spec_idx * 1009 + 7)
        base = generate_base_load(
            archetype=spec["archetype"],
            occupants=spec["occupants"],
            simulation_hours=SIMULATION_HOURS,
            jitter=0.10,
            seed=spec_idx,
        )
        loads = _generate_loads(spec["house_id"], spec["archetype"], rng)
        houses.append(House(
            house_id=spec["house_id"],
            archetype=spec["archetype"],
            occupants=spec["occupants"],
            base_load_kw=base,
            shiftable_loads=loads,
            solar=spec["solar"],
            battery=spec["battery"],
            preferences=spec["preferences"],
        ))

    sig_rng = random.Random(42)
    irr, cloud = _solar_irradiance(SIMULATION_HOURS, sig_rng)
    temps      = _temperature(SIMULATION_HOURS, sig_rng)
    carbon     = _carbon_intensity(SIMULATION_HOURS, sig_rng)

    tariff = Tariff(
        peak_hours=[16, 17, 18, 19, 20],
        peak_rate_per_kwh=0.32,
        offpeak_rate_per_kwh=0.11,
        export_credit_per_kwh=0.05,
    )

    # One demand-response event on Friday (day index 4), 18:00-20:00
    dr_events = [
        DREvent(
            start_hour=4 * 24 + 18,
            duration_hours=2,
            target_reduction_kw=6.0,
            incentive_per_kwh=0.50,
        )
    ]

    data = HEMSData(
        neighborhood=Neighborhood(
            id=NEIGHBORHOOD_ID,
            timezone=TIMEZONE,
            simulation_start=SIMULATION_START,
            simulation_hours=SIMULATION_HOURS,
            weather_temp_f=temps,
            cloud_cover_pct=cloud,
            solar_irradiance_wm2=irr,
            carbon_intensity_gco2_per_kwh=carbon,
            tariff=tariff,
            dr_events=dr_events,
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
        solar = f"{h.solar.panel_kw}kW solar" if h.solar else "no solar"
        bat   = f"{h.battery.capacity_kwh}kWh battery" if h.battery else "no battery"
        print(
            f"  {h.house_id} {h.archetype:18s} occ={h.occupants}  loads: "
            f"{len(h.shiftable_loads)} ({n_ev}EV/{n_wd}wash/{n_wh}wh)  · {solar} · {bat}"
        )
    print(f"  peak solar irradiance: {max(irr):.0f} W/m²")
    print(f"  carbon range: {min(carbon):.0f}–{max(carbon):.0f} gCO₂/kWh")
    print(f"  tariff peak hours: {tariff.peak_hours}, peak {tariff.peak_rate_per_kwh}/kWh, off {tariff.offpeak_rate_per_kwh}/kWh")
    print(f"  {len(dr_events)} demand-response event(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
