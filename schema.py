"""Pydantic models for the HEMS simulation dataset.

Two schemas:
  - HEMSData / Neighborhood / House / ShiftableLoad — the final dataset
    consumed by the simulator and the runtime agents.
  - NeighborhoodDraft / HouseDraft / LoadDraft — the lighter schema we
    ask the LLM to produce. Post-processing fills in IDs, base loads,
    and neighborhood metadata.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, NonNegativeFloat, PositiveFloat

LoadType = Literal["ev_charging", "washer_dryer", "water_heater"]
Archetype = Literal[
    "retired_couple",
    "young_family",
    "wfh_single",
    "dual_income_no_kids",
]


# ---------- Final schema (what the simulator consumes) ----------

class PrivateContext(BaseModel):
    reason: str
    flexibility_score: float = Field(..., ge=0.0, le=1.0)


class ShiftableLoad(BaseModel):
    load_id: str
    type: LoadType
    power_kw: PositiveFloat
    duration_hours: PositiveFloat
    preemptible: bool = False
    earliest_start: datetime
    latest_finish: datetime
    default_start: datetime
    private_context: PrivateContext


class House(BaseModel):
    house_id: str
    archetype: Archetype
    occupants: int = Field(..., ge=1, le=4)
    base_load_kw: list[NonNegativeFloat]
    shiftable_loads: list[ShiftableLoad]


class Neighborhood(BaseModel):
    id: str
    timezone: str
    simulation_start: datetime
    simulation_hours: int = Field(..., gt=0)


class HEMSData(BaseModel):
    neighborhood: Neighborhood
    houses: list[House]


# ---------- LLM draft schema (what we ask Ollama to produce) ----------

class LoadDraft(BaseModel):
    type: LoadType
    power_kw: PositiveFloat
    duration_hours: PositiveFloat
    earliest_start: datetime
    latest_finish: datetime
    default_start: datetime
    reason: str
    flexibility_score: float = Field(..., ge=0.0, le=1.0)


class HouseDraft(BaseModel):
    archetype: Archetype
    occupants: int = Field(..., ge=1, le=4)
    shiftable_loads: list[LoadDraft]


class NeighborhoodDraft(BaseModel):
    houses: list[HouseDraft]
