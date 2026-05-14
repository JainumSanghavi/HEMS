"""Pydantic models for the HEMS simulation dataset (rich edition).

The dataset is split into two shapes:

  - `HEMSData` / `Neighborhood` / `House` / `ShiftableLoad` / `Solar` /
    `Battery` / `HouseholdPreferences` etc. — the final, validated
    object the simulator and runtime agents consume.

  - `NeighborhoodDraft` / `HouseDraft` / `LoadDraft` — the lighter
    schema we ask Ollama to produce when we want LLM-generated reasons
    and narrative variety. Numeric arrays (base loads, weather, carbon
    intensity, solar irradiance) are deterministic Python work and
    bypass the LLM entirely.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, PositiveFloat

LoadType = Literal["ev_charging", "washer_dryer", "water_heater"]
Archetype = Literal[
    "retired_couple",
    "young_family",
    "wfh_single",
    "dual_income_no_kids",
]


# ---------- per-house systems ----------

class Solar(BaseModel):
    """Rooftop PV. Generation profile is derived from neighborhood
    solar_irradiance × panel_kw × inverter_efficiency."""
    panel_kw: PositiveFloat = Field(..., description="Peak panel capacity (DC)")
    inverter_efficiency: float = Field(0.95, ge=0, le=1)


class Battery(BaseModel):
    """Stationary home battery (e.g. Powerwall)."""
    capacity_kwh: PositiveFloat
    max_charge_kw: PositiveFloat
    max_discharge_kw: PositiveFloat
    initial_soc: float = Field(0.5, ge=0, le=1)
    round_trip_efficiency: float = Field(0.90, ge=0.5, le=1.0)


class HouseholdPreferences(BaseModel):
    """Multi-objective weights the agent uses, plus a personality string
    the LLM reads to flavor decisions. Weights are private — the
    coordinator does not see them; only the household agent does."""
    cost_weight: float = Field(0.4, ge=0, le=1)
    comfort_weight: float = Field(0.4, ge=0, le=1)
    carbon_weight: float = Field(0.15, ge=0, le=1)
    reliability_weight: float = Field(0.05, ge=0, le=1)
    personality: str = Field(..., description="Short personality string the LLM uses, e.g. 'frugal retired engineer'")


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


# ---------- house ----------

class House(BaseModel):
    house_id: str
    archetype: Archetype
    occupants: int = Field(..., ge=1, le=4)
    base_load_kw: list[NonNegativeFloat]
    shiftable_loads: list[ShiftableLoad]
    solar: Optional[Solar] = None
    battery: Optional[Battery] = None
    preferences: HouseholdPreferences


# ---------- neighborhood-level signals ----------

class Tariff(BaseModel):
    """Simple time-of-use tariff. Hours-of-day in `peak_hours` get the
    peak rate; everything else gets off-peak."""
    peak_hours: list[int]  # 0..23
    peak_rate_per_kwh: PositiveFloat
    offpeak_rate_per_kwh: PositiveFloat
    export_credit_per_kwh: NonNegativeFloat = 0.0


class DREvent(BaseModel):
    """Utility demand-response event: 'reduce neighborhood draw during
    these hours and we'll pay an incentive per kWh saved.'"""
    start_hour: int  # offset from simulation_start
    duration_hours: int
    target_reduction_kw: NonNegativeFloat
    incentive_per_kwh: NonNegativeFloat


class Neighborhood(BaseModel):
    id: str
    timezone: str
    simulation_start: datetime
    simulation_hours: int = Field(..., gt=0)

    # Per-hour exogenous signals. Each list length must equal simulation_hours.
    weather_temp_f: list[float] = Field(default_factory=list)
    cloud_cover_pct: list[float] = Field(default_factory=list)
    solar_irradiance_wm2: list[NonNegativeFloat] = Field(default_factory=list)
    carbon_intensity_gco2_per_kwh: list[NonNegativeFloat] = Field(default_factory=list)

    tariff: Tariff
    dr_events: list[DREvent] = Field(default_factory=list)


# ---------- top-level ----------

class HEMSData(BaseModel):
    model_config = ConfigDict(extra="ignore")
    neighborhood: Neighborhood
    houses: list[House]


# ---------- LLM draft schemas (kept for the narrative LLM pass) ----------

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
