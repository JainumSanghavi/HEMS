"""LLM-driven house agent (rich edition).

Each HouseAgent represents one house in the neighborhood. It now
reasons over a much richer context: its own personality + preference
weights (cost / comfort / carbon / reliability), its solar generation
forecast (if any), its battery state and capacity (if any), the
neighborhood's time-of-use tariff, the per-hour grid carbon intensity,
and the coordinator's congestion-price signal. The output schema is
unchanged (a HouseBidResponse with a load_bids list and a message), so
the orchestrator and simulator do not need to change shape.

Defensive parsing is preserved end-to-end: markdown fences stripped,
top-level field names normalized structurally, proposed start hours
clamped into each load's window, omitted loads filled with the
default_start so the simulator always has a complete schedule.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from ollama import AsyncClient

from agent_schemas import HouseBidResponse, LoadBid
from generate_data import _strip_to_json
from schema import HEMSData, House, ShiftableLoad


SYSTEM_PROMPT = """You are an energy-management agent for one household \
in a 3-house neighborhood. You have a personality and private preference \
weights (cost vs. comfort vs. carbon vs. reliability). You pick start times \
for your shiftable loads (EV charging, laundry, water heater) that respect \
each load's window and stated reason while reflecting your household's \
priorities and avoiding hours where the neighborhood is already crowded.

You may have rooftop solar (positive generation during the day) and a home \
battery (charges from solar surplus, discharges during peak tariff hours \
automatically). Take these into account: prefer running loads during your \
solar window if your priority is cost or carbon; prefer off-peak tariff \
hours otherwise.

Return ONLY a raw JSON object matching the supplied schema. No markdown \
fences, no prose, no explanation outside the JSON."""


# -------- formatting helpers ------------------------------------------------

def _hour_offset(when: datetime, sim_start: datetime) -> float:
    return (when - sim_start).total_seconds() / 3600.0


def _format_grid(values: list[float], label: str, decimals: int = 2) -> str:
    """Show a 240-element series as one row of 24 hour labels per day."""
    fmt = f"{{:>5.{decimals}f}}"
    lines = []
    n_days = len(values) // 24
    for day in range(n_days):
        offsets = range(day * 24, (day + 1) * 24)
        if day == 0:
            header = " ".join(f"{h % 24:>5d}" for h in offsets)
            lines.append(f"  hr-of-day:  {header}")
        row = " ".join(fmt.format(values[h]) for h in offsets)
        lines.append(f"  D{day+1} {label:7s}{row}")
    return "\n".join(lines)


def _format_prefs(p) -> str:
    """A compact pref line — agents read this and shift behavior accordingly."""
    return (
        f"cost {p.cost_weight:.0%} · comfort {p.comfort_weight:.0%} · "
        f"carbon {p.carbon_weight:.0%} · reliability {p.reliability_weight:.0%}"
    )


def _solar_summary(house: House, irradiance: list[float]) -> str:
    """One line per day giving the agent a sense of when the sun is out."""
    if house.solar is None:
        return "  (no solar — your house has no rooftop PV)"
    panel_kw = house.solar.panel_kw
    n_days = len(irradiance) // 24
    lines = ["  (estimated kWh of solar generation per day, peak window 09–17)"]
    for day in range(n_days):
        gen = sum(irradiance[day * 24 + h] for h in range(24)) * panel_kw / 1000 * 0.95
        # name the day
        lines.append(f"  D{day+1}: {gen:5.1f} kWh of solar (peak generation typically 11–14)")
    return "\n".join(lines)


def _battery_summary(house: House) -> str:
    if house.battery is None:
        return "  (no battery — your house has no home storage)"
    b = house.battery
    return (
        f"  capacity {b.capacity_kwh:.1f} kWh, ±{b.max_charge_kw:.1f} kW. Starts at "
        f"{int(b.initial_soc*100)}% SOC. Dispatches automatically: charges from solar surplus, "
        f"discharges during peak tariff hours when above 30% SOC."
    )


def _tariff_summary(tariff) -> str:
    return (
        f"  peak hours of day: {tariff.peak_hours}\n"
        f"  peak rate: ${tariff.peak_rate_per_kwh:.2f}/kWh · off-peak: ${tariff.offpeak_rate_per_kwh:.2f}/kWh · "
        f"export credit: ${tariff.export_credit_per_kwh:.2f}/kWh"
    )


def _carbon_summary(carbon: list[float]) -> str:
    """Categorical view of carbon by hour-of-day, averaged across days."""
    if not carbon:
        return "  (no carbon data)"
    by_hour = [0.0] * 24
    counts = [0] * 24
    for h in range(len(carbon)):
        by_hour[h % 24] += carbon[h]
        counts[h % 24] += 1
    avg = [by_hour[h] / counts[h] for h in range(24)]
    lo = min(avg); hi = max(avg)
    classify = lambda v: "LOW " if v < lo + (hi - lo) * 0.33 else ("HI  " if v > lo + (hi - lo) * 0.66 else "MED ")
    line1 = " ".join(f"{h:>4d}" for h in range(24))
    line2 = " ".join(classify(avg[h]).rstrip() + " " for h in range(24))
    return f"  hour-of-day: {line1}\n  carbon tier: {line2}"


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


# -------- prompt assembly ---------------------------------------------------

def _build_user_prompt(
    house: House,
    data: HEMSData,
    round_number: int,
    prices: list[float],
    coordinator_message: str,
    flagged_loads: list[str],
) -> str:
    sim_start = data.neighborhood.simulation_start
    loads_text = "\n".join(_load_block(ld, sim_start) for ld in house.shiftable_loads)
    flagged_for_me = [lid for lid in flagged_loads if lid.startswith(house.house_id + "-")]
    flag_text = (
        f"Coordinator has flagged these of your loads as deadline-critical: "
        f"{flagged_for_me}. Treat them as high-priority and try to finish early."
        if flagged_for_me else
        "Coordinator has not flagged any of your loads this round."
    )

    return f"""Round {round_number} of negotiation.

YOU
  house: {house.house_id} ({house.archetype}, {house.occupants} occupants)
  personality: "{house.preferences.personality}"
  private preferences: {_format_prefs(house.preferences)}

YOUR SYSTEMS
  solar:
{_solar_summary(house, data.neighborhood.solar_irradiance_wm2)}
  battery:
{_battery_summary(house)}

NEIGHBORHOOD TARIFF (same for everyone)
{_tariff_summary(data.neighborhood.tariff)}

NEIGHBORHOOD GRID CARBON INTENSITY (averaged across days)
{_carbon_summary(data.neighborhood.carbon_intensity_gco2_per_kwh)}

CURRENT CONGESTION PRICES (this round, 0.0=empty, 1.0=peak crowd):
{_format_grid(prices, "price ")}

COORDINATOR SAYS: {coordinator_message or '(no message yet)'}
{flag_text}

YOUR SHIFTABLE LOADS:
{loads_text}

For each load, pick a proposed_start_hour (a float hour offset from \
simulation start). Reflect your private preferences:
  - If your cost_weight is high, prefer off-peak hours and use solar if you have it.
  - If your carbon_weight is high, prefer LOW-carbon hours.
  - If your comfort_weight is high, keep loads near socially natural times.
  - If your reliability_weight is high, finish loads well before their deadlines.

Stay inside each load's valid start range. Give a 1-sentence rationale per \
load. Add a 1-2 sentence message addressed to the coordinator. Use \
house_id="{house.house_id}" and the exact load_ids shown above."""


# -------- response normalization (preserved from prior version) -------------

_LOAD_BID_FIELD_ALIASES = {
    "load": "load_id", "id": "load_id", "loadId": "load_id",
    "start_hour": "proposed_start_hour", "proposedStart": "proposed_start_hour",
    "proposed_start": "proposed_start_hour", "proposed_hour": "proposed_start_hour",
    "start": "proposed_start_hour", "hour": "proposed_start_hour",
    "reasoning": "rationale", "reason": "rationale",
    "justification": "rationale", "explanation": "rationale",
}


def _normalize_bid(obj: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    leftover_strings: list[tuple[str, str]] = []
    array_field = None
    for key, value in obj.items():
        if key in ("house_id", "houseId", "house"):
            out["house_id"] = value
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            if array_field is None:
                out["load_bids"] = value
                array_field = key
        elif isinstance(value, str):
            leftover_strings.append((key, value))
        elif key in ("message", "msg", "summary", "note", "comment"):
            out["message"] = value
    if "message" not in out and leftover_strings:
        leftover_strings.sort(key=lambda kv: -len(kv[1]))
        out["message"] = leftover_strings[0][1]
    for lb in out.get("load_bids", []):
        for src, dst in _LOAD_BID_FIELD_ALIASES.items():
            if src in lb and dst not in lb:
                lb[dst] = lb.pop(src)
    return out


# -------- HouseAgent --------------------------------------------------------

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
        import asyncio
        prompt = _build_user_prompt(
            house=self.house,
            data=self.data,
            round_number=round_number,
            prices=prices,
            coordinator_message=coordinator_message,
            flagged_loads=flagged_loads,
        )

        last_err: Exception | None = None
        response = None
        for attempt in range(3):
            try:
                response = await self.client.chat(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    format=HouseBidResponse.model_json_schema(),
                    options={"temperature": 0.5},
                )
                break
            except Exception as e:
                last_err = e
                await asyncio.sleep(2.0 * (attempt + 1))
        if response is None:
            raise RuntimeError(f"Ollama chat failed after retries: {last_err!r}")
        raw = response.message.content
        cleaned = _strip_to_json(raw)
        obj = _normalize_bid(json.loads(cleaned))
        obj.setdefault("house_id", self.house.house_id)
        bid = HouseBidResponse.model_validate(obj)

        clamped: list[LoadBid] = []
        known_ids = {ld.load_id for ld in self.house.shiftable_loads}
        seen: set[str] = set()
        for lb in bid.load_bids:
            if lb.load_id not in known_ids:
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

    async def evaluate_swap(self, my_load_id: str, proposed_new_start: float, reason: str) -> tuple[bool, str]:
        """Bilateral swap phase: a peer (or coordinator) is asking whether
        the household would accept moving a specific load to a new hour.
        Returns (accept: bool, rationale: str)."""
        import asyncio
        from agent_schemas import SwapResponse  # local import to avoid cycles

        target = next(ld for ld in self.house.shiftable_loads if ld.load_id == my_load_id)
        sim_start = self.data.neighborhood.simulation_start
        earliest = _hour_offset(target.earliest_start, sim_start)
        latest = _hour_offset(target.latest_finish, sim_start) - target.duration_hours

        if proposed_new_start < earliest or proposed_new_start > latest:
            return False, f"refusal: new start {proposed_new_start:.2f} is outside the load's window."

        prompt = f"""A swap is being proposed.

You are house {self.house.house_id} ({self.house.archetype}).
Your personality: "{self.house.preferences.personality}"
Your private preferences: {_format_prefs(self.house.preferences)}

Your load {my_load_id} ({target.type}, {target.power_kw}kW, {target.duration_hours}h):
  current reason: "{target.private_context.reason}"
  flexibility:    {target.private_context.flexibility_score}

Proposal: move start from your current time to hour {proposed_new_start:.2f}. \
Justification offered: "{reason}"

Tariff peak hours: {self.data.neighborhood.tariff.peak_hours}. \
Peak rate ${self.data.neighborhood.tariff.peak_rate_per_kwh:.2f}/kWh, \
off-peak ${self.data.neighborhood.tariff.offpeak_rate_per_kwh:.2f}/kWh.

Decide whether to ACCEPT or REFUSE the move, weighing your private preferences. \
Cost-driven households accept moves to off-peak hours. Carbon-driven households \
accept moves to lower-carbon hours. Comfort-driven households are cautious about \
unfamiliar slots. Be honest — refuse if it conflicts with your values."""

        # Retry on transient cloud errors (502, connection reset, etc.).
        last_err: Exception | None = None
        response = None
        for attempt in range(3):
            try:
                response = await self.client.chat(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": "You evaluate a swap proposal. Return ONLY a JSON object."},
                        {"role": "user", "content": prompt},
                    ],
                    format=SwapResponse.model_json_schema(),
                    options={"temperature": 0.3},
                )
                break
            except Exception as e:
                last_err = e
                await asyncio.sleep(1.5 * (attempt + 1))
        if response is None:
            return False, f"declined (network: {type(last_err).__name__})"

        cleaned = _strip_to_json(response.message.content)
        obj = json.loads(cleaned)
        # normalize accept field
        accept = bool(obj.get("accept", obj.get("accepted", obj.get("approve", False))))
        rationale = (
            obj.get("rationale") or obj.get("reason") or obj.get("explanation") or
            obj.get("message") or ("agreed" if accept else "declined")
        )
        return accept, rationale

    def schedule_from_bid(self, bid: HouseBidResponse) -> dict[str, datetime]:
        sim_start = self.data.neighborhood.simulation_start
        return {
            lb.load_id: sim_start + timedelta(hours=lb.proposed_start_hour)
            for lb in bid.load_bids
        }
