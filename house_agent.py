"""LLM-driven house agent.

Each HouseAgent represents one house in the neighborhood. Per round, it
gets the coordinator's current price signal and any flagged loads,
prompts the local Ollama model, and returns a structured bid: a
proposed start hour for each of its shiftable loads plus rationale and
a public message.

We defensively strip markdown fences and normalize the response (the
gpt-oss model substitutes near-synonyms in `format=`), then validate
each proposed start against the load's window. Anything that drifts
outside its window gets clamped to the nearest valid start so the
simulator can always score the result.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from ollama import AsyncClient

from agent_schemas import HouseBidResponse, LoadBid
from generate_data import _strip_to_json
from schema import House, HEMSData, ShiftableLoad


SYSTEM_PROMPT = """You are an energy-management agent for one household in \
a 3-house neighborhood. Your job: pick start times for your shiftable loads \
(EV charging, laundry, water heater) that respect each load's window and \
reason, while avoiding hours where the neighborhood is already crowded.

Return ONLY a raw JSON object matching the supplied schema. No markdown \
fences, no prose, no explanation outside the JSON."""


def _format_prices(prices: list[float]) -> str:
    """Show prices as two 24-hour rows (one per simulation day)."""
    lines = []
    for day in range(len(prices) // 24):
        offsets = range(day * 24, (day + 1) * 24)
        header = " ".join(f"{h % 24:>5d}" for h in offsets)
        values = " ".join(f"{prices[h]:>5.2f}" for h in offsets)
        label = "Day 1" if day == 0 else f"Day {day + 1}"
        lines.append(f"  {label} hour-of-day:  {header}")
        lines.append(f"  {label} price:        {values}")
    return "\n".join(lines)


def _hour_offset(when: datetime, sim_start: datetime) -> float:
    return (when - sim_start).total_seconds() / 3600.0


def _load_block(load: ShiftableLoad, sim_start: datetime) -> str:
    earliest = _hour_offset(load.earliest_start, sim_start)
    latest = _hour_offset(load.latest_finish, sim_start)
    valid_start_max = latest - load.duration_hours
    return (
        f"  - id={load.load_id}  type={load.type}  power={load.power_kw}kW  "
        f"duration={load.duration_hours}h\n"
        f"    valid start range (hour offsets): [{earliest:.1f}, {valid_start_max:.1f}]\n"
        f"    reason: \"{load.private_context.reason}\"\n"
        f"    flexibility: {load.private_context.flexibility_score:.1f} "
        f"(0=rigid deadline, 1=freely shiftable)"
    )


def _build_user_prompt(
    house: House,
    sim_start: datetime,
    round_number: int,
    prices: list[float],
    coordinator_message: str,
    flagged_loads: list[str],
) -> str:
    loads_text = "\n".join(_load_block(ld, sim_start) for ld in house.shiftable_loads)
    flagged_for_me = [lid for lid in flagged_loads if lid.startswith(house.house_id + "-")]
    flag_text = (
        f"Coordinator has flagged these of your loads as deadline-critical: "
        f"{flagged_for_me}. Treat them as high-priority."
        if flagged_for_me else
        "Coordinator has not flagged any of your loads this round."
    )

    return f"""Round {round_number} of negotiation.

You are house {house.house_id} ({house.archetype}, {house.occupants} occupants).

YOUR SHIFTABLE LOADS:
{loads_text}

CURRENT HOURLY PRICES (0.0=empty, 1.0=peak congestion across the neighborhood):
{_format_prices(prices)}

COORDINATOR SAYS: {coordinator_message or '(no message yet)'}
{flag_text}

Pick a proposed_start_hour (as a float hour offset from simulation start) for each \
of your loads. Aim to:
  - place each load inside its valid start range
  - prefer hours with LOWER prices (avoid the crowd)
  - respect the load's reason (e.g. EV must be ready for a morning departure)
  - if flexibility is low, stay near a reasonable time for that load's purpose
  - if flagged as deadline-critical, prioritize finishing early

Give a 1-sentence rationale per load and a 1-2 sentence message to the coordinator \
about your situation. Use house_id="{house.house_id}" and the exact load_ids above."""


# gpt-oss substitutes near-synonyms for top-level keys every run
# (load_schedule, proposed_schedule, bids, ...). Rather than maintain
# an alias whitelist, normalize structurally: the array-of-objects is
# the load_bids; any spare string is the message; an obvious house_id
# is the house_id. Inside each load_bid item, alias the common drifts.
_LOAD_BID_FIELD_ALIASES = {
    "load": "load_id",
    "id": "load_id",
    "loadId": "load_id",
    "start_hour": "proposed_start_hour",
    "proposedStart": "proposed_start_hour",
    "proposed_start": "proposed_start_hour",
    "proposed_hour": "proposed_start_hour",
    "start": "proposed_start_hour",
    "hour": "proposed_start_hour",
    "reasoning": "rationale",
    "reason": "rationale",
    "justification": "rationale",
    "explanation": "rationale",
}


def _normalize_bid(obj: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    leftover_strings: list[tuple[str, str]] = []
    array_field: str | None = None

    for key, value in obj.items():
        if key in ("house_id", "houseId", "house"):
            out["house_id"] = value
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            # The list-of-objects is our load_bids.
            if array_field is None:
                out["load_bids"] = value
                array_field = key
            # else: ignore additional lists; we only expect one
        elif isinstance(value, str):
            leftover_strings.append((key, value))
        elif key in ("message", "msg", "summary", "note", "comment"):
            out["message"] = value

    if "message" not in out and leftover_strings:
        # Use the longest leftover string as the message.
        leftover_strings.sort(key=lambda kv: -len(kv[1]))
        out["message"] = leftover_strings[0][1]

    for lb in out.get("load_bids", []):
        for src, dst in _LOAD_BID_FIELD_ALIASES.items():
            if src in lb and dst not in lb:
                lb[dst] = lb.pop(src)

    return out


class HouseAgent:
    def __init__(
        self,
        house: House,
        data: HEMSData,
        client: AsyncClient,
        model: str,
    ):
        self.house = house
        self.data = data
        self.client = client
        self.model = model
        # Pre-compute valid ranges to clamp into if the LLM strays.
        self._valid_ranges: dict[str, tuple[float, float]] = {}
        for ld in house.shiftable_loads:
            earliest = _hour_offset(ld.earliest_start, data.neighborhood.simulation_start)
            latest = _hour_offset(ld.latest_finish, data.neighborhood.simulation_start)
            self._valid_ranges[ld.load_id] = (earliest, latest - ld.duration_hours)

    async def bid(
        self,
        round_number: int,
        prices: list[float],
        coordinator_message: str,
        flagged_loads: list[str],
    ) -> HouseBidResponse:
        prompt = _build_user_prompt(
            house=self.house,
            sim_start=self.data.neighborhood.simulation_start,
            round_number=round_number,
            prices=prices,
            coordinator_message=coordinator_message,
            flagged_loads=flagged_loads,
        )

        response = await self.client.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            format=HouseBidResponse.model_json_schema(),
            options={"temperature": 0.5},
        )
        raw = response.message.content
        cleaned = _strip_to_json(raw)
        obj = _normalize_bid(json.loads(cleaned))
        # Make sure house_id is right even if the model omitted it.
        obj.setdefault("house_id", self.house.house_id)
        bid = HouseBidResponse.model_validate(obj)

        # Clamp any proposed start outside its valid window. Cheap, robust,
        # and prevents one bad LLM output from breaking the whole run.
        clamped: list[LoadBid] = []
        known_ids = {ld.load_id for ld in self.house.shiftable_loads}
        seen: set[str] = set()
        for lb in bid.load_bids:
            if lb.load_id not in known_ids:
                # Skip hallucinated load_ids.
                continue
            lo, hi = self._valid_ranges[lb.load_id]
            start = max(lo, min(hi, lb.proposed_start_hour))
            note = ""
            if abs(start - lb.proposed_start_hour) > 0.01:
                note = f" [clamped from {lb.proposed_start_hour:.2f}]"
            clamped.append(LoadBid(
                load_id=lb.load_id,
                proposed_start_hour=start,
                rationale=lb.rationale + note,
            ))
            seen.add(lb.load_id)
        # If the LLM omitted any of our loads, fall back to default_start.
        for ld in self.house.shiftable_loads:
            if ld.load_id not in seen:
                default_h = _hour_offset(ld.default_start, self.data.neighborhood.simulation_start)
                clamped.append(LoadBid(
                    load_id=ld.load_id,
                    proposed_start_hour=default_h,
                    rationale="(LLM omitted; defaulted to original start)",
                ))

        return HouseBidResponse(
            house_id=self.house.house_id,
            load_bids=clamped,
            message=bid.message,
        )

    def schedule_from_bid(self, bid: HouseBidResponse) -> dict[str, datetime]:
        sim_start = self.data.neighborhood.simulation_start
        return {
            lb.load_id: sim_start + timedelta(hours=lb.proposed_start_hour)
            for lb in bid.load_bids
        }
