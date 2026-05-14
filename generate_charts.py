"""Render static PNG charts for the README from the rich transcript.

Outputs three PNGs into docs/:
  1. hero_curve.png       — aggregate net grid demand, before vs after,
                            with solar overlay and peak markers.
  2. headlines.png        — 3 stat blocks: peak / cost / CO2 reductions.
  3. agents_diverged.png  — small-multiples showing each house picked
                            different times based on its own priorities.

Style matches the UI: deep ink background, Fraunces-tone serif heads,
coral "before" / mint "after" / amber solar.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.patches import FancyBboxPatch

from schema import HEMSData
from simulator import compute_curve


DATA_PATH = Path("data/neighborhood_rich.json")
TRANSCRIPT_PATH = Path("data/transcript_rich.json")
OUT = Path("docs")
OUT.mkdir(exist_ok=True)


# ---- palette ----
INK         = "#0a0d14"
SURFACE     = "#11151f"
PAPER       = "#f5efe1"
PAPER_DIM   = "#c9c3b0"
PAPER_MUTE  = "#7a7568"
RULE        = "#2a3142"
BEFORE      = "#ff7e6b"
AFTER       = "#6be9a8"
AMBER       = "#ff9a3c"
LAVENDER    = "#b39ddb"
SCARLET     = "#e8454c"

plt.rcParams.update({
    "figure.facecolor":  INK,
    "axes.facecolor":    SURFACE,
    "axes.edgecolor":    RULE,
    "axes.labelcolor":   PAPER_DIM,
    "axes.titlecolor":   PAPER,
    "xtick.color":       PAPER_MUTE,
    "ytick.color":       PAPER_MUTE,
    "text.color":        PAPER,
    "font.family":       ["serif"],
    "font.serif":        ["Fraunces", "Iowan Old Style", "Georgia", "DejaVu Serif"],
    "axes.spines.top":    False,
    "axes.spines.right":  False,
    "axes.grid":         True,
    "grid.color":        "#1a1f2c",
    "grid.linewidth":    0.6,
    "savefig.facecolor": INK,
    "savefig.dpi":       180,
})

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _load():
    data = HEMSData.model_validate_json(DATA_PATH.read_text())
    transcript = json.loads(TRANSCRIPT_PATH.read_text())
    return data, transcript


def _curve_from_schedule(data: HEMSData, sched_iso: dict[str, str]):
    sched = {k: datetime.fromisoformat(v) for k, v in sched_iso.items()}
    return compute_curve(data, sched)


# ============================================================
# Chart 1 — hero (before vs after)
# ============================================================

def chart_hero(data: HEMSData, transcript: dict) -> None:
    H = data.neighborhood.simulation_hours
    sim_start = data.neighborhood.simulation_start

    before = _curve_from_schedule(data, transcript["before"])
    after  = _curve_from_schedule(data, transcript["final"])

    hours = list(range(H))
    # aggregate solar across all houses
    solar = [sum(after.per_house[h].solar_kw[i] for h in after.per_house) for i in hours]

    fig, ax = plt.subplots(figsize=(14, 6.2))
    fig.subplots_adjust(left=0.06, right=0.97, top=0.86, bottom=0.16)

    # solar mirrored under x-axis (negative axis area) -- shows it offsetting demand
    ax.fill_between(hours, [-s for s in solar], 0,
                    color=AMBER, alpha=0.22, linewidth=0, zorder=1)

    # DR event bands
    for ev in data.neighborhood.dr_events:
        ax.axvspan(ev.start_hour, ev.start_hour + ev.duration_hours,
                   color=SCARLET, alpha=0.10, zorder=0)
        ax.text(ev.start_hour + ev.duration_hours / 2,
                ax.get_ylim()[1] if ax.get_ylim()[1] > 0 else 30,
                "DR EVENT",
                color=SCARLET, fontsize=9, ha="center", va="top",
                family="monospace")

    # before line
    ax.plot(hours, before.net_grid_kw,
            color=BEFORE, linewidth=1.5, linestyle=(0, (4, 3)),
            label="Before — uncoordinated", zorder=3, alpha=0.85)
    # after line
    ax.plot(hours, after.net_grid_kw,
            color=AFTER, linewidth=2.5,
            label="After — coordinated", zorder=4)

    # peak markers
    ax.scatter([before.peak_hour], [before.peak_kw],
               color=BEFORE, s=80, zorder=5, edgecolor=INK, linewidth=2)
    ax.annotate(f"  Peak {before.peak_kw:.1f} kW",
                (before.peak_hour, before.peak_kw),
                color=BEFORE, fontsize=11, fontstyle="italic",
                xytext=(8, 8), textcoords="offset points")
    ax.scatter([after.peak_hour], [after.peak_kw],
               color=AFTER, s=80, zorder=5, edgecolor=INK, linewidth=2)
    ax.annotate(f"  Peak {after.peak_kw:.1f} kW",
                (after.peak_hour, after.peak_kw),
                color=AFTER, fontsize=11, fontstyle="italic",
                xytext=(8, 8), textcoords="offset points")

    # day-of-week x-axis labels
    day_starts = list(range(0, H, 24))
    day_labels = []
    for h in day_starts:
        d = sim_start + timedelta(hours=h)
        day_labels.append(DAY_NAMES[(d.weekday()) % 7])
    ax.set_xticks([h + 12 for h in day_starts])
    ax.set_xticklabels(day_labels, fontsize=10, family="monospace")
    ax.set_xlim(0, H)
    ax.set_ylim(min(-max(solar) * 1.05, -1),
                max(before.peak_kw, after.peak_kw) * 1.18)

    # day gridlines
    for h in day_starts[1:]:
        ax.axvline(h, color=RULE, linewidth=0.7, zorder=0)

    ax.set_ylabel("Net grid demand   (kW)", fontsize=11, color=PAPER_DIM,
                  fontstyle="italic")
    ax.tick_params(axis="y", labelsize=10)
    ax.set_title("A neighborhood negotiates its load",
                 fontsize=22, color=PAPER, pad=18, loc="left",
                 fontstyle="italic", weight="light")
    fig.text(0.06, 0.92,
             f"AGGREGATE NET GRID DEMAND  ·  240 HOURS  ·  REDUCTION  {transcript['peak_reduction_pct']:.1f}%",
             fontsize=9.5, color=PAPER_MUTE, family="monospace")

    # legend
    leg = ax.legend(loc="upper right", frameon=False, fontsize=10,
                    labelcolor=PAPER_DIM)
    # solar legend manually
    solar_patch = patches.Patch(color=AMBER, alpha=0.45, label="Solar generation (offset, mirrored)")
    handles = leg.legend_handles + [solar_patch]
    ax.legend(handles=handles, loc="upper right", frameon=False, fontsize=10,
              labelcolor=PAPER_DIM)

    # footnote
    fig.text(0.06, 0.04,
             "Three households · 10 days · same routines, same total energy delivered. Solar offset shown mirrored below the x-axis.",
             fontsize=9, color=PAPER_MUTE, fontstyle="italic")

    fig.savefig(OUT / "hero_curve.png")
    plt.close(fig)
    print(f"  wrote {OUT / 'hero_curve.png'}")


# ============================================================
# Chart 2 — three headline stats
# ============================================================

def chart_headlines(transcript: dict) -> None:
    peak_before = transcript["before_peak_kw"]
    peak_after  = transcript["final_peak_kw"]
    peak_pct    = transcript["peak_reduction_pct"]
    cost_before = transcript["before_cost_usd"]
    cost_after  = transcript["final_cost_usd"]
    cost_saved  = transcript["cost_savings_usd"]
    co2_before  = transcript["before_co2_kg"]
    co2_after   = transcript["final_co2_kg"]
    co2_saved   = transcript["co2_savings_kg"]

    cost_pct = (cost_saved / cost_before * 100) if cost_before else 0
    co2_pct  = (co2_saved / co2_before * 100)   if co2_before else 0

    fig, axes = plt.subplots(1, 3, figsize=(14, 4.6))
    fig.subplots_adjust(left=0.04, right=0.96, top=0.78, bottom=0.10, wspace=0.18)

    def stat_panel(ax, label, before_val, after_val, pct, unit, accent):
        ax.set_facecolor(SURFACE)
        ax.set_xticks([]); ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_visible(False)
        # 3px top rule in accent
        ax.add_patch(patches.Rectangle((0, 0.97), 1, 0.03,
                                       transform=ax.transAxes,
                                       color=accent, clip_on=False))
        ax.text(0.05, 0.83, label.upper(),
                transform=ax.transAxes, fontsize=10, color=PAPER_MUTE,
                family="monospace")
        ax.text(0.05, 0.50, f"{pct:.1f}%",
                transform=ax.transAxes, fontsize=64, color=PAPER,
                fontstyle="italic", weight="light", va="center")
        ax.text(0.05, 0.30, "REDUCTION",
                transform=ax.transAxes, fontsize=9, color=PAPER_MUTE,
                family="monospace")
        ax.text(0.05, 0.13,
                f"{_fmt(before_val)} {unit}  to  {_fmt(after_val)} {unit}",
                transform=ax.transAxes, fontsize=12, color=accent,
                fontstyle="italic")

    def _fmt(v):
        if isinstance(v, float):
            return f"{v:.2f}" if v < 100 else f"{v:.1f}"
        return str(v)

    stat_panel(axes[0], "Peak demand", peak_before, peak_after, peak_pct, "kW", BEFORE)
    stat_panel(axes[1], "Electricity cost (10 d)", cost_before, cost_after, cost_pct, "USD", AMBER)
    stat_panel(axes[2], "Grid CO₂ (10 d)",   co2_before, co2_after, co2_pct, "kg", AFTER)

    fig.text(0.04, 0.9, "HEADLINE RESULTS  ·  THE SAME ROUTINES, COORDINATED",
             fontsize=10, color=PAPER_MUTE, family="monospace")

    fig.savefig(OUT / "headlines.png")
    plt.close(fig)
    print(f"  wrote {OUT / 'headlines.png'}")


# ============================================================
# Chart 3 — three agents diverged
# ============================================================

def chart_agents_diverged(data: HEMSData, transcript: dict) -> None:
    """Show where each house's EV loads ended up, before vs after,
    overlaid by hour-of-day. Demonstrates that agents made different
    choices based on their private priorities."""
    fig, axes = plt.subplots(3, 1, figsize=(14, 5.4), sharex=True)
    fig.subplots_adjust(left=0.08, right=0.97, top=0.84, bottom=0.13, hspace=0.45)

    sim_start = data.neighborhood.simulation_start
    H = data.neighborhood.simulation_hours

    house_titles = {
        "H1": "H1 · Retired couple   —   priorities: cost-dominated",
        "H2": "H2 · Young family     —   priorities: comfort-dominated",
        "H3": "H3 · WFH single       —   priorities: carbon-dominated",
    }
    accents = {"H1": BEFORE, "H2": AFTER, "H3": LAVENDER}

    for ax, house in zip(axes, data.houses):
        ax.set_facecolor(SURFACE)
        ax.set_yticks([])
        ax.set_xlim(0, H)
        for spine in ("top", "right", "left"):
            ax.spines[spine].set_visible(False)
        ax.spines["bottom"].set_color(RULE)

        before_sched = transcript["before"]
        after_sched  = transcript["final"]
        # Show only EV loads for clarity
        for load in house.shiftable_loads:
            if load.type != "ev_charging":
                continue
            b_iso = before_sched[load.load_id]
            a_iso = after_sched[load.load_id]
            b_h = (datetime.fromisoformat(b_iso) - sim_start).total_seconds() / 3600
            a_h = (datetime.fromisoformat(a_iso) - sim_start).total_seconds() / 3600
            dur = load.duration_hours
            # ghost before
            ax.add_patch(patches.Rectangle((b_h, 0.55), dur, 0.30,
                                           facecolor="none",
                                           edgecolor=BEFORE, linewidth=1, linestyle=(0, (3, 2)),
                                           alpha=0.65))
            # filled after
            ax.add_patch(patches.Rectangle((a_h, 0.10), dur, 0.30,
                                           facecolor=accents[house.house_id],
                                           edgecolor=INK, linewidth=0.5,
                                           alpha=0.92))

        ax.set_ylim(0, 1.0)
        ax.set_title(house_titles[house.house_id],
                     fontsize=11.5, color=PAPER, loc="left",
                     fontstyle="italic", pad=4)
        # tiny legend row labels
        ax.text(-0.005, 0.70, "BEFORE",
                transform=ax.get_yaxis_transform(),
                fontsize=8, color=BEFORE, ha="right", va="center",
                family="monospace")
        ax.text(-0.005, 0.25, "AFTER",
                transform=ax.get_yaxis_transform(),
                fontsize=8, color=accents[house.house_id], ha="right", va="center",
                family="monospace")

        if ax is axes[-1]:
            day_starts = list(range(0, H, 24))
            day_labels = []
            for h in day_starts:
                d = sim_start + timedelta(hours=h)
                day_labels.append(DAY_NAMES[(d.weekday()) % 7])
            ax.set_xticks([h + 12 for h in day_starts])
            ax.set_xticklabels(day_labels, fontsize=10, family="monospace")
        else:
            ax.set_xticks([])

    fig.text(0.04, 0.92,
             "EV CHARGING — WHERE EACH AGENT PLACED ITS LOADS  ·  BEFORE vs AFTER",
             fontsize=10, color=PAPER_MUTE, family="monospace")
    fig.text(0.04, 0.04,
             "Each house's agent made different decisions based on its private preference weights. "
             "Same demand. Same windows. Different priorities.",
             fontsize=9, color=PAPER_MUTE, fontstyle="italic")

    fig.savefig(OUT / "agents_diverged.png")
    plt.close(fig)
    print(f"  wrote {OUT / 'agents_diverged.png'}")


# ============================================================
# main
# ============================================================

def main():
    data, transcript = _load()
    print("rendering charts...")
    chart_hero(data, transcript)
    chart_headlines(transcript)
    chart_agents_diverged(data, transcript)
    print("done.")


if __name__ == "__main__":
    main()
