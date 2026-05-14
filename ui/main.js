/* HEMS UI — editorial monitoring console.
   Loads the 10-day neighborhood + transcript, computes before/after
   aggregate curves, renders a dual-line chart + per-house load
   timelines, and animates a clock that sweeps through 240 hours,
   building the consumption graph in real time.
*/

const DATA_URL = "../data/neighborhood_10day.json";
const TRANSCRIPT_URL = "../data/transcript_10day.json";

const HOURS_PER_REAL_SECOND_AT_1X = 8;   // 240 hours plays in ~30 seconds at 1x
const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

const state = {
  data: null,
  transcript: null,
  simStartMs: 0,
  hours: 240,
  beforeCurve: [],
  afterCurve: [],
  roundCurves: [],         // one curve per round
  currentHour: 0,
  speed: 1,
  isPlaying: false,
  lastTime: null,
  currentRound: -1,        // -1 = final (after)
};

// ===================================================================
// data + curve computation
// ===================================================================

async function loadData() {
  const [data, transcript] = await Promise.all([
    fetch(DATA_URL).then(r => r.json()),
    fetch(TRANSCRIPT_URL).then(r => r.json()),
  ]);
  state.data = data;
  state.transcript = transcript;
  state.simStartMs = new Date(data.neighborhood.simulation_start).getTime();
  state.hours = data.neighborhood.simulation_hours;
}

function computeCurve(schedule) {
  const H = state.hours;
  const total = new Array(H).fill(0);
  const perHouse = {};
  for (const house of state.data.houses) {
    const hCurve = house.base_load_kw.slice();
    for (const load of house.shiftable_loads) {
      const startIso = schedule[load.load_id];
      if (!startIso) continue;
      const startMs = new Date(startIso).getTime();
      const offsetHr = (startMs - state.simStartMs) / 3600000;
      let remaining = load.duration_hours;
      let cursor = offsetHr;
      while (remaining > 1e-9) {
        const hi = Math.floor(cursor);
        const used = Math.min(remaining, (hi + 1) - cursor);
        if (hi >= 0 && hi < H) hCurve[hi] += load.power_kw * used;
        cursor += used;
        remaining -= used;
      }
    }
    perHouse[house.house_id] = hCurve;
    for (let i = 0; i < H; i++) total[i] += hCurve[i];
  }
  return { total, perHouse };
}

function computeAllCurves() {
  const beforeRes = computeCurve(state.transcript.before);
  state.beforeCurve = beforeRes.total;
  state.beforePerHouse = beforeRes.perHouse;

  state.roundCurves = state.transcript.rounds.map(r => computeCurve(r.schedule).total);

  const afterRes = computeCurve(state.transcript.final);
  state.afterCurve = afterRes.total;
  state.afterPerHouse = afterRes.perHouse;
}

// ===================================================================
// stats + masthead
// ===================================================================

function renderStats() {
  const t = state.transcript;
  const startStr = new Date(state.data.neighborhood.simulation_start)
    .toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  document.getElementById("dateline").textContent = startStr + " · 10-day simulation";

  document.getElementById("peak-before").textContent = fmtKw(t.before_peak_kw);
  document.getElementById("peak-after").textContent  = fmtKw(t.final_peak_kw);
  document.getElementById("peak-reduction").textContent = t.peak_reduction_pct.toFixed(1) + "%";

  document.getElementById("peak-before-when").textContent = hourToDayLabel(t.before_peak_hour);
  document.getElementById("peak-after-when").textContent  = hourToDayLabel(t.final_peak_hour);
}

function fmtKw(v) { return v.toFixed(1) + " kW"; }

function hourToDayLabel(h) {
  const d = new Date(state.simStartMs + h * 3600000);
  return DAY_NAMES[(d.getDay() + 6) % 7] + " " +
    String(d.getHours()).padStart(2, "0") + ":" +
    String(d.getMinutes()).padStart(2, "0");
}

function fmtClock(hourFloat) {
  const dayIdx = Math.floor(hourFloat / 24);
  const inDay = hourFloat - dayIdx * 24;
  const hh = Math.floor(inDay);
  const mm = Math.floor((inDay - hh) * 60);
  const d = new Date(state.simStartMs + hourFloat * 3600000);
  const dow = DAY_NAMES[(d.getDay() + 6) % 7];
  return `Day ${dayIdx + 1} · ${dow} ${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}`;
}

// ===================================================================
// aggregate chart (D3)
// ===================================================================

const chartGeom = {
  margin: { top: 20, right: 32, bottom: 32, left: 56 },
  get width() { return document.getElementById("agg-chart").clientWidth; },
  get height() { return document.getElementById("agg-chart").clientHeight; },
};

let chartScales = {};

function renderChart() {
  const svg = d3.select("#agg-chart");
  svg.selectAll("*").remove();
  const W = chartGeom.width, H = chartGeom.height;
  const m = chartGeom.margin;
  svg.attr("viewBox", `0 0 ${W} ${H}`);

  const innerW = W - m.left - m.right;
  const innerH = H - m.top - m.bottom;

  const x = d3.scaleLinear().domain([0, state.hours]).range([0, innerW]);
  const yMax = Math.max(d3.max(state.beforeCurve), d3.max(state.afterCurve)) * 1.08;
  const y = d3.scaleLinear().domain([0, yMax]).range([innerH, 0]);
  chartScales = { x, y, innerW, innerH };

  const g = svg.append("g").attr("transform", `translate(${m.left},${m.top})`);

  // y grid
  g.append("g")
    .attr("class", "grid")
    .call(d3.axisLeft(y).ticks(5).tickSize(-innerW).tickFormat(""));

  // day shading (alternate)
  for (let d = 0; d < state.hours / 24; d++) {
    if (d % 2 === 0) {
      g.append("rect")
        .attr("x", x(d * 24)).attr("y", 0)
        .attr("width", x(24) - x(0)).attr("height", innerH)
        .attr("fill", "rgba(255,255,255,0.015)");
    }
  }

  // day tick marks + labels on x-axis (using day names)
  const dayTicks = g.append("g").attr("transform", `translate(0,${innerH})`).attr("class", "axis");
  for (let d = 0; d <= state.hours / 24; d++) {
    const date = new Date(state.simStartMs + d * 24 * 3600000);
    const label = DAY_NAMES[(date.getDay() + 6) % 7];
    const gx = dayTicks.append("g").attr("transform", `translate(${x(d * 24)},0)`);
    gx.append("line").attr("y2", 6).attr("stroke", "currentColor").attr("stroke-opacity", 0.25);
    if (d < state.hours / 24) {
      gx.append("text")
        .attr("x", (x(24) - x(0)) / 2).attr("y", 18)
        .attr("text-anchor", "middle")
        .style("fill", "var(--paper-mute)")
        .style("font-family", "var(--font-mono)")
        .style("font-size", "10px")
        .style("letter-spacing", "0.14em")
        .text(label.toUpperCase());
    }
  }

  // y-axis (kW)
  g.append("g")
    .attr("class", "axis")
    .call(d3.axisLeft(y).ticks(5).tickFormat(v => v + " kW"))
    .call(s => s.select(".domain").remove());

  // shape definitions
  const lineGen = d3.line()
    .x((_, i) => x(i))
    .y(d => y(d))
    .curve(d3.curveMonotoneX);
  const areaGen = d3.area()
    .x((_, i) => x(i))
    .y0(innerH)
    .y1(d => y(d))
    .curve(d3.curveMonotoneX);

  // "Before" line is always fully visible — it's the frozen baseline.
  // The coordinated "after" line gets progressively revealed as the
  // clock advances, conveying the optimization unfolding in real time.
  g.append("path")
    .datum(state.beforeCurve)
    .attr("class", "line-before")
    .attr("d", lineGen);

  svg.append("defs").append("clipPath").attr("id", "reveal-clip").append("rect")
    .attr("x", 0).attr("y", 0).attr("width", 0).attr("height", innerH);

  const layer = g.append("g").attr("clip-path", "url(#reveal-clip)");

  layer.append("path")
    .datum(state.afterCurve)
    .attr("class", "area-after")
    .attr("d", areaGen);

  layer.append("path")
    .datum(state.afterCurve)
    .attr("class", "line-after")
    .attr("d", lineGen);

  // peak markers (drawn outside the clip so they're always visible)
  const tBefore = state.transcript;
  const peakAfterIdx = tBefore.final_peak_hour;
  const peakBeforeIdx = tBefore.before_peak_hour;

  const peakMarker = (idx, value, cls, label) => {
    const grp = g.append("g")
      .attr("class", `peak-marker ${cls}`)
      .attr("transform", `translate(${x(idx)},${y(value)})`);
    grp.append("circle").attr("r", 4);
    grp.append("text")
      .attr("x", 10).attr("y", 0)
      .attr("dominant-baseline", "middle")
      .text(label);
    return grp;
  };
  state.peakMarkers = {
    before: peakMarker(peakBeforeIdx, state.beforeCurve[peakBeforeIdx], "peak-marker-before", `Peak ${fmtKw(state.transcript.before_peak_kw)}`),
    after: peakMarker(peakAfterIdx, state.afterCurve[peakAfterIdx], "peak-marker-after", `Peak ${fmtKw(state.transcript.final_peak_kw)}`),
  };

  // cursor (vertical line + label)
  const cursorG = g.append("g").attr("class", "cursor");
  cursorG.append("line")
    .attr("class", "cursor-line")
    .attr("y1", 0).attr("y2", innerH);
  cursorG.append("text")
    .attr("class", "cursor-label")
    .attr("y", -6)
    .attr("text-anchor", "middle");

  state.chartRefs = { cursorG, layer, x, y, innerW, innerH };
}

function updateChartCursor() {
  if (!state.chartRefs) return;
  const { cursorG, x, innerH } = state.chartRefs;
  const cx = x(state.currentHour);
  cursorG.select(".cursor-line").attr("x1", cx).attr("x2", cx);
  cursorG.select(".cursor-label").attr("x", cx).text("NOW");

  // clip width = revealed portion
  d3.select("#reveal-clip rect").attr("width", cx);
}

// ===================================================================
// per-house panels (D3)
// ===================================================================

const LOAD_TYPE_ROW = { ev_charging: 0, washer_dryer: 1, water_heater: 2 };
const LOAD_TYPE_COLOR = {
  ev_charging:   "var(--ev)",
  washer_dryer:  "var(--laundry)",
  water_heater:  "var(--wh)",
};
const LOAD_ROW_LABELS = ["EV", "WSH", "H₂O"];

function renderHouses() {
  const grid = document.getElementById("house-grid");
  grid.innerHTML = "";
  state.houseRefs = {};

  for (const house of state.data.houses) {
    const card = document.createElement("div");
    card.className = "house-card";
    card.dataset.house = house.house_id;
    card.innerHTML = `
      <div class="house-head">
        <div>
          <div class="house-id">${house.house_id}</div>
          <h3 class="house-archetype">${formatArchetype(house.archetype)}</h3>
        </div>
        <div class="house-meta">
          <span><strong>${house.occupants}</strong> ${house.occupants === 1 ? "occupant" : "occupants"}</span>
        </div>
      </div>
      <div class="house-timeline"><svg></svg></div>
      <div class="house-stat-row">
        <span>Loads <strong class="load-count">${house.shiftable_loads.length}</strong></span>
        <span>Energy <strong class="load-energy">${totalEnergyKwh(house).toFixed(1)} kWh</strong></span>
      </div>
      <div class="house-message" data-house="${house.house_id}">—</div>
    `;
    grid.appendChild(card);

    const svg = d3.select(card).select("svg");
    state.houseRefs[house.house_id] = renderHouseTimeline(house, svg);
  }
}

function formatArchetype(a) {
  const map = {
    retired_couple:      "retired <em>couple</em>",
    young_family:        "young <em>family</em>",
    wfh_single:          "wfh <em>single</em>",
    dual_income_no_kids: "two earners <em>no kids</em>",
  };
  return map[a] || a;
}

function totalEnergyKwh(house) {
  return house.shiftable_loads.reduce((s, l) => s + l.power_kw * l.duration_hours, 0);
}

function renderHouseTimeline(house, svg) {
  const W = svg.node().clientWidth || 300;
  const H = 64;
  svg.attr("viewBox", `0 0 ${W} ${H}`);
  svg.selectAll("*").remove();

  const padLeft = 32, padRight = 4;
  const innerW = W - padLeft - padRight;
  const rowH = 14, rowGap = 4;
  const xScale = v => padLeft + (v / state.hours) * innerW;

  // alternating day shading
  for (let d = 0; d < state.hours / 24; d++) {
    if (d % 2 === 1) {
      svg.append("rect")
        .attr("x", xScale(d * 24))
        .attr("y", 2)
        .attr("width", xScale(24) - xScale(0))
        .attr("height", H - 4)
        .attr("fill", "rgba(255,255,255,0.025)");
    }
  }

  // row labels
  ["ev_charging", "washer_dryer", "water_heater"].forEach((t, i) => {
    svg.append("text")
      .attr("class", "row-label")
      .attr("x", 4)
      .attr("y", 2 + i * (rowH + rowGap) + rowH * 0.7)
      .text(LOAD_ROW_LABELS[i]);
  });

  const finalSched = state.transcript.final;
  const beforeSched = state.transcript.before;

  // ghost (before) load positions — faint outlines
  const ghostLayer = svg.append("g").attr("class", "ghost-layer");
  for (const load of house.shiftable_loads) {
    const startIso = beforeSched[load.load_id];
    if (!startIso) continue;
    const startHr = (new Date(startIso).getTime() - state.simStartMs) / 3600000;
    const row = LOAD_TYPE_ROW[load.type];
    ghostLayer.append("rect")
      .attr("x", xScale(startHr))
      .attr("y", 2 + row * (rowH + rowGap))
      .attr("width", Math.max(1, xScale(load.duration_hours) - xScale(0)))
      .attr("height", rowH)
      .attr("fill", "none")
      .attr("stroke", "var(--before)")
      .attr("stroke-width", 0.5)
      .attr("opacity", 0.35);
  }

  // final (after) load positions — filled blocks
  const blockLayer = svg.append("g").attr("class", "block-layer");
  const blocksByLoad = {};
  for (const load of house.shiftable_loads) {
    const startIso = finalSched[load.load_id];
    if (!startIso) continue;
    const startHr = (new Date(startIso).getTime() - state.simStartMs) / 3600000;
    const row = LOAD_TYPE_ROW[load.type];
    const block = blockLayer.append("rect")
      .attr("class", "load-block dim")
      .attr("x", xScale(startHr))
      .attr("y", 2 + row * (rowH + rowGap))
      .attr("width", Math.max(1, xScale(load.duration_hours) - xScale(0)))
      .attr("height", rowH)
      .attr("fill", LOAD_TYPE_COLOR[load.type])
      .attr("rx", 1)
      .style("color", LOAD_TYPE_COLOR[load.type])
      .datum({ start: startHr, end: startHr + load.duration_hours, load });
    blocksByLoad[load.load_id] = block;
  }

  // cursor line
  const cursor = svg.append("line")
    .attr("class", "cursor-line")
    .attr("y1", 2).attr("y2", H - 2)
    .attr("stroke", "var(--gold)")
    .attr("stroke-width", 1)
    .attr("opacity", 0.85);

  return { svg, xScale, blockLayer, blocksByLoad, cursor };
}

function updateHouseTimelines() {
  for (const houseId of Object.keys(state.houseRefs)) {
    const ref = state.houseRefs[houseId];
    const cx = ref.xScale(state.currentHour);
    ref.cursor.attr("x1", cx).attr("x2", cx);
    ref.blockLayer.selectAll("rect").each(function(d) {
      const passed = state.currentHour >= d.start;
      const active = state.currentHour >= d.start && state.currentHour < d.end;
      d3.select(this)
        .classed("dim", !passed)
        .classed("active", active);
    });
  }
}

// ===================================================================
// coordinator pull quote + per-house messages
// ===================================================================

let lastRoundShown = -2;

function currentRoundForHour(h) {
  // Map 240 hours to 3 rounds: 0..79 = R1, 80..159 = R2, 160..239 = R3.
  // Visualizes "negotiation happening as time passes."
  const r = Math.min(state.transcript.rounds.length - 1, Math.floor(h / (state.hours / state.transcript.rounds.length)));
  return r;
}

function updateCoordinator() {
  const r = currentRoundForHour(state.currentHour);
  if (r === lastRoundShown) return;
  lastRoundShown = r;
  const round = state.transcript.rounds[r];
  document.getElementById("coord-body").textContent = round.coordinator_message;
  document.getElementById("coord-attr").textContent =
    `— Coordinator · Round ${round.round_number} of ${state.transcript.rounds.length} · peak ${round.peak_kw.toFixed(1)} kW`;

  // house messages
  for (const houseId of Object.keys(round.house_messages)) {
    const el = document.querySelector(`.house-message[data-house="${houseId}"]`);
    if (el) {
      el.style.opacity = 0;
      requestAnimationFrame(() => {
        el.textContent = round.house_messages[houseId];
        el.style.transition = "opacity 360ms ease";
        el.style.opacity = 1;
      });
    }
  }

  // round pager
  document.querySelectorAll(".round-dot").forEach((el, i) => {
    el.classList.toggle("active", i === r);
  });
}

function renderRoundPager() {
  const pager = document.getElementById("round-pager");
  pager.innerHTML = "";
  state.transcript.rounds.forEach((round, i) => {
    const dot = document.createElement("button");
    dot.className = "round-dot";
    dot.textContent = `Round ${round.round_number}`;
    dot.addEventListener("click", () => {
      // Jump time to the midpoint of this round's window.
      const span = state.hours / state.transcript.rounds.length;
      state.currentHour = span * i + span / 2;
      syncFromScrub();
      // and force a coord refresh
      lastRoundShown = -2;
      updateAll();
    });
    pager.appendChild(dot);
  });
}

// ===================================================================
// playback controls
// ===================================================================

function setupControls() {
  const playBtn = document.getElementById("play-btn");
  const playIcon = document.getElementById("play-icon");
  const pauseIcon = document.getElementById("pause-icon");
  const scrub = document.getElementById("scrub");
  scrub.max = state.hours - 1;
  const speedToggle = document.getElementById("speed-toggle");

  playBtn.addEventListener("click", () => {
    state.isPlaying = !state.isPlaying;
    playIcon.style.display = state.isPlaying ? "none" : "block";
    pauseIcon.style.display = state.isPlaying ? "block" : "none";
    if (state.isPlaying) state.lastTime = performance.now();
  });

  scrub.addEventListener("input", () => {
    state.currentHour = +scrub.value;
    updateAll();
  });

  speedToggle.querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", () => {
      speedToggle.querySelectorAll("button").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      state.speed = +btn.dataset.speed;
    });
  });

  // day-ticks labels under scrubber
  const dayTicks = document.getElementById("day-ticks");
  dayTicks.innerHTML = "";
  for (let d = 0; d < state.hours / 24; d++) {
    const span = document.createElement("span");
    const date = new Date(state.simStartMs + d * 24 * 3600000);
    span.textContent = DAY_NAMES[(date.getDay() + 6) % 7];
    dayTicks.appendChild(span);
  }
}

function syncFromScrub() {
  const scrub = document.getElementById("scrub");
  scrub.value = state.currentHour;
}

// ===================================================================
// main loop + glue
// ===================================================================

function updateAll() {
  document.getElementById("clock").textContent = fmtClock(state.currentHour);
  updateChartCursor();
  updateHouseTimelines();
  updateCoordinator();
  syncFromScrub();
}

function tick(ts) {
  if (state.isPlaying) {
    if (state.lastTime == null) state.lastTime = ts;
    const dt = (ts - state.lastTime) / 1000;
    state.lastTime = ts;
    state.currentHour += dt * state.speed * HOURS_PER_REAL_SECOND_AT_1X;
    if (state.currentHour >= state.hours) {
      state.currentHour = 0;
      lastRoundShown = -2;
    }
    updateAll();
  } else {
    state.lastTime = null;
  }
  requestAnimationFrame(tick);
}

async function init() {
  try {
    await loadData();
    computeAllCurves();
    renderStats();
    renderChart();
    renderHouses();
    renderRoundPager();
    setupControls();
    updateAll();
    // Auto-play almost immediately — the "before" baseline reads instantly,
    // and the negotiation unfolds without the user hunting for play.
    setTimeout(() => {
      if (!state.isPlaying) document.getElementById("play-btn").click();
    }, 600);
    // gentle resize handling
    window.addEventListener("resize", () => {
      renderChart();
      // re-render house timelines (they pick up new widths)
      for (const houseId of Object.keys(state.houseRefs)) {
        const house = state.data.houses.find(h => h.house_id === houseId);
        const svg = d3.select(`.house-card[data-house="${houseId}"] svg`);
        state.houseRefs[houseId] = renderHouseTimeline(house, svg);
      }
      updateAll();
    });
    requestAnimationFrame(tick);
  } catch (err) {
    document.getElementById("coord-body").textContent =
      `Failed to load data. Are you serving from the project root? (${err.message})`;
    console.error(err);
  }
}

init();
