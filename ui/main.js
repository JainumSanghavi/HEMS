/* =============================================================
   HEMS · The Negotiated Grid
   Rich-edition UI: net grid demand, solar, battery, DR events,
   carbon intensity, bilateral swap choreography.

   This file is intentionally framework-free and uses D3 v7 from CDN.
   ============================================================= */

const DATA_URLS = [
  "../data/neighborhood_rich.json",
  "../data/neighborhood_10day.json",
];
const TRANSCRIPT_URLS = [
  "../data/transcript_rich.json",
  "../data/transcript_10day.json",
];

const HOURS_PER_SECOND_1X = 8;  // 240h → 30s at 1× (matches prior pacing)
const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

const LOAD_TYPE_ROW = { ev_charging: 0, washer_dryer: 1, water_heater: 2 };
const LOAD_TYPE_COLOR = {
  ev_charging:   "var(--ev)",
  washer_dryer:  "var(--laundry)",
  water_heater:  "var(--wh)",
};
const LOAD_ROW_LABELS = ["EV", "WSH", "H₂O"];

const HOUSE_ACCENT = { H1: "var(--amber)", H2: "var(--after)", H3: "var(--lavender)" };

const state = {
  data: null,
  transcript: null,
  datasetTag: "",
  simStartMs: 0,
  hours: 240,

  // aggregate curves
  beforeNetCurve: [],
  afterNetCurve:  [],
  beforeGrossCurve: [],
  afterGrossCurve:  [],
  aggSolarCurve: [],  // sum across houses (positive)

  // per-house traces (computed from richer transcript or simulated client-side)
  perHouseBefore: {},
  perHouseAfter:  {},

  // headline metrics
  peakBeforeKw: 0,
  peakAfterKw:  0,
  peakBeforeHour: 0,
  peakAfterHour:  0,
  peakReductionPct: 0,
  dollarsSavedMonth: 0,
  co2SavedMonthKg: 0,

  // swaps (rich transcript only)
  swaps: [],
  swapsByHour: {},
  swapsShown: new Set(),

  // playback
  currentHour: 0,
  speed: 1,
  isPlaying: false,
  lastTime: null,
  mode: "animate",  // or "compare"

  // refs into DOM/SVG
  chartRefs: null,
  carbonRefs: null,
  houseRefs: {},
  housePositions: {},
};

// ===================================================================
// DATA LOADING
// ===================================================================

async function tryFetch(urls) {
  for (const url of urls) {
    try {
      const r = await fetch(url);
      if (r.ok) {
        const json = await r.json();
        return { json, url };
      }
    } catch (e) { /* try next */ }
  }
  throw new Error("could not load any of " + urls.join(", "));
}

async function loadData() {
  const [d, t] = await Promise.all([tryFetch(DATA_URLS), tryFetch(TRANSCRIPT_URLS)]);
  state.data = d.json;
  state.transcript = t.json;
  state.datasetTag = `dataset · ${d.url.split("/").pop()} + ${t.url.split("/").pop()}`;
  state.simStartMs = new Date(state.data.neighborhood.simulation_start).getTime();
  state.hours = state.data.neighborhood.simulation_hours;
}

// ===================================================================
// CURVE COMPUTATION — fallback when transcript_rich isn't present
// ===================================================================

function isPeakHour(absHour) {
  const tariff = state.data.neighborhood.tariff;
  if (!tariff) return false;
  const d = new Date(state.simStartMs + absHour * 3600000);
  return tariff.peak_hours.includes(d.getHours());
}

function generateSolar(house) {
  const irr = state.data.neighborhood.solar_irradiance_wm2 || [];
  if (!house.solar || !irr.length) return new Array(state.hours).fill(0);
  const factor = house.solar.panel_kw * (house.solar.inverter_efficiency || 0.95) / 1000;
  return irr.map(w => Math.max(0, w * factor));
}

function dispatchBattery(house, netBeforeBattery) {
  const H = netBeforeBattery.length;
  if (!house.battery) {
    return { battery_kw: new Array(H).fill(0), battery_soc: new Array(H).fill(0) };
  }
  const b = house.battery;
  let soc = b.capacity_kwh * (b.initial_soc ?? 0.5);
  const eta = Math.sqrt(b.round_trip_efficiency ?? 0.9);
  const battery = new Array(H).fill(0);
  const socArr = new Array(H).fill(0);
  for (let h = 0; h < H; h++) {
    const net = netBeforeBattery[h];
    if (net < 0) {
      // surplus → charge
      const avail = b.capacity_kwh - soc;
      const want = Math.min(-net, b.max_charge_kw, avail / eta);
      if (want > 0) {
        battery[h] = want;
        soc += want * eta;
      }
    } else if (isPeakHour(h) && soc > 0.3 * b.capacity_kwh) {
      const want = Math.min(net, b.max_discharge_kw, soc - 0.2 * b.capacity_kwh);
      if (want > 0) {
        battery[h] = -want;
        soc -= want / eta;
      }
    }
    socArr[h] = soc;
  }
  return { battery_kw: battery, battery_soc: socArr };
}

function computeCurveClientSide(schedule) {
  const H = state.hours;
  const tot = new Array(H).fill(0);
  const totNet = new Array(H).fill(0);
  const totSolar = new Array(H).fill(0);
  const perHouse = {};
  for (const house of state.data.houses) {
    const base = house.base_load_kw.slice();
    const shift = new Array(H).fill(0);
    for (const load of house.shiftable_loads) {
      const startIso = schedule[load.load_id];
      if (!startIso) continue;
      const offsetHr = (new Date(startIso).getTime() - state.simStartMs) / 3600000;
      let remaining = load.duration_hours, cursor = offsetHr;
      while (remaining > 1e-9) {
        const hi = Math.floor(cursor);
        const used = Math.min(remaining, (hi + 1) - cursor);
        if (hi >= 0 && hi < H) shift[hi] += load.power_kw * used;
        cursor += used;
        remaining -= used;
      }
    }
    const solar = generateSolar(house);
    const netBefore = base.map((b, i) => b + shift[i] - solar[i]);
    const { battery_kw, battery_soc } = dispatchBattery(house, netBefore);
    const netGrid = netBefore.map((v, i) => v + battery_kw[i]);
    perHouse[house.house_id] = {
      gross_load_kw: base.map((b, i) => b + shift[i]),
      base_kw: base,
      shiftable_kw: shift,
      solar_kw: solar,
      battery_kw,
      battery_soc,
      net_grid_kw: netGrid,
    };
    for (let i = 0; i < H; i++) {
      tot[i]     += base[i] + shift[i];
      totNet[i]  += netGrid[i];
      totSolar[i] += solar[i];
    }
  }
  return { totalGross: tot, totalNet: totNet, totalSolar: totSolar, perHouse };
}

function tryReadRichTraces(forSchedule /* "before" | "after" */) {
  // If transcript has per_house.{hid}.before.* or per_house.{hid}.{phase}.* etc., use it.
  // We probe several shapes the parent might emit.
  const t = state.transcript;
  if (!t.per_house) return null;
  const out = {};
  const probeKeys = [
    `${forSchedule}_net_grid_kw`,
    forSchedule, // nested object e.g. per_house.H1.before.net_grid_kw
    forSchedule === "after" ? "final_net_grid_kw" : null,
  ].filter(Boolean);
  for (const hid of Object.keys(t.per_house)) {
    const ph = t.per_house[hid];
    const slot = { net_grid_kw: null, solar_kw: null, battery_kw: null, battery_soc: null, gross_load_kw: null };
    // shape A: flat ("net_grid_kw" applies to "after" — the simulator's final)
    if (forSchedule === "after" && Array.isArray(ph.net_grid_kw)) {
      slot.net_grid_kw = ph.net_grid_kw;
      slot.solar_kw    = ph.solar_kw || null;
      slot.battery_kw  = ph.battery_kw || null;
      slot.battery_soc = ph.battery_soc || null;
      slot.gross_load_kw = ph.gross_load_kw || null;
    }
    // shape B: phase-scoped
    if (ph[forSchedule] && typeof ph[forSchedule] === "object") {
      const p = ph[forSchedule];
      slot.net_grid_kw = p.net_grid_kw || slot.net_grid_kw;
      slot.solar_kw    = p.solar_kw    || slot.solar_kw;
      slot.battery_kw  = p.battery_kw  || slot.battery_kw;
      slot.battery_soc = p.battery_soc || slot.battery_soc;
      slot.gross_load_kw = p.gross_load_kw || slot.gross_load_kw;
    }
    // shape C: prefixed keys
    for (const k of probeKeys) {
      if (Array.isArray(ph[k])) slot.net_grid_kw = ph[k];
    }
    if (!slot.net_grid_kw) return null;  // no useful data → bail to client-side
    out[hid] = slot;
  }
  return out;
}

function computeAllCurves() {
  // try rich-transcript shape first
  const afterRich  = tryReadRichTraces("after");
  const beforeRich = tryReadRichTraces("before");

  const afterFallback  = computeCurveClientSide(state.transcript.final);
  const beforeFallback = computeCurveClientSide(state.transcript.before);

  function aggregate(perHouse) {
    const H = state.hours;
    const net = new Array(H).fill(0);
    const gross = new Array(H).fill(0);
    const solar = new Array(H).fill(0);
    for (const hid of Object.keys(perHouse)) {
      const p = perHouse[hid];
      for (let i = 0; i < H; i++) {
        net[i]   += p.net_grid_kw[i] ?? 0;
        gross[i] += p.gross_load_kw ? p.gross_load_kw[i] : (p.base_kw?.[i] ?? 0) + (p.shiftable_kw?.[i] ?? 0);
        solar[i] += p.solar_kw?.[i] ?? 0;
      }
    }
    return { net, gross, solar };
  }

  let afterPerHouse, beforePerHouse;
  let afterAgg, beforeAgg;
  if (afterRich) {
    // fill in fields missing from rich by using fallback (e.g., gross, solar)
    afterPerHouse = {};
    for (const hid of Object.keys(afterRich)) {
      afterPerHouse[hid] = {
        ...afterFallback.perHouse[hid],
        ...afterRich[hid],
      };
    }
    afterAgg = aggregate(afterPerHouse);
  } else {
    afterPerHouse = afterFallback.perHouse;
    afterAgg = { net: afterFallback.totalNet, gross: afterFallback.totalGross, solar: afterFallback.totalSolar };
  }
  if (beforeRich) {
    beforePerHouse = {};
    for (const hid of Object.keys(beforeRich)) {
      beforePerHouse[hid] = {
        ...beforeFallback.perHouse[hid],
        ...beforeRich[hid],
      };
    }
    beforeAgg = aggregate(beforePerHouse);
  } else {
    beforePerHouse = beforeFallback.perHouse;
    beforeAgg = { net: beforeFallback.totalNet, gross: beforeFallback.totalGross, solar: beforeFallback.totalSolar };
  }

  state.perHouseBefore = beforePerHouse;
  state.perHouseAfter  = afterPerHouse;
  state.beforeNetCurve = beforeAgg.net;
  state.afterNetCurve  = afterAgg.net;
  state.beforeGrossCurve = beforeAgg.gross;
  state.afterGrossCurve  = afterAgg.gross;
  state.aggSolarCurve  = afterAgg.solar;  // solar generation doesn't change with schedule

  // Headline metrics
  const t = state.transcript;
  let peakBeforeKw = t.before_peak_kw;
  let peakAfterKw  = t.final_peak_kw;
  let peakBeforeHour = t.before_peak_hour;
  let peakAfterHour  = t.final_peak_hour;
  // recompute against net curve so the chart and headline agree
  const argMax = (arr) => arr.reduce((best, v, i) => v > arr[best] ? i : best, 0);
  if (state.beforeNetCurve.length === state.hours) {
    peakBeforeHour = argMax(state.beforeNetCurve);
    peakBeforeKw   = state.beforeNetCurve[peakBeforeHour];
  }
  if (state.afterNetCurve.length === state.hours) {
    peakAfterHour = argMax(state.afterNetCurve);
    peakAfterKw   = state.afterNetCurve[peakAfterHour];
  }
  state.peakBeforeKw = peakBeforeKw;
  state.peakAfterKw  = peakAfterKw;
  state.peakBeforeHour = peakBeforeHour;
  state.peakAfterHour  = peakAfterHour;
  state.peakReductionPct = (peakBeforeKw - peakAfterKw) / Math.max(1e-9, peakBeforeKw) * 100;

  // $ saved per month: tariff cost(before) - cost(after) over 10 days * 3
  const tariff = state.data.neighborhood.tariff;
  const cost = (curve) => {
    let s = 0;
    for (let h = 0; h < state.hours; h++) {
      const rate = isPeakHour(h) ? tariff.peak_rate_per_kwh : tariff.offpeak_rate_per_kwh;
      const v = curve[h];
      if (v >= 0) s += v * rate;
      else        s += v * (tariff.export_credit_per_kwh || 0);  // export = credit
    }
    return s;
  };
  const dollarsSaved10d = cost(state.beforeNetCurve) - cost(state.afterNetCurve);
  state.dollarsSavedMonth = Math.max(0, dollarsSaved10d * 3);

  // kg CO₂ saved per month: ∑ (before_net - after_net) * gco2_per_kwh, /1000 → kg
  const carbon = state.data.neighborhood.carbon_intensity_gco2_per_kwh || [];
  let co2g = 0;
  for (let h = 0; h < state.hours; h++) {
    const diff = Math.max(0, state.beforeNetCurve[h]) - Math.max(0, state.afterNetCurve[h]);
    co2g += diff * (carbon[h] || 0);
  }
  state.co2SavedMonthKg = Math.max(0, (co2g / 1000) * 3);

  // Prefer transcript-supplied metrics if present.
  // Rich edition emits cost_savings_usd / co2_savings_kg over the 10-day window;
  // we extrapolate to a month (×3).
  if (typeof t.cost_savings_usd === "number") {
    state.dollarsSavedMonth = Math.max(0, t.cost_savings_usd * 3);
  } else if (typeof t.dollars_saved_month === "number") {
    state.dollarsSavedMonth = t.dollars_saved_month;
  }
  if (typeof t.co2_savings_kg === "number") {
    state.co2SavedMonthKg = Math.max(0, t.co2_savings_kg * 3);
  } else if (typeof t.co2_saved_month_kg === "number") {
    state.co2SavedMonthKg = t.co2_saved_month_kg;
  }

  // Swaps. The rich transcript emits time-shift swaps (one load moves
  // earlier/later); each has {load_id, house_id, from_hour, to_hour,
  // accepted, rationale, reduced_peak_kw}. We choreograph them in order,
  // pacing them through the back half of the timeline.
  const rawSwaps = Array.isArray(t.swaps) ? t.swaps : [];
  state.swaps = rawSwaps.map((sw, idx) => ({ ...sw, _idx: idx }));
  state.swapsByHour = {};
  state.swaps.forEach((sw, idx) => {
    const hour = (typeof sw.trigger_hour === "number")
      ? sw.trigger_hour
      : Math.round(state.hours * 0.45 + (idx + 1) * (state.hours * 0.5 / Math.max(1, state.swaps.length + 1)));
    sw._hour = hour;
    state.swapsByHour[hour] = state.swapsByHour[hour] || [];
    state.swapsByHour[hour].push(sw);
  });
}

// ===================================================================
// MASTHEAD + HEADLINE STATS
// ===================================================================

function fmtKw(v)   { return v.toFixed(1) + " kW"; }
function fmtPct(v)  { return v.toFixed(1) + "%"; }
function fmtMoney(v){ return Math.round(v).toLocaleString(); }
function fmtCO2(v)  { return Math.round(v).toLocaleString(); }

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

function renderMasthead() {
  const startStr = new Date(state.data.neighborhood.simulation_start)
    .toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  const endStr = new Date(state.simStartMs + state.hours * 3600000)
    .toLocaleDateString("en-US", { month: "short", day: "numeric" });
  document.getElementById("dateline").textContent =
    `${startStr.toUpperCase()} → ${endStr.toUpperCase()}`;
  document.getElementById("dataset-tag").textContent = state.datasetTag;
}

function renderStats() {
  document.getElementById("peak-reduction-pct").textContent = fmtPct(state.peakReductionPct);
  document.getElementById("peak-before").textContent = fmtKw(state.peakBeforeKw);
  document.getElementById("peak-after").textContent  = fmtKw(state.peakAfterKw);
  document.getElementById("dollars-saved").textContent = fmtMoney(state.dollarsSavedMonth);
  document.getElementById("dollars-saved-detail").textContent =
    `tariff: $${state.data.neighborhood.tariff.peak_rate_per_kwh.toFixed(2)} peak · $${state.data.neighborhood.tariff.offpeak_rate_per_kwh.toFixed(2)} offpeak`;
  document.getElementById("co2-saved").textContent = fmtCO2(state.co2SavedMonthKg);
  document.getElementById("co2-saved-detail").textContent =
    `grid avg ${avgCarbon().toFixed(0)} gCO₂/kWh · cleanest at midday`;
}

function avgCarbon() {
  const c = state.data.neighborhood.carbon_intensity_gco2_per_kwh || [];
  if (!c.length) return 0;
  return c.reduce((a, b) => a + b, 0) / c.length;
}

// ===================================================================
// AGGREGATE CHART
// ===================================================================

function renderChart() {
  const svgNode = document.getElementById("agg-chart");
  const svg = d3.select(svgNode);
  svg.selectAll("*").remove();
  const W = svgNode.clientWidth || 800;
  const H = svgNode.clientHeight || 420;
  const m = { top: 28, right: 36, bottom: 38, left: 64 };
  svg.attr("viewBox", `0 0 ${W} ${H}`);

  const innerW = W - m.left - m.right;
  const innerH = H - m.top - m.bottom;

  // y domain: include negative space for solar (shown below baseline)
  const maxNet   = Math.max(d3.max(state.beforeNetCurve), d3.max(state.afterNetCurve)) * 1.08;
  const maxSolar = d3.max(state.aggSolarCurve) || 0;
  const yMax = maxNet;
  const yMin = -Math.max(1, maxSolar) * 1.15;

  const x = d3.scaleLinear().domain([0, state.hours]).range([0, innerW]);
  const y = d3.scaleLinear().domain([yMin, yMax]).range([innerH, 0]).nice();

  // ---- gradient defs ----
  const defs = svg.append("defs");
  const solarGrad = defs.append("linearGradient")
    .attr("id", "solar-gradient")
    .attr("x1", 0).attr("y1", 0).attr("x2", 0).attr("y2", 1);
  solarGrad.append("stop").attr("offset", "0%").attr("stop-color", "var(--amber)").attr("stop-opacity", "0.55");
  solarGrad.append("stop").attr("offset", "100%").attr("stop-color", "var(--amber)").attr("stop-opacity", "0.0");

  const peakGrad = defs.append("linearGradient")
    .attr("id", "peak-gradient")
    .attr("x1", 0).attr("y1", 0).attr("x2", 0).attr("y2", 1);
  peakGrad.append("stop").attr("offset", "0%").attr("stop-color", "var(--amber)").attr("stop-opacity", "0.12");
  peakGrad.append("stop").attr("offset", "100%").attr("stop-color", "var(--amber)").attr("stop-opacity", "0.0");

  // reveal clip
  defs.append("clipPath").attr("id", "reveal-clip").append("rect")
    .attr("x", 0).attr("y", -10).attr("width", 0).attr("height", innerH + 20);

  const g = svg.append("g").attr("transform", `translate(${m.left},${m.top})`);

  // ---- alternating day shading ----
  for (let d = 0; d < state.hours / 24; d++) {
    if (d % 2 === 0) {
      g.append("rect")
        .attr("x", x(d * 24)).attr("y", 0)
        .attr("width", x((d + 1) * 24) - x(d * 24)).attr("height", innerH)
        .attr("fill", "rgba(255,255,255,0.012)");
    }
  }

  // ---- tariff peak window shading (per day) ----
  const peakHours = state.data.neighborhood.tariff.peak_hours || [];
  if (peakHours.length) {
    const minP = Math.min(...peakHours), maxP = Math.max(...peakHours);
    // map peak-window hours-of-day to absolute hour indices each day
    const sim = new Date(state.simStartMs);
    const startHourOfDay = sim.getHours();
    for (let d = 0; d < state.hours / 24 + 1; d++) {
      // absolute hour where wall-clock = minP on day d
      let absStart = d * 24 + (minP - startHourOfDay);
      let absEnd   = d * 24 + (maxP + 1 - startHourOfDay);
      if (absEnd < 0 || absStart > state.hours) continue;
      absStart = Math.max(0, absStart);
      absEnd   = Math.min(state.hours, absEnd);
      g.append("rect")
        .attr("class", "peak-window")
        .attr("x", x(absStart)).attr("y", 0)
        .attr("width", Math.max(0, x(absEnd) - x(absStart)))
        .attr("height", innerH);
    }
  }

  // ---- DR event bands ----
  const drEvents = state.data.neighborhood.dr_events || [];
  for (const ev of drEvents) {
    const startH = ev.start_hour;
    const endH   = ev.start_hour + ev.duration_hours;
    g.append("rect")
      .attr("class", "dr-band")
      .attr("x", x(startH)).attr("y", 0)
      .attr("width", Math.max(2, x(endH) - x(startH)))
      .attr("height", innerH);
    g.append("line").attr("class", "dr-band-edge").attr("x1", x(startH)).attr("x2", x(startH)).attr("y1", 0).attr("y2", innerH);
    g.append("line").attr("class", "dr-band-edge").attr("x1", x(endH)).attr("x2", x(endH)).attr("y1", 0).attr("y2", innerH);
    g.append("text").attr("class", "dr-label")
      .attr("x", x(startH) + 4).attr("y", 14)
      .text("DR EVENT · " + ev.target_reduction_kw.toFixed(0) + " kW");
  }

  // ---- y-axis grid ----
  g.append("g").attr("class", "grid")
    .call(d3.axisLeft(y).ticks(6).tickSize(-innerW).tickFormat(""));

  // ---- day axis ----
  const dayAxis = g.append("g").attr("class", "axis").attr("transform", `translate(0,${innerH})`);
  for (let d = 0; d <= state.hours / 24; d++) {
    const date = new Date(state.simStartMs + d * 24 * 3600000);
    const label = DAY_NAMES[(date.getDay() + 6) % 7];
    const tx = x(d * 24);
    dayAxis.append("line").attr("x1", tx).attr("x2", tx).attr("y2", 6).attr("stroke", "currentColor").attr("stroke-opacity", 0.25);
    if (d < state.hours / 24) {
      const mid = (x((d + 1) * 24) + x(d * 24)) / 2;
      dayAxis.append("text")
        .attr("x", mid).attr("y", 22)
        .attr("text-anchor", "middle")
        .style("fill", "var(--paper-mute)")
        .style("font-family", "var(--font-mono)")
        .style("font-size", "10px")
        .style("letter-spacing", "0.12em")
        .text(label.toUpperCase() + " · D" + (d + 1));
    }
  }

  // ---- y-axis label ----
  g.append("g").attr("class", "axis")
    .call(d3.axisLeft(y).ticks(6).tickFormat(v => v + " kW"))
    .call(s => s.select(".domain").remove());

  // zero line
  g.append("line").attr("class", "zero-line")
    .attr("x1", 0).attr("x2", innerW)
    .attr("y1", y(0)).attr("y2", y(0));

  // ---- shape generators ----
  const lineGen = d3.line()
    .x((_, i) => x(i)).y(d => y(d))
    .curve(d3.curveMonotoneX);

  // solar area: from 0 → -solar (drawn below the x-axis, inverted)
  const solarArea = d3.area()
    .x((_, i) => x(i))
    .y0(y(0))
    .y1((d) => y(-d))  // negate solar to draw below baseline
    .curve(d3.curveMonotoneX);

  const afterArea = d3.area()
    .x((_, i) => x(i))
    .y0(y(0))
    .y1(d => y(d))
    .curve(d3.curveMonotoneX);

  // solar generation (always visible, full width — it's exogenous)
  g.append("path")
    .datum(state.aggSolarCurve)
    .attr("class", "area-solar")
    .attr("d", solarArea);
  g.append("path")
    .datum(state.aggSolarCurve.map(v => -v))
    .attr("class", "line-solar")
    .attr("d", lineGen);

  // before (always visible, baseline narrative)
  g.append("path")
    .datum(state.beforeNetCurve)
    .attr("class", "line-before")
    .attr("d", lineGen);

  // after — clipped to reveal in animate mode
  const layer = g.append("g").attr("clip-path", "url(#reveal-clip)");
  layer.append("path")
    .datum(state.afterNetCurve)
    .attr("class", "area-after")
    .attr("d", afterArea);
  layer.append("path")
    .datum(state.afterNetCurve)
    .attr("class", "line-after")
    .attr("d", lineGen);

  // ---- peak markers (always visible) ----
  function peakMarker(idx, value, cls, label, dyText, anchor) {
    if (idx == null || isNaN(value)) return null;
    const xPx = x(idx);
    const overflowRight = xPx > innerW - 110;
    const useAnchor = anchor || (overflowRight ? "end" : "start");
    const dx = useAnchor === "end" ? -10 : 10;
    const grp = g.append("g")
      .attr("class", `peak-marker ${cls}`)
      .attr("transform", `translate(${xPx},${y(value)})`);
    grp.append("circle").attr("r", 4);
    grp.append("text")
      .attr("x", dx)
      .attr("y", dyText)
      .attr("text-anchor", useAnchor)
      .text(label);
    return grp;
  }
  peakMarker(state.peakBeforeHour, state.peakBeforeKw, "peak-marker-before", `Peak ${fmtKw(state.peakBeforeKw)} · before`, -12);
  peakMarker(state.peakAfterHour,  state.peakAfterKw,  "peak-marker-after",  `New peak ${fmtKw(state.peakAfterKw)} · after`, 20);

  // ---- cursor ----
  const cursorG = g.append("g").attr("class", "cursor");
  cursorG.append("line")
    .attr("class", "cursor-line")
    .attr("y1", -8).attr("y2", innerH);
  cursorG.append("text")
    .attr("class", "cursor-label")
    .attr("y", -14)
    .attr("text-anchor", "middle")
    .text("NOW");

  state.chartRefs = { svg, g, x, y, innerW, innerH, cursorG };
}

function renderCarbonStrip() {
  const node = document.getElementById("carbon-strip");
  const svg = d3.select(node);
  svg.selectAll("*").remove();
  const W = node.clientWidth || 800;
  const H = 14;
  svg.attr("viewBox", `0 0 ${W} ${H}`);
  const carbon = state.data.neighborhood.carbon_intensity_gco2_per_kwh || [];
  if (!carbon.length) return;
  const cMin = Math.min(...carbon);
  const cMax = Math.max(...carbon);
  const color = d3.scaleLinear()
    .domain([cMin, (cMin + cMax) / 2, cMax])
    .range(["#6be9a8", "#ff9a3c", "#e8454c"]);
  const cellW = W / carbon.length;
  carbon.forEach((c, i) => {
    svg.append("rect")
      .attr("x", i * cellW)
      .attr("y", 0)
      .attr("width", cellW + 0.5)
      .attr("height", H)
      .attr("fill", color(c))
      .attr("opacity", 0.85);
  });
  state.carbonRefs = { svg, W, H, color };
}

function updateChartCursor() {
  if (!state.chartRefs) return;
  const { cursorG, x, innerH } = state.chartRefs;
  const cx = x(state.currentHour);
  cursorG.select(".cursor-line").attr("x1", cx).attr("x2", cx);
  cursorG.select(".cursor-label").attr("x", cx);
  if (state.mode === "animate") {
    d3.select("#reveal-clip rect").attr("width", Math.max(0, cx));
  } else {
    d3.select("#reveal-clip rect").attr("width", state.chartRefs.innerW);
  }
}

// ===================================================================
// HOUSE CARDS
// ===================================================================

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

function renderHouses() {
  const grid = document.getElementById("house-grid");
  grid.innerHTML = "";
  state.houseRefs = {};

  // swap overlay (single SVG above grid)
  const overlay = document.createElement("div");
  overlay.className = "swap-overlay";
  overlay.id = "swap-overlay";
  overlay.innerHTML = `<svg id="swap-svg" preserveAspectRatio="none"></svg>`;
  grid.style.position = "relative";
  grid.appendChild(overlay);

  for (const house of state.data.houses) {
    const card = document.createElement("div");
    card.className = "house-card";
    card.dataset.house = house.house_id;
    card.style.setProperty("--accent", HOUSE_ACCENT[house.house_id] || "var(--gold)");
    card.innerHTML = `
      <div class="house-head">
        <div>
          <div class="house-id">${house.house_id} &nbsp;·&nbsp; ${formatArchetypeFlat(house.archetype)}</div>
          <h3 class="house-archetype">${formatArchetype(house.archetype)}</h3>
        </div>
        <div class="house-occupants">
          <strong>${house.occupants}</strong>
          ${house.occupants === 1 ? "occupant" : "occupants"}
        </div>
      </div>

      <div class="house-systems">${systemBadges(house)}</div>

      <div class="house-personality">${escapeHtml(house.preferences.personality)}</div>

      <div class="house-prefs">
        <div class="pref-bars">
          ${prefBarRow("cost",        house.preferences.cost_weight)}
          ${prefBarRow("comfort",     house.preferences.comfort_weight)}
          ${prefBarRow("carbon",      house.preferences.carbon_weight)}
          ${prefBarRow("reliability", house.preferences.reliability_weight)}
        </div>
        <svg class="pref-radar" viewBox="-50 -50 100 100"></svg>
      </div>

      <div class="house-timeline-wrap">
        <svg class="tl-svg"></svg>
        <div class="tl-legend">
          ${house.solar ? '<span class="li-solar"><i></i>solar</span>' : ''}
          ${house.battery ? '<span class="li-batt"><i></i>battery SOC</span>' : ''}
          <span class="li-ghost"><i></i>before</span>
        </div>
      </div>

      <div class="house-stat-row">
        <span>Loads <strong>${house.shiftable_loads.length}</strong></span>
        <span>Energy <strong>${totalEnergyKwh(house).toFixed(1)} kWh</strong></span>
        <span>Pers. <strong>${shortPersonality(house.archetype)}</strong></span>
      </div>
      <div class="house-message" data-house="${house.house_id}">—</div>
    `;
    grid.appendChild(card);

    renderPrefRadar(card, house);
    const svg = d3.select(card).select("svg.tl-svg");
    state.houseRefs[house.house_id] = renderHouseTimeline(house, svg);
  }
  // animate the pref bars in after a tick
  requestAnimationFrame(() => {
    document.querySelectorAll(".pref-fill").forEach(el => {
      el.style.width = el.dataset.target;
    });
  });
}

function formatArchetypeFlat(a) {
  return {
    retired_couple: "retired couple",
    young_family:   "young family",
    wfh_single:     "wfh single",
    dual_income_no_kids: "two earners",
  }[a] || a;
}

function shortPersonality(a) {
  return {
    retired_couple: "frugal",
    young_family:   "comfort",
    wfh_single:     "eco",
    dual_income_no_kids: "ambivalent",
  }[a] || "—";
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));
}

function systemBadges(house) {
  let parts = [];
  if (house.solar) {
    parts.push(`<span class="sys-badge solar"><span class="sys-ico">☀</span><strong>${house.solar.panel_kw} kW</strong> rooftop PV</span>`);
  } else {
    parts.push(`<span class="sys-badge none"><span class="sys-ico">○</span>no solar</span>`);
  }
  if (house.battery) {
    parts.push(`<span class="sys-badge battery"><span class="sys-ico">▣</span><strong>${house.battery.capacity_kwh} kWh</strong> battery</span>`);
  } else {
    parts.push(`<span class="sys-badge none"><span class="sys-ico">○</span>no battery</span>`);
  }
  return parts.join("");
}

function prefBarRow(key, weight) {
  const pct = Math.round((weight || 0) * 100);
  return `
    <div class="pref-row" data-key="${key}">
      <span>${key}</span>
      <div class="pref-track"><div class="pref-fill" data-target="${pct}%" style="width: 0%"></div></div>
      <span class="pref-num">${pct}</span>
    </div>
  `;
}

function renderPrefRadar(card, house) {
  const svg = d3.select(card).select(".pref-radar");
  const prefs = house.preferences;
  const axes = [
    { k: "cost_weight",        label: "C" },
    { k: "comfort_weight",     label: "F" },
    { k: "carbon_weight",      label: "Co₂" },
    { k: "reliability_weight", label: "R" },
  ];
  const N = axes.length;
  const R = 42;
  // background rings
  [0.33, 0.66, 1].forEach(r => {
    svg.append("circle").attr("r", R * r).attr("fill", "none").attr("stroke", "var(--rule)").attr("stroke-width", 0.5);
  });
  // axes + labels
  axes.forEach((ax, i) => {
    const ang = -Math.PI / 2 + (i / N) * Math.PI * 2;
    const x2 = Math.cos(ang) * R;
    const y2 = Math.sin(ang) * R;
    svg.append("line").attr("class", "axis-line")
      .attr("x1", 0).attr("y1", 0).attr("x2", x2).attr("y2", y2);
    svg.append("text").attr("class", "axis-label")
      .attr("x", Math.cos(ang) * (R + 7))
      .attr("y", Math.sin(ang) * (R + 7) + 3)
      .attr("text-anchor", "middle")
      .text(ax.label);
  });
  // shape
  const points = axes.map((ax, i) => {
    const v = prefs[ax.k] || 0;
    const ang = -Math.PI / 2 + (i / N) * Math.PI * 2;
    return [Math.cos(ang) * R * v, Math.sin(ang) * R * v];
  });
  const path = "M " + points.map(p => p.join(",")).join(" L ") + " Z";
  svg.append("path").attr("class", "radar-shape").attr("d", path);
}

function renderHouseTimeline(house, svg) {
  const node = svg.node();
  const W = node.clientWidth || 320;
  const H = 90;
  svg.attr("viewBox", `0 0 ${W} ${H}`);
  svg.attr("height", H);
  svg.selectAll("*").remove();

  const padLeft = 32, padRight = 4;
  const innerW = W - padLeft - padRight;
  const rowH = 11, rowGap = 3;
  const ganttBaseY = 4;
  const ganttHeight = 3 * rowH + 2 * rowGap;
  const ribbonY = ganttBaseY + ganttHeight + 6;
  const ribbonH = 24;

  const xScale = v => padLeft + (v / state.hours) * innerW;

  // alternating day shading
  for (let d = 0; d < state.hours / 24; d++) {
    if (d % 2 === 1) {
      svg.append("rect")
        .attr("x", xScale(d * 24))
        .attr("y", 0)
        .attr("width", xScale((d + 1) * 24) - xScale(d * 24))
        .attr("height", H)
        .attr("fill", "rgba(255,255,255,0.018)");
    }
  }

  // tariff peak window shading
  const peakHours = state.data.neighborhood.tariff.peak_hours || [];
  if (peakHours.length) {
    const minP = Math.min(...peakHours), maxP = Math.max(...peakHours);
    const startHourOfDay = new Date(state.simStartMs).getHours();
    for (let d = 0; d < state.hours / 24 + 1; d++) {
      const absStart = Math.max(0, d * 24 + (minP - startHourOfDay));
      const absEnd   = Math.min(state.hours, d * 24 + (maxP + 1 - startHourOfDay));
      if (absEnd <= absStart) continue;
      svg.append("rect")
        .attr("x", xScale(absStart))
        .attr("y", 0)
        .attr("width", xScale(absEnd) - xScale(absStart))
        .attr("height", ganttBaseY + ganttHeight + 2)
        .attr("fill", "rgba(255,154,60,0.06)");
    }
  }

  // row labels
  ["ev_charging", "washer_dryer", "water_heater"].forEach((t, i) => {
    svg.append("text")
      .attr("class", "row-label")
      .attr("x", 4)
      .attr("y", ganttBaseY + i * (rowH + rowGap) + rowH * 0.78)
      .text(LOAD_ROW_LABELS[i]);
  });

  // ghost (before) load positions — faint dashed outlines
  const beforeSched = state.transcript.before;
  const ghostLayer = svg.append("g").attr("class", "ghost-layer");
  for (const load of house.shiftable_loads) {
    const startIso = beforeSched[load.load_id];
    if (!startIso) continue;
    const startHr = (new Date(startIso).getTime() - state.simStartMs) / 3600000;
    const row = LOAD_TYPE_ROW[load.type];
    ghostLayer.append("rect")
      .attr("class", "ghost-block")
      .attr("x", xScale(startHr))
      .attr("y", ganttBaseY + row * (rowH + rowGap))
      .attr("width", Math.max(1.5, xScale(load.duration_hours) - xScale(0)))
      .attr("height", rowH);
  }

  // final (after) load positions
  const finalSched = state.transcript.final;
  const blockLayer = svg.append("g").attr("class", "block-layer");
  for (const load of house.shiftable_loads) {
    const startIso = finalSched[load.load_id];
    if (!startIso) continue;
    const startHr = (new Date(startIso).getTime() - state.simStartMs) / 3600000;
    const row = LOAD_TYPE_ROW[load.type];
    blockLayer.append("rect")
      .attr("class", "load-block dim")
      .attr("x", xScale(startHr))
      .attr("y", ganttBaseY + row * (rowH + rowGap))
      .attr("width", Math.max(1.5, xScale(load.duration_hours) - xScale(0)))
      .attr("height", rowH)
      .attr("fill", LOAD_TYPE_COLOR[load.type])
      .style("color", LOAD_TYPE_COLOR[load.type])
      .datum({ start: startHr, end: startHr + load.duration_hours, load });
  }

  // separator between gantt and ribbon
  svg.append("line")
    .attr("class", "tl-zero-line")
    .attr("x1", padLeft).attr("x2", padLeft + innerW)
    .attr("y1", ribbonY - 3).attr("y2", ribbonY - 3);

  // ribbon label
  svg.append("text")
    .attr("class", "row-label")
    .attr("x", 4)
    .attr("y", ribbonY + ribbonH / 2 + 3)
    .text("SYS");

  // solar ribbon (top half of ribbon area)
  const trace = state.perHouseAfter[house.house_id] || {};
  const solar = trace.solar_kw || [];
  if (house.solar && solar.length) {
    const sMax = Math.max(1e-6, d3.max(solar));
    const halfH = ribbonH / 2;
    const yScale = v => ribbonY + halfH - (v / sMax) * halfH;
    const area = d3.area()
      .x((_, i) => xScale(i))
      .y0(ribbonY + halfH)
      .y1((d) => yScale(d))
      .curve(d3.curveStep);
    svg.append("path")
      .datum(solar)
      .attr("class", "solar-ribbon")
      .attr("d", area);
  }

  // battery SOC ribbon (bottom half)
  const soc = trace.battery_soc || [];
  if (house.battery && soc.length) {
    const capacity = house.battery.capacity_kwh;
    const halfH = ribbonH / 2;
    const baseY = ribbonY + ribbonH;
    const yScale = v => baseY - (v / capacity) * halfH;
    const area = d3.area()
      .x((_, i) => xScale(i))
      .y0(baseY)
      .y1((d) => yScale(d))
      .curve(d3.curveStep);
    svg.append("path")
      .datum(soc)
      .attr("class", "battery-ribbon")
      .attr("d", area);
    // outline
    const line = d3.line()
      .x((_, i) => xScale(i))
      .y(d => yScale(d))
      .curve(d3.curveStep);
    svg.append("path")
      .datum(soc)
      .attr("class", "battery-ribbon-outline")
      .attr("d", line);
  } else if (!house.battery) {
    svg.append("text")
      .attr("x", padLeft + 6)
      .attr("y", ribbonY + ribbonH / 2 + 4)
      .attr("fill", "var(--paper-deep)")
      .style("font-family", "var(--font-mono)")
      .style("font-size", "9px")
      .style("letter-spacing", "0.18em")
      .text(house.solar ? "(no battery — solar exports direct)" : "(no on-site generation or storage)");
  }

  // cursor
  const cursor = svg.append("line")
    .attr("class", "cursor-line")
    .attr("y1", 0).attr("y2", H)
    .attr("stroke", "var(--gold)")
    .attr("stroke-width", 1)
    .attr("opacity", 0.85);

  return { svg, xScale, blockLayer, cursor, H, totalW: W };
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
// COORDINATOR + SWAPS
// ===================================================================

let lastRoundShown = -2;

function currentRoundForHour(h) {
  if (!state.transcript.rounds.length) return 0;
  const r = Math.min(state.transcript.rounds.length - 1, Math.floor(h / (state.hours / state.transcript.rounds.length)));
  return r;
}

function updateCoordinator() {
  const r = currentRoundForHour(state.currentHour);
  if (r === lastRoundShown) return;
  lastRoundShown = r;
  const round = state.transcript.rounds[r];
  const body = document.getElementById("coord-body");
  const attr = document.getElementById("coord-attr");
  body.style.opacity = 0;
  requestAnimationFrame(() => {
    body.textContent = round.coordinator_message;
    attr.textContent = `— Coordinator · Round ${round.round_number} of ${state.transcript.rounds.length} · peak ${round.peak_kw.toFixed(1)} kW`;
    body.style.transition = "opacity 360ms ease";
    body.style.opacity = 1;
  });

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
      const span = state.hours / state.transcript.rounds.length;
      state.currentHour = span * i + span / 2;
      syncFromScrub();
      lastRoundShown = -2;
      updateAll();
    });
    pager.appendChild(dot);
  });
}

function renderSwapFeedInitial() {
  // List view (always shows all swaps, dim until "triggered" by cursor crossing).
  const list = document.getElementById("swap-list");
  list.innerHTML = "";
  if (!state.swaps.length) {
    list.innerHTML = '<li class="swap-empty">No bilateral trades in this transcript — coordinator handled the curve alone.</li>';
    document.getElementById("swap-count").textContent = "0";
    return;
  }
  // Show accepted count prominently
  const accepted = state.swaps.filter(s => s.accepted).length;
  document.getElementById("swap-count").textContent = `${accepted}/${state.swaps.length}`;
  state.swaps.forEach((sw, idx) => {
    const li = document.createElement("li");
    li.className = "swap-item" + (sw.accepted ? "" : " rejected");
    li.dataset.swapIdx = idx;
    li.style.opacity = 0.22;
    const arrow = sw.to_hour > sw.from_hour ? "→ later" : "← earlier";
    const delta = Math.abs(sw.to_hour - sw.from_hour).toFixed(0);
    li.innerHTML = `
      <span class="swap-pair">
        <strong>${sw.house_id}</strong>
        <span style="color:var(--gold)">${escapeHtml(prettyLoadId(sw.load_id))}</span>
        <span class="swap-shift">${formatHour(sw.from_hour)} ${arrow} ${formatHour(sw.to_hour)} <em>(${delta}h)</em></span>
      </span>
      ${sw.accepted
        ? (sw.reduced_peak_kw > 0
            ? `<span class="swap-yield">−${sw.reduced_peak_kw.toFixed(2)} kW peak</span>`
            : '<span class="swap-yield" style="color:var(--after)">accepted</span>')
        : '<span class="swap-yield" style="color:var(--paper-mute)">declined</span>'}
      <span class="swap-rationale">${escapeHtml(sw.rationale || "")}</span>
    `;
    list.appendChild(li);
  });
}

function prettyLoadId(id) {
  if (!id) return "";
  // H2-EV-D4 → EV · day 4 (drop the household, it's already in swap-pair)
  const m = id.match(/^H(\d)-([A-Z0-9]+)-D(\d+)$/);
  if (m) return `${m[2]} · day ${parseInt(m[3], 10) + 1}`;
  return id;
}

function formatHour(h) {
  if (h == null) return "—";
  const dayIdx = Math.floor(h / 24);
  const hr = Math.round(h - dayIdx * 24);
  return `D${dayIdx + 1}·${String(hr).padStart(2, "0")}h`;
}

function updateSwapAnimations() {
  for (const hour of Object.keys(state.swapsByHour)) {
    if (state.currentHour < parseFloat(hour)) continue;
    for (const sw of state.swapsByHour[hour]) {
      if (state.swapsShown.has(sw._idx)) continue;
      state.swapsShown.add(sw._idx);
      // brighten list entry
      const li = document.querySelector(`.swap-item[data-swap-idx="${sw._idx}"]`);
      if (li) {
        li.style.transition = "opacity 400ms ease, transform 400ms ease";
        li.style.opacity = 1;
        // briefly pop the item
        li.style.transform = "translateX(4px)";
        setTimeout(() => { li.style.transform = ""; }, 420);
        // ensure it's visible in the scroll container
        li.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
      // draw on-card flash + arc above the affected house card
      drawSwapBurst(sw);
    }
  }
}

function drawSwapBurst(sw) {
  const overlay = document.getElementById("swap-svg");
  if (!overlay) return;
  const grid = document.getElementById("house-grid");
  const overlayBox = grid.getBoundingClientRect();
  const card = grid.querySelector(`.house-card[data-house="${sw.house_id}"]`);
  if (!card) return;
  const a = card.getBoundingClientRect();
  const cx = a.left + a.width / 2 - overlayBox.left;
  const ay = a.top - overlayBox.top + 6;
  const sw_kw_ok = sw.accepted;

  overlay.setAttribute("viewBox", `0 0 ${overlayBox.width} ${overlayBox.height}`);
  overlay.setAttribute("width", overlayBox.width);
  overlay.setAttribute("height", overlayBox.height);

  // small arched arrow showing the load moving (from_hour → to_hour, schematic)
  const span = 90;  // px arc width
  const direction = sw.to_hour > sw.from_hour ? 1 : -1;
  const x0 = cx - direction * span / 2;
  const x1 = cx + direction * span / 2;
  const yArc = ay - 26;

  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", `M ${x0} ${ay} Q ${cx} ${yArc} ${x1} ${ay}`);
  path.setAttribute("class", "swap-arc entering");
  path.style.stroke = sw_kw_ok ? "var(--gold)" : "var(--paper-mute)";
  overlay.appendChild(path);

  // arrowhead at terminus
  const head = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
  const hx = x1, hy = ay;
  const dx = direction * 6;
  head.setAttribute("points", `${hx},${hy} ${hx - dx},${hy - 4} ${hx - dx},${hy + 4}`);
  head.setAttribute("fill", sw_kw_ok ? "var(--gold)" : "var(--paper-mute)");
  head.style.filter = sw_kw_ok ? "drop-shadow(0 0 6px var(--gold))" : "none";
  overlay.appendChild(head);

  // text label below arc
  const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
  label.setAttribute("x", cx);
  label.setAttribute("y", yArc - 4);
  label.setAttribute("text-anchor", "middle");
  label.setAttribute("class", "swap-flash-label");
  label.style.fill = sw_kw_ok ? "var(--gold)" : "var(--paper-mute)";
  const tag = sw_kw_ok
    ? (sw.reduced_peak_kw > 0 ? `swap · −${sw.reduced_peak_kw.toFixed(2)} kW` : "swap · ok")
    : "swap · declined";
  label.textContent = tag;
  overlay.appendChild(label);

  // fade out after a few seconds (keep a faint trace)
  setTimeout(() => {
    path.style.transition = "opacity 1.2s ease";
    label.style.transition = "opacity 1.2s ease";
    head.style.transition = "opacity 1.2s ease";
    path.style.opacity = 0.12;
    label.style.opacity = 0.3;
    head.style.opacity = 0.2;
  }, 2600);
}

// ===================================================================
// CONTROLS
// ===================================================================

function setupControls() {
  const playBtn = document.getElementById("play-btn");
  const playIcon = document.getElementById("play-icon");
  const pauseIcon = document.getElementById("pause-icon");
  const scrub = document.getElementById("scrub");
  scrub.max = state.hours - 1;
  const speedToggle = document.getElementById("speed-toggle");
  const viewToggle = document.getElementById("view-toggle");

  playBtn.addEventListener("click", () => {
    state.isPlaying = !state.isPlaying;
    playIcon.style.display = state.isPlaying ? "none" : "block";
    pauseIcon.style.display = state.isPlaying ? "block" : "none";
    if (state.isPlaying) state.lastTime = performance.now();
  });

  scrub.addEventListener("input", () => {
    state.currentHour = +scrub.value;
    // rewind swaps shown if scrubbing back
    state.swapsShown.clear();
    document.querySelectorAll(".swap-item").forEach(li => li.style.opacity = 0.25);
    const overlay = document.getElementById("swap-svg");
    if (overlay) overlay.innerHTML = "";
    updateAll();
  });

  speedToggle.querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", () => {
      speedToggle.querySelectorAll("button").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      state.speed = +btn.dataset.speed;
    });
  });

  viewToggle.querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", () => {
      viewToggle.querySelectorAll("button").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      state.mode = btn.dataset.view;
      document.body.classList.toggle("mode-compare", state.mode === "compare");
      if (state.mode === "compare") {
        state.isPlaying = false;
        playIcon.style.display = "block";
        pauseIcon.style.display = "none";
        // reveal full after curve
        d3.select("#reveal-clip rect").attr("width", state.chartRefs.innerW);
      } else {
        updateChartCursor();
      }
      updateAll();
    });
  });

  // day-ticks under scrubber
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
// MAIN LOOP
// ===================================================================

function updateAll() {
  document.getElementById("clock").textContent = fmtClock(state.currentHour);
  updateChartCursor();
  updateHouseTimelines();
  updateCoordinator();
  updateSwapAnimations();
  syncFromScrub();
}

function tick(ts) {
  if (state.isPlaying) {
    if (state.lastTime == null) state.lastTime = ts;
    const dt = (ts - state.lastTime) / 1000;
    state.lastTime = ts;
    state.currentHour += dt * state.speed * HOURS_PER_SECOND_1X;
    if (state.currentHour >= state.hours) {
      state.currentHour = 0;
      lastRoundShown = -2;
      state.swapsShown.clear();
      document.querySelectorAll(".swap-item").forEach(li => li.style.opacity = 0.25);
      const overlay = document.getElementById("swap-svg");
      if (overlay) overlay.innerHTML = "";
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
    renderMasthead();
    renderStats();
    renderChart();
    renderCarbonStrip();
    renderHouses();
    renderRoundPager();
    renderSwapFeedInitial();
    setupControls();
    updateAll();

    // auto-play after a short pause so the audience can read the headlines
    setTimeout(() => {
      if (!state.isPlaying && state.mode === "animate") {
        document.getElementById("play-btn").click();
      }
    }, 900);

    // resize handling — re-render layout-dependent svgs
    let resizeTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        renderChart();
        renderCarbonStrip();
        for (const houseId of Object.keys(state.houseRefs)) {
          const house = state.data.houses.find(h => h.house_id === houseId);
          const svg = d3.select(`.house-card[data-house="${houseId}"] svg.tl-svg`);
          state.houseRefs[houseId] = renderHouseTimeline(house, svg);
        }
        // clear stale swap overlay (positions will be re-drawn as time advances)
        const overlay = document.getElementById("swap-svg");
        if (overlay) overlay.innerHTML = "";
        state.swapsShown.clear();
        document.querySelectorAll(".swap-item").forEach(li => li.style.opacity = 0.25);
        updateAll();
      }, 180);
    });

    requestAnimationFrame(tick);
  } catch (err) {
    const body = document.getElementById("coord-body");
    if (body) body.textContent = `Failed to load data. Are you serving from the project root? (${err.message})`;
    console.error(err);
  }
}

init();
