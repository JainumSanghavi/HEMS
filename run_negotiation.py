"""Orchestrate the full HEMS negotiation end-to-end.

Loads data/neighborhood.json, instantiates 3 house agents + 1
coordinator, runs N rounds (default 3) with houses bidding in parallel,
and writes:

  - data/transcript.json — full negotiation log (round-by-round
    messages, prices, schedules, peaks). Drives the demo UI.
  - data/schedule_after.json — final agent-decided schedule.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from ollama import AsyncClient

from agent_schemas import Transcript, TranscriptRound
from coordinator import CoordinatorAgent
from house_agent import HouseAgent
from schema import HEMSData
from simulator import compute_curve, default_schedule


DATA_PATH = Path(os.environ.get("HEMS_DATA", str(Path(__file__).parent / "data" / "neighborhood.json")))
TRANSCRIPT_PATH = Path(os.environ.get("HEMS_TRANSCRIPT", str(Path(__file__).parent / "data" / "transcript.json")))
SCHEDULE_AFTER_PATH = Path(os.environ.get("HEMS_SCHEDULE", str(Path(__file__).parent / "data" / "schedule_after.json")))

DEFAULT_MODEL = "gpt-oss:120b-cloud"
DEFAULT_HOST = "http://localhost:11434"
ROUNDS = 3


def _iso_schedule(schedule: dict[str, datetime]) -> dict[str, str]:
    return {k: v.isoformat() for k, v in schedule.items()}


async def run_negotiation(data: HEMSData, client: AsyncClient, model: str, rounds: int) -> Transcript:
    sim_start = data.neighborhood.simulation_start

    houses = [HouseAgent(house=h, data=data, client=client, model=model) for h in data.houses]
    coord = CoordinatorAgent(data=data, client=client, model=model)

    # --- Before baseline ---
    before = default_schedule(data)
    before_curve = compute_curve(data, before)
    print(f"BEFORE: peak={before_curve.peak_kw:.2f} kW at hour {before_curve.peak_hour}, "
          f"peak/avg={before_curve.peak_to_avg:.2f}")

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

        print(f"  peak={curve.peak_kw:.2f} kW @ hour {curve.peak_hour}, peak/avg={curve.peak_to_avg:.2f}")
        print(f"  coord: {cm.message}")
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

    final_curve = compute_curve(data, final_schedule)
    reduction = (before_curve.peak_kw - final_curve.peak_kw) / before_curve.peak_kw * 100
    print(f"\nAFTER: peak={final_curve.peak_kw:.2f} kW at hour {final_curve.peak_hour}, "
          f"peak/avg={final_curve.peak_to_avg:.2f}")
    print(f"PEAK REDUCTION: {reduction:.1f}%")

    return Transcript(
        before=_iso_schedule(before),
        before_peak_kw=before_curve.peak_kw,
        before_peak_hour=before_curve.peak_hour,
        rounds=rounds_log,
        final=_iso_schedule(final_schedule),
        final_peak_kw=final_curve.peak_kw,
        final_peak_hour=final_curve.peak_hour,
        peak_reduction_pct=reduction,
    )


async def main() -> int:
    model = os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)
    host = os.environ.get("OLLAMA_HOST", DEFAULT_HOST)
    rounds = int(os.environ.get("HEMS_ROUNDS", str(ROUNDS)))

    data = HEMSData.model_validate_json(DATA_PATH.read_text())
    client = AsyncClient(host=host)

    transcript = await run_negotiation(data=data, client=client, model=model, rounds=rounds)

    TRANSCRIPT_PATH.write_text(transcript.model_dump_json(indent=2))
    SCHEDULE_AFTER_PATH.write_text(json.dumps(transcript.final, indent=2))
    print(f"\nWrote {TRANSCRIPT_PATH}")
    print(f"Wrote {SCHEDULE_AFTER_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
