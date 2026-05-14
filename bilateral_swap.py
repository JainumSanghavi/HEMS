"""Bilateral swap phase.

After the main coordinator-led rounds converge, each agent gets one
more chance to improve the schedule via direct peer-to-peer style
moves. The loop:

  1. Identify loads contributing to the current peak hour.
  2. For each such load, look for an alternative low-congestion hour
     inside the load's allowed window.
  3. Ask the owning house agent: "would you accept moving this load
     to that hour? Here is the rationale." The agent evaluates against
     its private preferences and returns accept / refuse + reason.
  4. If accepted AND the simulator confirms the move actually lowers
     peak, apply it. Record the SwapEvent.
  5. Repeat until no peak-reducing swap is accepted, or budget runs out.

The agent's evaluate_swap call gives it real autonomy — a frugal house
will say yes to an off-peak slot, an eco-conscious house will say yes
to a low-carbon slot, a comfort-driven family might refuse if the
new slot is 3am.
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta

from agent_schemas import SwapEvent
from house_agent import HouseAgent
from schema import HEMSData
from simulator import compute_curve


MAX_SWAPS = 8
MIN_PEAK_DELTA = 0.1  # kW — minimum improvement required to attempt a swap


def _hour_offset(when: datetime, sim_start: datetime) -> float:
    return (when - sim_start).total_seconds() / 3600.0


def _candidate_alternatives(
    data: HEMSData,
    load,
    schedule: dict[str, datetime],
    avoid_hour: int,
) -> list[float]:
    """Return some candidate alternative start hours for this load — the
    least-crowded slots inside its valid window."""
    sim_start = data.neighborhood.simulation_start
    H = data.neighborhood.simulation_hours
    earliest = _hour_offset(load.earliest_start, sim_start)
    latest = _hour_offset(load.latest_finish, sim_start) - load.duration_hours

    # Score every valid integer-hour candidate by what the aggregate kW
    # would look like if we placed this load there. Cheaper to do once
    # against the current curve than to re-simulate per candidate.
    base_res = compute_curve(data, schedule)
    candidates = []
    # Round earliest UP and latest DOWN to be strictly inside the valid window
    # (avoids float precision rejections in evaluate_swap).
    lo = math.ceil(earliest)
    hi = math.floor(latest)
    for h in range(lo, hi + 1):
        if h == avoid_hour:
            continue
        end = h + int(load.duration_hours)
        score = sum(base_res.net_grid_kw[i] for i in range(h, min(H, end)))
        candidates.append((score, float(h)))
    candidates.sort()
    return [c[1] for c in candidates[:3]]


async def run_bilateral_swaps(
    data: HEMSData,
    agents_by_house: dict[str, HouseAgent],
    schedule: dict[str, datetime],
    initial_curve_result,
) -> tuple[dict[str, datetime], list[SwapEvent]]:
    sim_start = data.neighborhood.simulation_start
    events: list[SwapEvent] = []
    current_schedule = dict(schedule)

    for swap_attempt in range(MAX_SWAPS):
        curve = compute_curve(data, current_schedule)
        peak_hour = curve.peak_hour
        peak_kw = curve.peak_kw

        # Find loads scheduled at or spanning the peak hour.
        contributors = []
        for house in data.houses:
            for load in house.shiftable_loads:
                start_dt = current_schedule[load.load_id]
                start_h = _hour_offset(start_dt, sim_start)
                end_h = start_h + load.duration_hours
                if start_h <= peak_hour < end_h:
                    contributors.append((house, load, start_h))
        if not contributors:
            break

        # Sort: most-flexible loads first (they're most likely to accept).
        contributors.sort(key=lambda t: -t[1].private_context.flexibility_score)

        progress = False
        for house, load, start_h in contributors:
            cands = _candidate_alternatives(data, load, current_schedule, peak_hour)
            if not cands:
                continue

            for new_h in cands:
                # Probe: would moving here actually lower peak?
                trial = dict(current_schedule)
                trial[load.load_id] = sim_start + timedelta(hours=new_h)
                trial_curve = compute_curve(data, trial)
                delta = peak_kw - trial_curve.peak_kw
                if delta < MIN_PEAK_DELTA:
                    continue

                agent = agents_by_house[house.house_id]
                rationale_offered = (
                    f"Move from hour {start_h:.1f} (currently part of the {peak_kw:.1f} kW peak) "
                    f"to hour {new_h:.1f}, projected to drop the peak by {delta:.2f} kW."
                )
                accept, reply = await agent.evaluate_swap(
                    my_load_id=load.load_id,
                    proposed_new_start=new_h,
                    reason=rationale_offered,
                )
                events.append(SwapEvent(
                    load_id=load.load_id,
                    house_id=house.house_id,
                    from_hour=start_h,
                    to_hour=new_h,
                    accepted=accept,
                    rationale=reply,
                    reduced_peak_kw=delta if accept else 0.0,
                ))
                if accept:
                    current_schedule[load.load_id] = trial[load.load_id]
                    progress = True
                    break
            if progress:
                break

        if not progress:
            break

    return current_schedule, events
