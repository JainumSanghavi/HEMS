"""Generate the HEMS simulation dataset via the local Ollama daemon.

Talks to http://localhost:11434. If you use a Cloud-proxied model
(e.g. gpt-oss:120b-cloud), the daemon handles auth — no API key here.

Flow:
  1. Ask Ollama for a `NeighborhoodDraft` with engineered conflict
     (default starts clustered at 6-8pm to make the "before" peak sharp).
  2. Post-process the draft: assign house_ids / load_ids, scale base
     load curves from archetype templates, wrap with neighborhood
     metadata.
  3. Validate the final structure with the full `HEMSData` schema.
  4. Write to data/neighborhood.json.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ollama import Client

from archetypes import generate_base_load
from schema import (
    HEMSData,
    House,
    Neighborhood,
    NeighborhoodDraft,
    PrivateContext,
    ShiftableLoad,
)

# ---------- Config ----------

OUTPUT_PATH = Path(__file__).parent / "data" / "neighborhood.json"

DEFAULT_MODEL = "gpt-oss:120b-cloud"
DEFAULT_HOST = "http://localhost:11434"

NEIGHBORHOOD_ID = "hackathon-demo"
TIMEZONE = "America/New_York"
# Tuesday morning, ET. Two-day sim window.
SIMULATION_START = datetime(2026, 5, 12, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
SIMULATION_HOURS = 48


# ---------- Prompt ----------

SYSTEM_PROMPT = """You generate realistic synthetic data for a 3-house \
neighborhood energy simulation. Return ONLY a raw JSON object matching the \
supplied schema. Do not wrap the output in markdown fences. Do not include \
any prose, explanation, or trailing text. Output the JSON object and nothing \
else."""

USER_PROMPT = f"""Generate 3 houses for a Home Energy Management System \
demo over a {SIMULATION_HOURS}-hour simulation starting \
{SIMULATION_START.isoformat()}.

Each house has shiftable loads (EV charging, washer/dryer, water heater). \
For every load, give:
  - power_kw, duration_hours: realistic values
       * EV charging: 7.2 kW, 3-4 hours duration
       * washer_dryer: 2.5 kW, 1.5-2 hours duration
       * water_heater: 1.5 kW, 1-1.5 hours duration
  - earliest_start, latest_finish: the ISO8601 window the load is allowed \
to run inside
       * EV: 18:00 today through 07:30 tomorrow
       * washer_dryer: 07:00-22:00 same day
       * water_heater: 05:00-22:00 same day
  - default_start: when the load would run UNCOORDINATED (no smart \
scheduling). IMPORTANT: engineer the default_start times to *cluster* \
between 18:00 and 20:30 across all houses so the uncoordinated baseline \
has a sharp coincident peak. The optimizer will spread these out later.
  - reason: 1 short sentence on WHY this load needs to run (e.g. "School \
run at 7:30am tomorrow", "After-dinner laundry pile up", "Evening dishes \
and showers"). Make these distinct and human.
  - flexibility_score: 0.0 (rigid deadline) to 1.0 (freely shiftable)

For each house:
  - Pick a DISTINCT archetype from: retired_couple, young_family, \
wfh_single, dual_income_no_kids
  - Occupants must match the archetype (retired_couple=2, young_family=3 or \
4, wfh_single=1, dual_income_no_kids=2)
  - Each house should have ONE EV load, ONE washer_dryer load, and ONE \
water_heater load — three loads total per house.

Use realistic ISO8601 timestamps with the {TIMEZONE} offset (-04:00). All \
timestamps must fall within the simulation window."""


# ---------- Generation ----------

def _strip_to_json(raw: str) -> str:
    """Some models (including gpt-oss) wrap structured output in ```json fences
    or prose even when `format=` is supplied. Pull out the JSON object."""
    s = raw.strip()
    if s.startswith("```"):
        # drop the opening fence (optionally with language tag) and closing fence
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    # last-resort: slice from first '{' to last '}'
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        s = s[start : end + 1]
    return s.strip()


# Ollama's `format=` is a hint, not a hard constraint, for cloud-proxied
# gpt-oss. The model substitutes near-synonyms for field names and load
# type values. Normalize before validation.
_LOAD_TYPE_ALIASES = {
    "ev": "ev_charging",
    "ev_charging": "ev_charging",
    "ev_charge": "ev_charging",
    "electric_vehicle": "ev_charging",
    "car": "ev_charging",
    "washer": "washer_dryer",
    "dryer": "washer_dryer",
    "washer_dryer": "washer_dryer",
    "laundry": "washer_dryer",
    "water_heater": "water_heater",
    "waterheater": "water_heater",
    "hot_water": "water_heater",
}


def _normalize_draft(obj: dict) -> dict:
    """Coerce common drift back to the draft schema."""
    for house in obj.get("houses", []):
        if "shiftable_loads" not in house and "loads" in house:
            house["shiftable_loads"] = house.pop("loads")
        for load in house.get("shiftable_loads", []):
            t = str(load.get("type", "")).strip().lower()
            if t in _LOAD_TYPE_ALIASES:
                load["type"] = _LOAD_TYPE_ALIASES[t]
    return obj


def call_ollama(model: str, host: str) -> NeighborhoodDraft:
    import json

    client = Client(host=host)
    response = client.chat(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT},
        ],
        format=NeighborhoodDraft.model_json_schema(),
        options={"temperature": 0.4},
    )
    raw = response.message.content
    cleaned = _strip_to_json(raw)
    obj = _normalize_draft(json.loads(cleaned))
    return NeighborhoodDraft.model_validate(obj)


def assemble_full_dataset(draft: NeighborhoodDraft) -> HEMSData:
    if len(draft.houses) != 3:
        raise ValueError(f"expected 3 houses, got {len(draft.houses)}")

    houses: list[House] = []
    for idx, hd in enumerate(draft.houses, start=1):
        house_id = f"H{idx}"
        base_load = generate_base_load(
            archetype=hd.archetype,
            occupants=hd.occupants,
            simulation_hours=SIMULATION_HOURS,
            seed=idx,
        )

        loads: list[ShiftableLoad] = []
        type_counts: dict[str, int] = {}
        for ld in hd.shiftable_loads:
            day_idx = (ld.default_start - SIMULATION_START).days
            type_tag = {
                "ev_charging": "EV",
                "washer_dryer": "LAUNDRY",
                "water_heater": "WH",
            }[ld.type]
            # disambiguate if a house somehow gets two of the same type
            type_counts[type_tag] = type_counts.get(type_tag, 0) + 1
            suffix = "" if type_counts[type_tag] == 1 else f"-{type_counts[type_tag]}"
            load_id = f"{house_id}-{type_tag}-D{day_idx}{suffix}"

            loads.append(ShiftableLoad(
                load_id=load_id,
                type=ld.type,
                power_kw=ld.power_kw,
                duration_hours=ld.duration_hours,
                preemptible=False,
                earliest_start=ld.earliest_start,
                latest_finish=ld.latest_finish,
                default_start=ld.default_start,
                private_context=PrivateContext(
                    reason=ld.reason,
                    flexibility_score=ld.flexibility_score,
                ),
            ))

        houses.append(House(
            house_id=house_id,
            archetype=hd.archetype,
            occupants=hd.occupants,
            base_load_kw=base_load,
            shiftable_loads=loads,
        ))

    return HEMSData(
        neighborhood=Neighborhood(
            id=NEIGHBORHOOD_ID,
            timezone=TIMEZONE,
            simulation_start=SIMULATION_START,
            simulation_hours=SIMULATION_HOURS,
        ),
        houses=houses,
    )


def main() -> int:
    model = os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)
    host = os.environ.get("OLLAMA_HOST", DEFAULT_HOST)

    print(f"Requesting draft from {host} ({model})...")
    draft = call_ollama(model=model, host=host)
    print(f"Got draft with {len(draft.houses)} houses. Assembling full dataset...")

    data = assemble_full_dataset(draft)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(data.model_dump_json(indent=2))
    print(f"Wrote {OUTPUT_PATH}")

    summary_lines = []
    for h in data.houses:
        summary_lines.append(
            f"  {h.house_id}: {h.archetype} ({h.occupants}p) — "
            f"{len(h.shiftable_loads)} loads"
        )
    print("\n".join(summary_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
