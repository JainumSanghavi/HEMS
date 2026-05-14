"""Pydantic schemas for agent inputs/outputs and shared negotiation state.

Hour offsets are floats measured from `neighborhood.simulation_start`,
e.g. 18.5 means 6:30pm of the first sim day. We pass numeric hour
offsets to/from the LLM (easier for it to reason about than ISO
timestamps) and convert to datetimes when assembling the final
schedule.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ---------- LLM outputs ----------

class LoadBid(BaseModel):
    """One house's choice for a single load."""
    load_id: str
    proposed_start_hour: float = Field(..., description="Hours offset from simulation_start")
    rationale: str = Field(default="", description="One short sentence justifying the choice")


class HouseBidResponse(BaseModel):
    """A house agent's full response for a round."""
    house_id: str
    load_bids: list[LoadBid]
    message: str = Field(default="", description="1-2 sentence summary the coordinator and neighbors can read")


class CoordinatorMessage(BaseModel):
    """Coordinator's narration + optional flags for the next round."""
    message: str = Field(..., description="1-2 sentence summary of what just happened")
    flagged_loads: list[str] = Field(
        default_factory=list,
        description="load_ids the coordinator considers deadline-critical; houses should treat these as high-priority"
    )


class SwapResponse(BaseModel):
    """Reply when a house agent is asked to accept a proposed swap."""
    accept: bool
    rationale: str = Field(default="")


class SwapEvent(BaseModel):
    """A swap that was actually applied (for the transcript)."""
    load_id: str
    house_id: str
    from_hour: float
    to_hour: float
    accepted: bool
    rationale: str
    reduced_peak_kw: float


# ---------- Shared state and transcript ----------

@dataclass
class RoundRecord:
    round_number: int
    coordinator_message: str
    flagged_loads: list[str]
    prices: list[float]
    house_messages: dict[str, str]
    schedule: dict[str, datetime]
    peak_kw: float
    peak_hour: int


@dataclass
class NegotiationState:
    """Mutable state passed between rounds."""
    round_number: int = 0
    schedule: dict[str, datetime] = field(default_factory=dict)  # load_id -> start
    prices: list[float] = field(default_factory=list)            # length = sim hours
    flagged_loads: list[str] = field(default_factory=list)
    last_coordinator_message: str = ""
    history: list[RoundRecord] = field(default_factory=list)


# ---------- Transcript on disk ----------

class TranscriptRound(BaseModel):
    round_number: int
    coordinator_message: str
    flagged_loads: list[str]
    prices: list[float]
    house_messages: dict[str, str]
    schedule: dict[str, str]   # load_id -> ISO datetime
    peak_kw: float
    peak_hour: int


class Transcript(BaseModel):
    before: dict[str, str]           # default schedule, ISO timestamps
    before_peak_kw: float
    before_peak_hour: int
    before_cost_usd: float = 0.0
    before_co2_kg: float = 0.0
    rounds: list[TranscriptRound]
    swaps: list[SwapEvent] = Field(default_factory=list)
    final: dict[str, str]
    final_peak_kw: float
    final_peak_hour: int
    final_cost_usd: float = 0.0
    final_co2_kg: float = 0.0
    peak_reduction_pct: float
    cost_savings_usd: float = 0.0
    co2_savings_kg: float = 0.0
