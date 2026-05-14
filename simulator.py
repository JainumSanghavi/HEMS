"""Deterministic energy simulator (rich edition).

Computes per-house and aggregate hourly curves given a schedule. With
the richer schema this now models:

  - Base load (non-shiftable)
  - Shiftable loads at their scheduled start
  - Rooftop solar generation per house, from irradiance × panel_kw
  - Home battery dispatch (default policy: charge surplus solar,
    discharge during peak-tariff hours)
  - Net grid demand per hour = base + shiftable − solar
                              + battery_charge − battery_discharge
  - $ cost per hour from the tariff
  - kg CO₂ per hour from grid carbon intensity

The agents make load-scheduling decisions; battery dispatch follows a
sensible default policy here (charge with surplus, discharge in peak
hours when SOC > 30%) so the simulator can always score any schedule
without requiring the LLM to also produce a battery schedule.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping

from schema import HEMSData, House, ShiftableLoad, Tariff


# ----- result containers --------------------------------------------------

@dataclass
class HouseTrace:
    base_kw:       list[float]   # non-shiftable consumption
    shiftable_kw:  list[float]   # scheduled loads (EV, laundry, water heater)
    solar_kw:      list[float]   # generation (negative load)
    battery_kw:    list[float]   # +charge / -discharge (signed)
    battery_soc:   list[float]   # state of charge per hour (kWh)
    net_grid_kw:   list[float]   # what we draw from grid (negative = exporting)


@dataclass
class CurveResult:
    total_kw:      list[float]   # aggregate gross load (no solar/battery)
    net_grid_kw:   list[float]   # aggregate net grid demand
    per_house:     dict[str, HouseTrace] = field(default_factory=dict)
    peak_kw:       float = 0.0
    peak_hour:     int = 0
    peak_to_avg:   float = 0.0
    total_kwh:     float = 0.0
    total_cost:    float = 0.0
    total_co2_kg:  float = 0.0


# ----- helpers ------------------------------------------------------------

def _hour_offset(when: datetime, sim_start: datetime) -> float:
    return (when - sim_start).total_seconds() / 3600.0


def _add_load(curve: list[float], load: ShiftableLoad, start: datetime, sim_start: datetime) -> None:
    h = _hour_offset(start, sim_start)
    remaining = load.duration_hours
    cursor = h
    H = len(curve)
    while remaining > 1e-9:
        hour_idx = int(cursor)
        used = min(remaining, (hour_idx + 1) - cursor)
        if 0 <= hour_idx < H:
            curve[hour_idx] += load.power_kw * used
        cursor += used
        remaining -= used


def _solar_generation(house: House, irradiance_wm2: list[float]) -> list[float]:
    """Convert hourly W/m² to kW output. Treat 1000 W/m² as panel_kw at
    100% (peak STC condition); scale linearly. Inverter efficiency
    applied once."""
    if house.solar is None or not irradiance_wm2:
        return [0.0] * len(irradiance_wm2 or [])
    factor = house.solar.panel_kw * house.solar.inverter_efficiency / 1000.0
    return [round(max(0.0, irr * factor), 4) for irr in irradiance_wm2]


def _is_peak_hour(hour_offset: int, sim_start: datetime, tariff: Tariff) -> bool:
    """Peak hours are wall-clock hours-of-day in tariff.peak_hours."""
    dt = sim_start + timedelta(hours=hour_offset)
    return dt.hour in tariff.peak_hours


def _dispatch_battery(
    house: House,
    net_before_battery: list[float],
    tariff: Tariff,
    sim_start: datetime,
) -> tuple[list[float], list[float]]:
    """Default policy: charge from solar surplus (negative net), discharge
    when net > 0 during peak tariff hours and battery has > 30% SOC.

    Returns (battery_kw_signed, soc_per_hour). battery_kw_signed is
    positive when charging, negative when discharging.
    """
    H = len(net_before_battery)
    if house.battery is None:
        return [0.0] * H, [0.0] * H

    b = house.battery
    soc_kwh = b.capacity_kwh * b.initial_soc
    battery = [0.0] * H
    soc = [0.0] * H
    eta_one_way = b.round_trip_efficiency ** 0.5  # split efficiency over the round-trip

    for h in range(H):
        net = net_before_battery[h]
        if net < 0:
            # surplus from solar — charge if we have capacity
            available = b.capacity_kwh - soc_kwh
            want_charge = min(-net, b.max_charge_kw, available / eta_one_way)
            if want_charge > 0:
                battery[h] = want_charge
                soc_kwh += want_charge * eta_one_way
        else:
            # net demand — discharge during peak if we have headroom
            if _is_peak_hour(h, sim_start, tariff) and soc_kwh > 0.3 * b.capacity_kwh:
                want_discharge = min(net, b.max_discharge_kw, (soc_kwh - 0.2 * b.capacity_kwh))
                if want_discharge > 0:
                    battery[h] = -want_discharge
                    soc_kwh -= want_discharge / eta_one_way
        soc[h] = round(soc_kwh, 3)
    return battery, soc


# ----- public API ---------------------------------------------------------

def compute_curve(data: HEMSData, schedule: Mapping[str, datetime]) -> CurveResult:
    """Run the deterministic simulation for a given load schedule.

    Every load_id in the dataset must appear in the schedule; raises
    KeyError otherwise.
    """
    H = data.neighborhood.simulation_hours
    sim_start = data.neighborhood.simulation_start
    tariff = data.neighborhood.tariff
    irradiance = data.neighborhood.solar_irradiance_wm2 or [0.0] * H
    carbon = data.neighborhood.carbon_intensity_gco2_per_kwh or [0.0] * H

    per_house: dict[str, HouseTrace] = {}
    aggregate_total = [0.0] * H
    aggregate_net = [0.0] * H

    for house in data.houses:
        # base + shiftable
        base = list(house.base_load_kw)
        if len(base) != H:
            raise ValueError(f"{house.house_id} base_load_kw length {len(base)} != {H}")
        shiftable = [0.0] * H
        for load in house.shiftable_loads:
            if load.load_id not in schedule:
                raise KeyError(f"schedule missing start for {load.load_id}")
            _add_load(shiftable, load, schedule[load.load_id], sim_start)

        # solar
        solar = _solar_generation(house, irradiance)

        # net before battery: base + shiftable - solar
        net_before = [base[i] + shiftable[i] - solar[i] for i in range(H)]

        # battery dispatch
        battery, soc = _dispatch_battery(house, net_before, tariff, sim_start)

        # final net grid draw
        net_grid = [net_before[i] + battery[i] for i in range(H)]

        per_house[house.house_id] = HouseTrace(
            base_kw=base,
            shiftable_kw=shiftable,
            solar_kw=solar,
            battery_kw=battery,
            battery_soc=soc,
            net_grid_kw=net_grid,
        )

        for i in range(H):
            aggregate_total[i] += base[i] + shiftable[i]
            aggregate_net[i]   += net_grid[i]

    peak_hour = max(range(H), key=lambda i: aggregate_net[i])
    peak = aggregate_net[peak_hour]
    avg = sum(aggregate_net) / H if H else 0.0
    total_kwh = sum(max(0.0, v) for v in aggregate_net)

    # cost: sum of net_grid[i] * (peak_rate if peak hour else offpeak), with
    # exports credited at export_credit_per_kwh.
    cost = 0.0
    for h in range(H):
        rate = (
            tariff.peak_rate_per_kwh if _is_peak_hour(h, sim_start, tariff)
            else tariff.offpeak_rate_per_kwh
        )
        v = aggregate_net[h]
        if v >= 0:
            cost += v * rate
        else:
            cost -= -v * tariff.export_credit_per_kwh

    # carbon: only counts grid import (exports are zero-rated here).
    co2_g = sum(max(0.0, aggregate_net[h]) * carbon[h] for h in range(H))

    return CurveResult(
        total_kw=aggregate_total,
        net_grid_kw=aggregate_net,
        per_house=per_house,
        peak_kw=peak,
        peak_hour=peak_hour,
        peak_to_avg=peak / avg if avg else 0.0,
        total_kwh=total_kwh,
        total_cost=round(cost, 2),
        total_co2_kg=round(co2_g / 1000.0, 2),
    )


def default_schedule(data: HEMSData) -> dict[str, datetime]:
    """The 'before' schedule: every load at its default_start."""
    return {
        load.load_id: load.default_start
        for house in data.houses
        for load in house.shiftable_loads
    }


def validate_schedule(data: HEMSData, schedule: Mapping[str, datetime]) -> list[str]:
    """Return a list of constraint-violation messages; empty list = valid."""
    errors: list[str] = []
    for house in data.houses:
        for load in house.shiftable_loads:
            start = schedule.get(load.load_id)
            if start is None:
                errors.append(f"{load.load_id}: missing in schedule")
                continue
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
