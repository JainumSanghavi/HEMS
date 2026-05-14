"""Orchestrate the full HEMS negotiation end-to-end.

Pipeline:

  1. Load the rich neighborhood data (default: data/neighborhood_rich.json).
  2. Compute the "before" baseline curve, cost, and carbon from each
     load's default_start.
  3. Run N coordinator-led rounds (default 3) of bidding. Each round:
     houses bid in parallel, coordinator aggregates + narrates.
  4. Run the bilateral-swap phase: agents directly accept/refuse
     proposed peak-reducing moves, weighted by their private preferences.
  5. Compute the final curve, cost, and carbon.
  6. Write data/transcript_rich.json (UI source of truth) and
     data/schedule_after_rich.json.

Env vars:
  HEMS_DATA         path to the input dataset
  HEMS_TRANSCRIPT   path to the output transcript
  HEMS_SCHEDULE     path to the final schedule
  HEMS_ROUNDS       integer (default 3)
  OLLAMA_HOST/MODEL Ollama target (default localhost + gpt-oss:120b-cloud)
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from ollama import AsyncClient

from agent_schemas import Transcript, TranscriptRound
from bilateral_swap import run_bilateral_swaps
from coordinator import CoordinatorAgent
from house_agent import HouseAgent
from schema import HEMSData
from simulator import compute_curve, default_schedule


DATA_PATH = Path(os.environ.get("HEMS_DATA", str(Path(__file__).parent / "data" / "neighborhood_rich.json")))
TRANSCRIPT_PATH = Path(os.environ.get("HEMS_TRANSCRIPT", str(Path(__file__).parent / "data" / "transcript_rich.json")))
SCHEDULE_AFTER_PATH = Path(os.environ.get("HEMS_SCHEDULE", str(Path(__file__).parent / "data" / "schedule_after_rich.json")))

DEFAULT_MODEL = "gpt-oss:120b-cloud"
DEFAULT_HOST = "http://localhost:11434"
ROUNDS = 3


def _iso_schedule(schedule: dict[str, datetime]) -> dict[str, str]:
    return {k: v.isoformat() for k, v in schedule.items()}


async def run_negotiation(data: HEMSData, client: AsyncClient, model: str, rounds: int) -> Transcript:
    sim_start = data.neighborhood.simulation_start

    houses = [HouseAgent(house=h, data=data, client=client, model=model) for h in data.houses]
    agents_by_house = {h.house_id: agent for agent, h in zip(houses, data.houses)}
    coord = CoordinatorAgent(data=data, client=client, model=model)

    # --- Before baseline ---
    before = default_schedule(data)
    before_curve = compute_curve(data, before)
    print(f"BEFORE: peak={before_curve.peak_kw:.2f} kW @ h{before_curve.peak_hour}, "
          f"peak/avg={before_curve.peak_to_avg:.2f}, cost=${before_curve.total_cost:.2f}, "
          f"CO2={before_curve.total_co2_kg:.1f} kg")

    prices = [0.0] * data.neighborhood.simulation_hours
    coordinator_message = ""
    flagged_loads: list[str] = []
    rounds_log: list[TranscriptRound] = []
    final_schedule: dict[str, datetime] = {}

    for r in range(1, rounds + 1):
        print(f"\n--- Round {r} ---")
        bids = await asyncio.gather(*(
            h.bid(round_number=r, prices=prices, coordinator_message=coordinator_message, flagged_loads=flagged_loads)
            for h in houses
        ))
        schedule, curve, new_prices = coord.integrate(bids)
        cm = await coord.narrate(round_number=r, schedule=schedule, curve=curve, prices=new_prices, bids=bids)

        print(f"  peak={curve.peak_kw:.2f} kW @ h{curve.peak_hour}  cost=${curve.total_cost:.2f}  "
              f"CO2={curve.total_co2_kg:.1f} kg")
        print(f"  coord: {cm.message[:150]}")
        if cm.flagged_loads:
            print(f"  flagged: {cm.flagged_loads}")

        rounds_log.append(TranscriptRound(
            round_number=r,
            coordinator_message=cm.message,
            flagged_loads=cm.flagged_loads,
            prices=new_prices,
            house_messages={b.house_id: b.message for b in bids},
            schedule=_iso_schedule(schedule),
            peak_kw=curve.peak_kw,
            peak_hour=curve.peak_hour,
        ))

        prices = new_prices
        coordinator_message = cm.message
        flagged_loads = cm.flagged_loads
        final_schedule = schedule

    # --- Bilateral swap phase ---
    print("\n--- Bilateral swaps ---")
    pre_swap_curve = compute_curve(data, final_schedule)
    final_schedule, swap_events = await run_bilateral_swaps(
        data=data,
        agents_by_house=agents_by_house,
        schedule=final_schedule,
        initial_curve_result=pre_swap_curve,
    )
    accepted_count = sum(1 for s in swap_events if s.accepted)
    print(f"  attempts: {len(swap_events)}  accepted: {accepted_count}")
    for s in swap_events[-6:]:
        flag = "✓" if s.accepted else "✗"
        print(f"  {flag} {s.load_id} h{s.from_hour:.1f}->h{s.to_hour:.1f}  Δ{s.reduced_peak_kw:+.2f} kW  '{s.rationale[:70]}'")

    final_curve = compute_curve(data, final_schedule)
    reduction = (before_curve.peak_kw - final_curve.peak_kw) / before_curve.peak_kw * 100
    cost_savings = before_curve.total_cost - final_curve.total_cost
    co2_savings = before_curve.total_co2_kg - final_curve.total_co2_kg
    print(f"\nAFTER: peak={final_curve.peak_kw:.2f} kW @ h{final_curve.peak_hour}  "
          f"cost=${final_curve.total_cost:.2f}  CO2={final_curve.total_co2_kg:.1f} kg")
    print(f"REDUCTIONS  peak: {reduction:.1f}%   $: ${cost_savings:.2f}   CO2: {co2_savings:.1f} kg")

    return Transcript(
        before=_iso_schedule(before),
        before_peak_kw=before_curve.peak_kw,
        before_peak_hour=before_curve.peak_hour,
        before_cost_usd=before_curve.total_cost,
        before_co2_kg=before_curve.total_co2_kg,
        rounds=rounds_log,
        swaps=swap_events,
        final=_iso_schedule(final_schedule),
        final_peak_kw=final_curve.peak_kw,
        final_peak_hour=final_curve.peak_hour,
        final_cost_usd=final_curve.total_cost,
        final_co2_kg=final_curve.total_co2_kg,
        peak_reduction_pct=reduction,
        cost_savings_usd=cost_savings,
        co2_savings_kg=co2_savings,
    )


async def main() -> int:
    model = os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)
    host = os.environ.get("OLLAMA_HOST", DEFAULT_HOST)
    rounds = int(os.environ.get("HEMS_ROUNDS", str(ROUNDS)))

    print(f"Loading {DATA_PATH}")
    data = HEMSData.model_validate_json(DATA_PATH.read_text())
    print(f"  {len(data.houses)} houses · {data.neighborhood.simulation_hours} hours · "
          f"{sum(len(h.shiftable_loads) for h in data.houses)} loads")
    client = AsyncClient(host=host)

    transcript = await run_negotiation(data=data, client=client, model=model, rounds=rounds)

    TRANSCRIPT_PATH.write_text(transcript.model_dump_json(indent=2))
    SCHEDULE_AFTER_PATH.write_text(json.dumps(transcript.final, indent=2))
    print(f"\nWrote {TRANSCRIPT_PATH}")
    print(f"Wrote {SCHEDULE_AFTER_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
