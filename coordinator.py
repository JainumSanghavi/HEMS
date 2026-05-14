"""LLM-narrated coordinator.

The coordinator does two jobs each round:

  1. Pure math: aggregate house bids into a schedule, run the
     simulator, compute new shadow prices (min-max normalize the
     aggregate kW curve to [0, 1]).
  2. LLM narration: explain what happened, optionally flag loads it
     considers deadline-critical so houses prioritize them next round.

The math is the load-bearing optimization signal. The narration is the
multi-agent story that makes the demo legible.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from ollama import AsyncClient

from agent_schemas import CoordinatorMessage, HouseBidResponse
from generate_data import _strip_to_json
from schema import HEMSData
from simulator import CurveResult, compute_curve


SYSTEM_PROMPT = """You are the neighborhood energy coordinator. You see all \
load bids from 3 houses, the resulting aggregate kW curve, and your \
just-computed shadow prices. Your job is to narrate what just happened in \
plain language and optionally flag loads as deadline-critical so the \
houses prioritize them next round.

Return ONLY a raw JSON object matching the supplied schema. No markdown \
fences, no prose, no explanation outside the JSON."""


def normalize_to_prices(curve: list[float]) -> list[float]:
    lo = min(curve)
    hi = max(curve)
    span = hi - lo
    if span < 1e-9:
        return [0.0] * len(curve)
    return [round((v - lo) / span, 3) for v in curve]


def _format_grid(values: list[float], label: str) -> str:
    lines = []
    for day in range(len(values) // 24):
        offsets = range(day * 24, (day + 1) * 24)
        header = " ".join(f"{h % 24:>5d}" for h in offsets)
        row = " ".join(f"{values[h]:>5.2f}" for h in offsets)
        d_label = f"Day {day + 1}"
        lines.append(f"  {d_label} hour-of-day: {header}")
        lines.append(f"  {d_label} {label}:    {row}")
    return "\n".join(lines)


# Structural normalizer for CoordinatorMessage drift.
def _normalize_coord(obj: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    leftover_strings: list[tuple[str, str]] = []
    for key, value in obj.items():
        if isinstance(value, list) and (not value or isinstance(value[0], str)):
            if "flagged_loads" not in out:
                out["flagged_loads"] = list(value)
        elif isinstance(value, str):
            leftover_strings.append((key, value))
        # ignore other shapes
    if leftover_strings:
        # Prefer a key that looks like a message; else longest string.
        for k, v in leftover_strings:
            if k.lower() in ("message", "msg", "narration", "summary", "explanation"):
                out["message"] = v
                break
        if "message" not in out:
            leftover_strings.sort(key=lambda kv: -len(kv[1]))
            out["message"] = leftover_strings[0][1]
    out.setdefault("flagged_loads", [])
    return out


class CoordinatorAgent:
    def __init__(self, data: HEMSData, client: AsyncClient, model: str):
        self.data = data
        self.client = client
        self.model = model
        # Map load_id -> reason (so we can show context in the prompt).
        self._reasons: dict[str, str] = {}
        for h in data.houses:
            for ld in h.shiftable_loads:
                self._reasons[ld.load_id] = ld.private_context.reason

    def integrate(self, bids: list[HouseBidResponse]) -> tuple[dict[str, datetime], CurveResult, list[float]]:
        """Combine bids -> schedule -> curve -> new prices."""
        sim_start = self.data.neighborhood.simulation_start
        schedule: dict[str, datetime] = {}
        for bid in bids:
            for lb in bid.load_bids:
                schedule[lb.load_id] = sim_start + timedelta(hours=lb.proposed_start_hour)
        curve = compute_curve(self.data, schedule)
        prices = normalize_to_prices(curve.total_kw)
        return schedule, curve, prices

    async def narrate(
        self,
        round_number: int,
        schedule: dict[str, datetime],
        curve: CurveResult,
        prices: list[float],
        bids: list[HouseBidResponse],
    ) -> CoordinatorMessage:
        sim_start = self.data.neighborhood.simulation_start

        schedule_lines = []
        for load_id in sorted(schedule):
            start = schedule[load_id]
            hr = (start - sim_start).total_seconds() / 3600
            reason = self._reasons.get(load_id, "")
            schedule_lines.append(f"  {load_id}: start hour {hr:.2f}  (reason: \"{reason}\")")

        house_msg_lines = []
        for bid in bids:
            house_msg_lines.append(f"  {bid.house_id}: \"{bid.message}\"")

        peak_hour = curve.peak_hour
        peak_when = sim_start + timedelta(hours=peak_hour)

        user_prompt = f"""Round {round_number} of negotiation just finished.

PROPOSED SCHEDULE FROM ALL HOUSES:
{chr(10).join(schedule_lines)}

RESULTING LOAD CURVE (kW per hour):
{_format_grid(curve.total_kw, "kW   ")}

NEW SHADOW PRICES (0=empty hour, 1=worst peak):
{_format_grid(prices, "price")}

PEAK: {curve.peak_kw:.1f} kW at hour {peak_hour} ({peak_when.strftime('%a %H:%M')}).
PEAK-TO-AVG RATIO: {curve.peak_to_avg:.2f}

HOUSES SAID:
{chr(10).join(house_msg_lines)}

Provide:
  1. A 1-2 sentence "message" describing what happened (where the peak is,
     which loads contributed, what should change next round).
  2. A "flagged_loads" list of load_ids whose reasons suggest a hard deadline
     (e.g. early morning departures, school runs) so houses prioritize them
     next round. Leave empty if nothing is critical."""

        response = await self.client.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            format=CoordinatorMessage.model_json_schema(),
            options={"temperature": 0.4},
        )
        cleaned = _strip_to_json(response.message.content)
        obj = _normalize_coord(json.loads(cleaned))
        cm = CoordinatorMessage.model_validate(obj)
        # Filter flagged loads to ones that actually exist
        valid_ids = set(self._reasons.keys())
        cm.flagged_loads = [lid for lid in cm.flagged_loads if lid in valid_ids]
        return cm
