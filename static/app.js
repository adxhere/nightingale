"use strict";
/* Nightingale dashboard. Plain JS, no build step.
   Views mount a static skeleton once per route, then refresh named regions
   only when their HTML changes, so inputs and scroll positions survive polling. */

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => Number(n || 0).toLocaleString("en-US");
const plural = (n, one, many) => `${fmt(n)} ${n === 1 ? one : (many || one + "s")}`;
const HALLS = "ABCDEFGH";

const S = {
  state: null, ok: true, lastOk: null, firstLoad: true,
  logs: [], lastLogId: 0,
  route: { name: "overview", id: null, q: {} },
  fleet: { hall: null, rack: null, server: null, gpu: null, metric: null },
  rack: null, gpuDetail: null,
  incTab: "open",
  logFilter: { q: "", source: "all", warnOnly: false, paused: false },
  analyze: null, analyzing: false,
  sizeChoice: null,
  asks: [], asking: false,
  st: null, stMinutes: 3, auto: null,
  pc: null, pcHist: null, pcHistAt: 0, pcSpan: "1h", pcMetric: "temp", pcConfirm: null, pcParams: {},
  bench: null, benchCount: 12, benchSize: 1024,
  regions: {},
  searchSize: 0,
};

/* ------------------------------------------------------------------ api */
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

let toastTimer;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 4200);
}

/* -------------------------------------------------------------- helpers */
function ago(ts) {
  if (!ts) return "";
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  return `${Math.floor(s / 3600)} h ago`;
}
function clock(ts) { return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
function money(v) { return v < 1 ? `$${(v || 0).toFixed(4)}` : `$${v.toFixed(2)}`; }
function shortModel(id) {
  const m = String(id || "");
  if (/ultra/i.test(m)) return "Nemotron 3 Ultra";
  if (/super/i.test(m)) return "Nemotron 3 Super";
  if (/nano/i.test(m)) return "Nemotron 3 Nano";
  return m.split("/").pop();
}
const LOCAL = "laptop";
const isLocal = (t) => t === LOCAL;
const REASON_TEXT = {
  HW_THERMAL_SLOWDOWN: "Too hot (hardware limit)", SW_THERMAL_SLOWDOWN: "Too hot (driver limit)",
  SW_POWER_CAP: "At its power limit", HW_POWER_BRAKE_SLOWDOWN: "Power brake (charger or battery)",
  HW_SLOWDOWN: "Hardware slowdown", APPLICATIONS_CLOCKS_SETTING: "App clock setting", SYNC_BOOST: "Sync boost",
  DISPLAY_CLOCK_SETTING: "Display clock setting",
};
const hallName = (h) => (h === "L" ? "This computer" : h === "R" ? "Real servers" : `Hall ${h}`);
const realRack = (t) => (S.state && S.state.real && S.state.real.rack_of ? S.state.real.rack_of[t] : null);
const isReal = (t) => !!realRack(t);
function shortGpu(name) { return String(name || "NVIDIA GPU").replace(/^NVIDIA\s+/, "").replace(/^GeForce\s+/, ""); }
function rackOfServer(name) {
  if (isLocal(name)) return "L-01";
  if (isReal(name)) return realRack(name);
  const n = parseInt(String(name).replace(/\D/g, ""), 10);
  if (!n) return null;
  const idx = Math.floor((n - 1) / 8);
  return `${HALLS[Math.floor(idx / 16)]}-${String((idx % 16) + 1).padStart(2, "0")}`;
}
function targetLink(t) {
  if (!t) return "";
  if (isLocal(t)) return "#/pc";
  if (/^dgx-/.test(t) || isReal(t)) return `#/fleet?rack=${rackOfServer(t)}&server=${encodeURIComponent(t)}`;
  return `#/fleet?rack=${t}`;
}
function targetName(t) { return isLocal(t) ? "this laptop" : /^dgx-/.test(t) || isReal(t) ? t : `rack ${t}`; }

function mix(a, b, t) { return a.map((v, i) => Math.round(v + (b[i] - v) * t)); }
const HEAT = [[30, [22, 34, 42]], [60, [30, 58, 70]], [71, [46, 88, 102]], [77, [122, 114, 72]], [82, [214, 140, 52]], [88, [240, 90, 60]], [95, [255, 59, 79]]];
function heatRgb(t) {
  if (t <= HEAT[0][0]) return HEAT[0][1];
  for (let i = 1; i < HEAT.length; i++) {
    if (t <= HEAT[i][0]) return mix(HEAT[i - 1][1], HEAT[i][1], (t - HEAT[i - 1][0]) / (HEAT[i][0] - HEAT[i - 1][0]));
  }
  return HEAT[HEAT.length - 1][1];
}
const rgb = (c) => `rgb(${c.join(",")})`;
function lum(c) {
  const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
}
const ACCENT = [255, 181, 71], WATCH = [255, 122, 198], RED = [255, 77, 94];
function cellColor(code, temp) {
  if (code === "x") return rgb(RED);
  if (code === "w") return rgb(WATCH);
  if (code === "r") return "#2a3239";
  if (code === "d") return rgb(mix(heatRgb(temp), [0, 0, 0], 0.5));
  return rgb(heatRgb(temp));
}

const STATUS = {
  investigating: ["Investigating", "watch"],
  awaiting_approval: ["Waiting for your approval", "attn"],
  needs_attention: ["Needs attention", "bad"],
  verifying: ["Checking the fix", "watch"],
  waiting_on_technician: ["Waiting on repair", ""],
  resolved: ["Resolved", "good"],
  closed: ["Closed by you", ""],
};
const forYou = (inc) => inc.actions.length > 0 && inc.actions.every((a) => a.action === "recommend");
function statusPill(inc) {
  let [label, cls] = STATUS[inc.status] || [inc.status, ""];
  if (inc.status === "awaiting_approval" && forYou(inc)) label = "Steps for you";
  const spin = inc.status === "investigating" || inc.status === "verifying" ? '<span class="spinner" aria-hidden="true"></span>' : "";
  return `<span class="pill ${cls}">${spin}${esc(label)}</span>`;
}

/* --------------------------------------------------------------- gauges */
/* The option's ring gauge: a 270-degree arc, amber when fine, pink under watch, red when critical. */
const LEVEL_COLOR = { ok: "var(--accent)", watch: "var(--watch)", crit: "var(--negative)" };
function ring(g) {
  const f = Math.max(0, Math.min(1, g.frac ?? 0));
  const color = LEVEL_COLOR[g.level || "ok"];
  const big = String(g.center).length > 4 ? 23 : 28;
  return `<svg class="ring" viewBox="0 0 120 120" role="img" aria-label="${esc(`${g.label}: ${g.center} ${g.unit || ""}`)}">
    <circle class="r-track" cx="60" cy="60" r="50" fill="none" stroke-width="10" stroke-linecap="round" stroke-dasharray="235.62 314.16" transform="rotate(135 60 60)"/>
    ${f > 0.002 ? `<circle class="r-value" cx="60" cy="60" r="50" fill="none" stroke="${color}" stroke-width="10" stroke-linecap="round" stroke-dasharray="${(235.62 * f).toFixed(1)} 314.16" transform="rotate(135 60 60)"/>` : ""}
    <text class="r-num" x="60" y="${g.unit ? 64 : 70}" text-anchor="middle" font-size="${big}">${esc(g.center)}</text>
    ${g.unit ? `<text class="r-unit" x="60" y="86" text-anchor="middle" font-size="12">${esc(g.unit)}</text>` : ""}
  </svg>`;
}
/* A vital in the top strip: ring on the left, what it means on the right. */
function vital(g) {
  const tag = g.href ? "a" : "div";
  return `<${tag} class="vital" ${g.href ? `href="${g.href}"` : ""}>${ring(g)}
    <span class="txt"><b>${esc(g.label)}</b><span class="v">${esc(g.value)}</span>${g.sub ? `<span class="s ${g.subLevel || ""}">${esc(g.sub)}</span>` : ""}</span></${tag}>`;
}
function miniGauge(g) { return `<div class="mini-gauge">${ring(g)}<b>${esc(g.label)}</b></div>`; }
function levelFor(value, zones) {
  let level = "ok";
  for (const z of zones) if (value >= z.from && value <= z.to) level = z.level === "crit" ? "crit" : level === "crit" ? level : "watch";
  return level;
}

/* --------------------------------------------------------------- charts */
function lineChart(points, opts) {
  const W = 320, H = 120, L = 34, R = 8, T = 10, B = 22;
  if (!points.length) return `<p class="small faint">No samples yet.</p>`;
  const vals = points.map((p) => p.v);
  let lo = opts.min ?? Math.min(...vals), hi = opts.max ?? Math.max(...vals);
  if (hi - lo < (opts.minSpan || 4)) { const m = (hi + lo) / 2; lo = m - (opts.minSpan || 4) / 2; hi = m + (opts.minSpan || 4) / 2; }
  const step = niceStep((hi - lo) / 3);
  lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;
  if (opts.floor !== undefined) lo = Math.max(opts.floor, lo);
  const x = (i) => L + (i * (W - L - R)) / Math.max(1, points.length - 1);
  const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo || 1));
  const ticks = [];
  for (let v = lo; v <= hi + 1e-9; v += step) ticks.push(v);
  const grid = ticks.map((v) => `<line class="grid-line" x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"/><text class="axis" x="${L - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${fmt(Math.round(v))}</text>`).join("");
  const d = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(p.v).toFixed(1)}`).join(" ");
  const last = points[points.length - 1];
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opts.label)}">
    ${grid}
    <path d="${d}" fill="none" stroke="${opts.color || "var(--accent)"}" stroke-width="2" stroke-linejoin="round"/>
    <circle cx="${x(points.length - 1).toFixed(1)}" cy="${y(last.v).toFixed(1)}" r="3" fill="${opts.color || "var(--accent)"}"/>
    <text class="axis" x="${L}" y="${H - 4}">${opts.leftLabel || `${Math.round((points.length - 1) * 2)} s ago`}</text>
    <text class="axis" x="${W - R}" y="${H - 4}" text-anchor="end">now</text>
  </svg>`;
}
function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1e-9))));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= raw) return m * p;
  return 10 * p;
}

/* ----------------------------------------------------------------- shell */
const ICONS = {
  pc: '<rect x="3.5" y="4" width="13" height="9" rx="1.5"/><path d="M1.5 16h17"/>',
  overview: '<path d="M3 3h6v6H3zM11 3h6v6h-6zM3 11h6v6H3zM11 11h6v6h-6z"/>',
  fleet: '<path d="M4 3h12v5H4zM4 12h12v5H4z"/><path d="M7 5.5h1M7 14.5h1"/>',
  incidents: '<path d="M10 3 2.5 16.5h15z"/><path d="M10 8v4M10 14.2v.3"/>',
  logs: '<path d="M4 5h12M4 9h12M4 13h8M4 17h10"/>',
  sources: '<ellipse cx="10" cy="4.8" rx="6" ry="2.3"/><path d="M4 4.8v10.4c0 1.3 2.7 2.3 6 2.3s6-1 6-2.3V4.8M4 10c0 1.3 2.7 2.3 6 2.3s6-1 6-2.3"/>',
  ask: '<path d="M3.5 4.5h13v9h-7l-4 3v-3h-2z"/><path d="M7 8.2h6M7 11h4"/>',
  benchmark: '<path d="M3 16.5a7 7 0 1 1 14 0"/><path d="m10 16.5 3.2-4.6"/><path d="M5.2 12.4l1 .6M14.8 12.4l-1 .6M10 9.5v1.2"/>',
};
function renderShell() {
  const st = S.state;
  const needs = st ? st.incidents.filter((i) => i.status === "awaiting_approval" || i.status === "needs_attention").length : 0;
  const item = (name, label, count) => `<a href="#/${name}" ${S.route.name === name ? 'aria-current="page"' : ""}>
      <svg viewBox="0 0 20 20" aria-hidden="true">${ICONS[name]}</svg>${label}${count ? `<span class="count" aria-label="${count} waiting for you">${count}</span>` : ""}</a>`;
  const mine = st ? st.incidents.filter((i) => i.target === LOCAL && ["awaiting_approval", "needs_attention"].includes(i.status)).length : 0;
  setRegion("nav", $("#nav"), item("pc", "My computer", mine) + `<div class="nav-group">Data center</div>` + item("overview", "Overview") + item("fleet", "Fleet") + item("incidents", "Incidents", needs) +
    item("logs", "Logs") + item("ask", "Ask") + `<div class="nav-group">Test and setup</div>` + item("benchmark", "Benchmark") +
    item("sources", "Data sources"));
  if (!st) return;
  const live = st.mode === "live";
  setRegion("foot", $("#sidebar-foot"), `
    <dl><dt>Triage, every ${st.agent.scan_seconds} s</dt><dd title="${esc(st.models.triage)}">${esc(shortModel(st.models.triage))}</dd></dl>
    <dl><dt>Investigation</dt><dd title="${esc(st.models.diagnosis)}">${esc(shortModel(st.models.diagnosis))}</dd></dl>
    ${st.model_note ? `<p>${esc(st.model_note)}</p>` : ""}`);
  setRegion("topright", $("#topbar-right"), `
    <span class="chip strong"><span class="dot ${S.ok ? (live ? "" : "demo") : "bad"}"></span><span class="label">${!S.ok ? "Server unreachable" : live ? "Live on Nebius Token Factory" : "Demo mode, offline models"}</span></span>
    ${modeSwitch(true)}
    <span class="chip model">Triage: ${esc(shortModel(st.models.triage))}</span>
    <span class="chip model">Investigation: ${esc(shortModel(st.models.diagnosis))}</span>
    <a class="chip dashed" href="#/sources" title="Data sources">Simulated data center, <b>${fmt(st.size)} GPUs</b>${st.local ? " + this laptop" : ""}${st.real ? ` + <b>${plural(st.real.servers, "real server")}</b>` : ""}</a>`);
  const searchKey = `${st.size}|${st.local ? 1 : 0}|${st.real ? Object.keys(st.real.rack_of || {}).join(",") : ""}`;
  if (S.searchSize !== searchKey) {
    S.searchSize = searchKey;
    const opts = [];
    st.fleet.forEach((h) => h.racks.forEach((r) => opts.push(`rack ${r.id}`)));
    for (let i = 1; i <= st.servers; i++) opts.push(`dgx-${String(i).padStart(4, "0")}`);
    st.jobs.forEach((j) => opts.push(j.id));
    if (st.real) Object.keys(st.real.rack_of || {}).forEach((n) => opts.push(n));
    opts.push(LOCAL);
    $("#search-list").innerHTML = opts.map((o) => `<option value="${o}">`).join("");
  }
}
function setRegion(key, el, html) {
  if (!el || S.regions[key] === html) return;
  S.regions[key] = html;
  el.innerHTML = html;
}

/* ---------------------------------------------------------------- views */
const VIEWS = {};

function banner() {
  if (S.ok) return "";
  const when = S.lastOk ? `Showing data from ${clock(S.lastOk / 1000)}.` : "";
  return `<div class="banner" role="alert"><span>Can't reach the Nightingale server. ${when} Retrying every 2 seconds.</span></div>`;
}

/* Overview: answers "what needs me right now?" */
VIEWS.overview = {
  mount: () => `<div class="page">
    <div id="r-banner"></div>
    <div class="title-row"><div><h1>Overview</h1><p class="lede" id="r-lede"></p></div><span class="fresh" id="r-fresh"></span></div>
    <section class="vitals-strip" aria-label="Data center vitals" id="r-gauges"></section>
    <div class="grid cols">
      <div class="span-7 stack">
        <section class="card" aria-labelledby="q-h">
          <div class="card-head"><h2 id="q-h">Needs you</h2><span class="hint" id="r-qhint"></span></div>
          <div id="r-queue"></div>
        </section>
        <section class="card" aria-labelledby="j-h"><div class="card-head"><h2 id="j-h">Training jobs</h2><span class="hint" id="r-jhint"></span></div><div id="r-jobs"></div></section>
      </div>
      <div class="span-5 stack">
        <section class="card" aria-labelledby="f-h"><div class="card-head"><h2 id="f-h">Fleet</h2><a class="link small" href="#/fleet">Open the fleet</a></div><div id="r-minimap"></div></section>
        <section class="card" aria-labelledby="m-h"><div class="card-head"><h2 id="m-h">Model routing</h2></div><div id="r-routing"></div></section>
      </div>
    </div></div>`,
  update(st) {
    const g = st.gauges;
    const needs = st.incidents.filter((i) => ["awaiting_approval", "needs_attention"].includes(i.status));
    const inv = st.incidents.filter((i) => i.status === "investigating");
    let lede;
    if (needs.length) lede = `<strong>${plural(needs.length, "incident needs", "incidents need")} you.</strong> `;
    else if (inv.length) lede = `<strong>Nothing needs you yet.</strong> `;
    else lede = `<strong>Nothing needs you right now.</strong> `;
    if (inv.length) lede += `Nemotron is investigating ${plural(inv.length, "more")}. `;
    lede += `${fmt(g.healthy)} of ${fmt(g.total)} GPUs healthy.`;
    setRegion("ov-banner", $("#r-banner"), banner());
    setRegion("ov-lede", $("#r-lede"), lede);
    const lt = st.agent.last_triage;
    setRegion("ov-fresh", $("#r-fresh"), lt ? (lt.quiet ? `<span class="dot demo"></span>Checked ${fmt(st.gauges.total)} GPUs ${ago(lt.at)}: nothing new, so no model call` : `<span class="dot ${st.mode === "live" ? "" : "demo"}"></span>Nano scanned ${fmt(st.gauges.total)} GPUs ${ago(lt.at)}`) : `<span class="spinner"></span>Waiting for the first scan`);
    setRegion("ov-qhint", $("#r-qhint"), st.agent.watching.length ? `Keeping an eye on ${esc(st.agent.watching.join(", "))}` : "");
    setRegion("ov-queue", $("#r-queue"), queueHtml(st));
    setRegion("ov-gauges", $("#r-gauges"), vitalsStrip(st).map(vital).join(""));
    setRegion("ov-minimap", $("#r-minimap"), miniMap(st));
    const running = st.jobs.filter((j) => j.status === "running").length;
    setRegion("ov-jhint", $("#r-jhint"), `${running} of ${st.jobs.length} running`);
    setRegion("ov-jobs", $("#r-jobs"), jobsTable(st));
    setRegion("ov-routing", $("#r-routing"), routing(st));
  },
};

function vitalsStrip(st) {
  const g = st.gauges;
  const healthyPct = (g.healthy / g.total) * 100;
  // Goodput: the share of GPUs assigned to training that are doing useful work right now.
  const assigned = st.jobs.reduce((a, j) => a + j.gpus, 0) || 1;
  const useful = st.jobs.reduce((a, j) => a + (j.status === "running" ? j.gpus * Math.min(1, j.rate) : 0), 0);
  const goodput = Math.round((useful / assigned) * 100);
  const stopped = st.jobs.find((j) => j.status !== "running");
  const slowed = st.jobs.filter((j) => j.status === "running" && j.rate < 0.9).sort((a, b) => a.rate - b.rate)[0];
  const gSub = stopped ? `${stopped.id} is ${stopped.status}` : slowed ? `${slowed.id} slowed to ${Math.round(slowed.rate * 100)}%` : "Every job at full speed";
  // Hottest rack, against the other racks that are busy training.
  const racks = st.fleet.flatMap((h) => h.racks);
  const busy = racks.filter((r) => r.busy >= 16);
  const pool = busy.length ? busy : racks;
  const hot = pool.reduce((a, r) => (r.avg > a.avg ? r : a), pool[0]);
  const others = pool.filter((r) => r !== hot);
  const otherAvg = others.length ? Math.round(others.reduce((a, r) => a + r.avg, 0) / others.length) : hot.avg;
  const hotLevel = hot.cooling || hot.avg - otherAvg >= 4 ? "watch" : hot.avg >= 87 ? "crit" : "ok";
  const u = st.usage;
  const spent = u.triage.cost + u.diagnosis.cost;
  return [
    { label: "GPUs healthy", center: `${healthyPct >= 99.95 ? "100" : healthyPct.toFixed(1)}%`, unit: "of GPUs", frac: healthyPct / 100,
      level: healthyPct < 95 ? "crit" : "ok", value: `${fmt(g.healthy)} of ${fmt(g.total)}`,
      sub: g.watch || g.lost ? `${fmt(g.watch)} under watch, ${fmt(g.lost)} down` : "None under watch", subLevel: g.watch || g.lost ? "watch" : "", href: "#/fleet" },
    { label: "Training goodput", center: `${goodput}%`, unit: "useful work", frac: goodput / 100,
      level: goodput < 70 ? "crit" : goodput < 95 ? "watch" : "ok", value: "GPU time spent training",
      sub: gSub, subLevel: stopped ? "bad" : slowed ? "watch" : "" },
    { label: "GPU power", center: fmt(g.power_kw), unit: "kW", frac: g.power_kw / g.budget_kw,
      level: g.power_kw > g.budget_kw * 0.95 ? "crit" : g.power_kw > g.budget_kw * 0.85 ? "watch" : "ok",
      value: `of a ${fmt(g.budget_kw)} kW budget`, sub: `${Math.round((g.power_kw / g.budget_kw) * 100)}% in use` },
    { label: "Hottest rack", center: `${hot.avg}°`, unit: "C average", frac: hot.avg / 100, level: hotLevel,
      value: `Rack ${hot.id}`, sub: hot.cooling ? "Coolant flow is low" : `Others average ${otherAvg}°C`, subLevel: hot.cooling ? "watch" : "",
      href: `#/fleet?rack=${hot.id}` },
    { label: "Saved by routing", center: `${u.saved_pct}%`, unit: "saved", frac: u.saved_pct / 100, level: "ok",
      value: "Nano scans, Ultra investigates", sub: `${money(spent)} versus ${money(u.ultra_only_cost)}` },
  ];
}

function queueHtml(st, opts = {}) {
  const order = { awaiting_approval: 0, needs_attention: 1, investigating: 2, verifying: 3 };
  const items = st.incidents.filter((i) => i.status in order).sort((a, b) => order[a.status] - order[b.status] || b.id - a.id);
  const cards = items.map((inc) => {
    const top = `<div class="q-top">${statusPill(inc)}<a class="link" href="${targetLink(inc.target)}">${esc(targetName(inc.target))}${inc.gpu !== null && inc.gpu !== undefined ? `, GPU ${inc.gpu}` : ""}</a><span>Opened ${ago(inc.opened_at)}</span></div>`;
    const title = `<h3 class="q-title"><a href="#/incidents/${inc.id}">${esc(inc.title || inc.trigger.signal)}</a></h3>`;
    let body = "";
    if (inc.status === "investigating") {
      const last = inc.trace[inc.trace.length - 1];
      body = `<p class="q-sum">${esc(inc.trigger.signal)}. ${esc(shortModel(inc.models.diagnosis))} is working through it with read-only tools.</p>
        <div class="q-live"><span class="spinner" aria-hidden="true"></span>${last ? `Step ${last.n}: <code>${esc(callText(last))}</code>` : "Reading the triage evidence"}</div>`;
    } else if (inc.status === "awaiting_approval") {
      const next = inc.actions.find((a) => a.status === "pending");
      const left = inc.actions.filter((a) => a.status === "pending").length;
      const dry = inc.dry_run ? dryPill(inc.dry_run.verdict, inc.dry_run.checked) : "";
      const locked = next.mode === "serious" && pcMode().mode !== "serious";
      const go = locked ? `<button class="btn primary" type="button" data-action="pc-mode" data-id="serious" data-inc="${inc.id}">Turn on serious mode</button><button class="btn" type="button" data-action="skip" data-id="${inc.id}">Skip this step</button>`
        : `<button class="btn primary" type="button" data-action="approve" data-id="${inc.id}">${next.action === "recommend" ? "I've done this" : next.admin ? "Approve (Windows asks)" : "Approve"}</button>`;
      body = `<p class="q-sum">${esc(inc.situation || inc.summary)}</p>
        <div class="q-next"><div class="step"><b>${next.action === "recommend" ? "Suggestion" : "Next"}: ${esc(next.label)}</b>${riskTag(next)} <span class="faint small">Step ${inc.actions.indexOf(next) + 1} of ${inc.actions.length}</span> ${next.action === "recommend" ? "" : dry}</div>
        ${go}
        <a class="btn" href="#/incidents/${inc.id}">Review plan${left > 1 ? ` (${left} steps)` : ""}</a></div>`;
    } else if (inc.status === "needs_attention") {
      body = `<p class="q-sum">${esc(inc.error || inc.verification || inc.summary || "The automatic plan could not finish.")}</p>
        <div class="q-next">${stuckButtons(inc)}<a class="btn quiet" href="#/incidents/${inc.id}">Open incident</a></div>`;
    } else {
      body = `<p class="q-sum">All steps are done. Re-checking telemetry on ${esc(targetName(inc.target))}.</p>`;
    }
    return `<article class="q-item ${inc.status === "awaiting_approval" ? "waiting" : ""}">${top}${title}${body}</article>`;
  });
  const queue = cards.length ? `<div class="queue">${cards.join("")}</div>` : opts.empty ? opts.empty :
    `<div class="empty"><b>Nothing needs you.</b><span>Nemotron 3 Nano scans every GPU every ${st.agent.scan_seconds} seconds and flags anything unusual. To see Nightingale at work, <a class="link" href="#/sources">run a failure drill</a>.</span></div>`;
  const open = st.tickets.filter((t) => t.status === "open");
  const repairs = open.length ? `<hr class="divider"><div class="card-head"><h2>Waiting on repairs</h2><a class="link small" href="#/incidents" data-action="tab" data-tab="tickets">All tickets</a></div>
    <div class="mini-list">${open.slice(-4).reverse().map((t) => `<div><span class="what"><a class="link" href="${targetLink(t.node || t.rack)}"><b>${esc(t.id)}</b></a> <span class="muted small">${esc(t.summary)}</span></span>
      <button class="btn small" type="button" data-action="repair" data-id="${esc(t.id)}" title="In this simulation, marks the on-site repair as finished">Mark repaired</button></div>`).join("")}</div>` : "";
  const done = st.incidents.filter((i) => i.status === "resolved" || i.status === "waiting_on_technician").slice(0, 3);
  const recent = done.length ? `<hr class="divider"><div class="card-head"><h2>Recently handled</h2><a class="link small" href="#/incidents" data-action="tab" data-tab="closed">History</a></div>
    <div class="mini-list">${done.map((i) => `<div><span class="what"><a href="#/incidents/${i.id}">${esc(i.title)}</a></span>${statusPill(i)}</div>`).join("")}</div>` : "";
  return queue + repairs + recent;
}
function riskTag(a) {
  if (a.action === "recommend") return '<span class="pill">For you</span>';
  return `<span class="risk ${a.risk}">${a.risk} risk</span>${a.mode === "serious" ? ' <span class="pill serious">Serious</span>' : ""}`;
}
function dryPill(v, checked) {
  if (checked) return v === "fail" ? '<span class="pill bad">A step can\'t run</span>' : '<span class="pill good">Checked</span>';
  if (v === "pass") return '<span class="pill good">Dry run passed</span>';
  if (v === "warn") return '<span class="pill attn">Dry run: warnings</span>';
  return '<span class="pill bad">Dry run failed</span>';
}
function callText(t) {
  const args = Object.entries(t.args || {}).map(([k, v]) => `${k}=${typeof v === "string" ? `"${v}"` : v}`).join(", ");
  return `${t.tool}(${args})`;
}

function miniMap(st) {
  const halls = st.fleet.map((h) => `<div>${st.fleet.length > 1 ? `<div class="hall-label">${hallName(h.hall)}</div>` : ""}
    <div class="racks mini">${h.racks.map((r) => rackButton(r, false, true)).join("")}</div></div>`).join("");
  return `<div class="halls">${halls}</div><div class="legend" style="margin-top:12px">${legendHtml(true)}</div>`;
}
function rackButton(r, selected, mini) {
  const cells = r.temps.map((t, i) => `<span style="--c:${cellColor(r.codes[i], t)}"></span>`).join("");
  if (r.local) return localRackButton(r, selected, mini, cells);
  if (r.real) return realRackButton(r, selected, mini, cells);
  const cls = r.lost && !r.watch ? "down" : r.watch || r.cooling ? "alert" : "";
  const issues = [r.lost ? `${r.lost} down` : "", r.watch ? `${r.watch} under watch` : "", r.cooling ? "cooling problem" : ""].filter(Boolean).join(", ") || "all healthy";
  const label = `Rack ${r.id}, average ${r.avg} degrees, ${issues}`;
  const dot = r.lost ? "bad" : r.watch || r.cooling ? "watch" : "";
  const head = mini ? `<span class="rack-head"><b>${r.id.slice(2)}</b></span>` : `<span class="rack-head"><b>${r.id}</b><span class="rdot ${dot}"></span></span>`;
  const foot = mini ? "" : `<span class="rack-foot">${r.avg}°C average</span>`;
  const attrs = mini ? `href="#/fleet?rack=${r.id}"` : `type="button" data-action="rack" data-id="${r.id}" aria-pressed="${selected}"`;
  const tag = mini ? "a" : "button";
  return `<${tag} class="rack ${cls}" ${attrs} aria-label="${esc(label)}" title="${esc(label)}">${head}<span class="cells">${cells}</span>${foot}</${tag}>`;
}
function localRackButton(r, selected, mini, cells) {
  const state = r.lost ? "not answering" : r.watch ? "under watch" : `${r.avg} degrees`;
  const label = `This computer, ${shortGpu(r.model)}, ${state}`;
  const dot = r.lost ? "bad" : r.watch ? "watch" : "";
  const cls = r.lost ? "down" : r.watch ? "alert" : "";
  const head = mini ? `<span class="rack-head"><b>PC</b></span>` : `<span class="rack-head"><b>Laptop</b><span class="rdot ${dot}"></span></span>`;
  const foot = mini ? "" : `<span class="rack-foot">${esc(shortGpu(r.model))}, ${r.avg}°C</span>`;
  const attrs = mini ? `href="#/fleet?rack=L-01&server=laptop&gpu=0"` : `type="button" data-action="rack" data-id="${r.id}" aria-pressed="${selected}"`;
  const tag = mini ? "a" : "button";
  return `<${tag} class="rack local ${cls}" ${attrs} aria-label="${esc(label)}" title="${esc(label)}">${head}<span class="cells n${r.temps.length}">${cells}</span>${foot}</${tag}>`;
}
function realRackButton(r, selected, mini, cells) {
  const issues = [r.lost ? `${r.lost} not reporting` : "", r.watch ? `${r.watch} under watch` : ""].filter(Boolean).join(", ") || "all healthy";
  const label = `Real servers, rack ${r.id} (${r.label}), ${plural(r.servers, "server")}, ${issues}`;
  const dot = r.lost ? "bad" : r.watch ? "watch" : "";
  const cls = r.lost && !r.watch ? "down" : r.watch ? "alert" : "";
  const head = mini ? `<span class="rack-head"><b>${r.id}</b></span>` : `<span class="rack-head"><b>${r.id}</b><span class="tag-real">Real</span><span class="rdot ${dot}"></span></span>`;
  const foot = mini ? "" : `<span class="rack-foot">${esc(r.label)}, ${plural(r.servers, "server")}</span>`;
  const attrs = mini ? `href="#/fleet?rack=${r.id}"` : `type="button" data-action="rack" data-id="${r.id}" aria-pressed="${selected}"`;
  const tag = mini ? "a" : "button";
  return `<${tag} class="rack real ${cls}" ${attrs} aria-label="${esc(label)}" title="${esc(label)}">${head}<span class="cells n${Math.min(r.temps.length, 16)}">${cells}</span>${foot}</${tag}>`;
}
function legendHtml(compact) {
  return `<span>${compact ? "" : "GPU temperature"}<span class="ramp" aria-hidden="true"></span>30°C to 95°C</span>
    <span><i class="sw" style="background:${rgb(WATCH)}"></i>Under watch</span>
    <span><i class="sw" style="background:${rgb(RED)}"></i>Down</span>${compact ? "" : `<span><i class="sw ring"></i>Selected rack</span>`}`;
}

function jobsTable(st) {
  const rows = st.jobs.map((j) => {
    if (j.real) {
      const rp = j.status === "running" ? '<span class="pill">Running</span>' : j.status === "restarting" ? '<span class="pill"><span class="spinner"></span>Restarting</span>'
        : j.status === "pending" ? '<span class="pill attn">Pending</span>' : '<span class="pill bad">Crashing</span>';
      return `<tr><td><b>${esc(j.id)}</b><span class="sub">namespace ${esc(j.owner)} <span class="tag-real">Real</span></span></td>
        <td class="r">${fmt(j.gpus)}<span class="sub">${plural(j.servers, "server")}</span></td>
        <td>${rp}${j.last_error ? `<span class="sub">${esc(j.last_error)}</span>` : ""}</td>
        <td class="hide-sm"><span class="sub">Kubernetes workload</span></td><td class="r hide-sm"><span class="sub">not reported</span></td><td class="r hide-sm"><span class="sub">its own checkpoints</span></td></tr>`;
    }
    const pill = j.status === "running" ? (j.rate < 0.9 ? '<span class="pill attn">Slowed</span>' : '<span class="pill">Running</span>')
      : j.status === "restarting" ? '<span class="pill"><span class="spinner"></span>Restarting</span>'
      : `<span class="pill bad">${j.status === "failed" ? "Failed" : "Crashed"}</span>`;
    const pct = Math.round(j.rate * 100);
    const cls = j.status !== "running" ? "bad" : pct < 90 ? "attn" : "";
    return `<tr><td><b>${esc(j.id)}</b><span class="sub">${esc(j.owner)}</span></td>
      <td class="r">${fmt(j.gpus)}<span class="sub">${fmt(j.servers)} servers</span></td>
      <td>${pill}${j.last_error ? `<span class="sub">${esc(j.last_error)}</span>` : ""}</td>
      <td class="hide-sm"><span class="bar"><i class="${cls}" style="width:${pct}%"></i></span><span class="sub">${pct}% of baseline</span></td>
      <td class="r hide-sm">${fmt(j.throughput)}<span class="sub">tokens/s per GPU</span></td>
      <td class="r hide-sm">${fmt(j.step)}<span class="sub">checkpoint ${fmt(j.checkpoint)}</span></td></tr>`;
  }).join("");
  return `<div class="table-wrap"><table class="collapse"><thead><tr><th>Job</th><th class="r">GPUs</th><th>Status</th><th class="hide-sm">Speed</th><th class="r hide-sm">Throughput</th><th class="r hide-sm">Step</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function routing(st) {
  const u = st.usage;
  const total = u.triage.cost + u.diagnosis.cost + (u.ask ? u.ask.cost : 0);
  const share = total ? Math.round((u.triage.cost / total) * 100) : 50;
  const demo = st.mode !== "live";
  return `<div class="route-rows">
    <div class="route-row"><b>${esc(shortModel(st.models.triage))}</b><b class="num">${money(u.triage.cost)}</b>
      <span class="muted">Scans every GPU every ${st.agent.scan_seconds} s: ${plural(u.triage.calls, "scan")}, ${fmt(u.triage.in + u.triage.out)} tokens</span><span class="muted">${share}% of spend</span></div>
    <div class="route-row"><b>${esc(shortModel(st.models.diagnosis))}</b><b class="num">${money(u.diagnosis.cost)}</b>
      <span class="muted">Only investigates incidents: ${plural(u.diagnosis.calls, "call")}, ${fmt(u.diagnosis.in + u.diagnosis.out)} tokens</span><span class="muted">${100 - share}% of spend</span></div>
    ${u.ask && u.ask.calls ? `<div class="route-row"><b>Ask your data center</b><b class="num">${money(u.ask.cost)}</b>
      <span class="muted">${esc(shortModel(st.models.diagnosis))} answering your questions: ${plural(u.ask.calls, "call")}</span><span class="muted"></span></div>` : ""}
    <div class="split" aria-hidden="true"><i style="flex:${share};background:var(--accent)"></i><i style="flex:${100 - share};background:var(--dim)"></i></div>
    ${st.agent.windows ? `<p class="small faint">${fmt(st.agent.quiet_windows)} of ${plural(st.agent.windows, "scan window")} had nothing new, so no model was called. Nemotron runs only when needed.</p>` : ""}
    <div class="saved"><span class="fig">${u.saved_pct}% saved</span><span class="muted">${money(total)} so far, versus ${money(u.ultra_only_cost)} if Ultra read every scan. Healthy scans never touch the large model.</span></div>
    <p class="small faint">${demo ? "Demo mode: tokens and costs are estimates." : "Costs use the per-token prices in your settings."}</p></div>`;
}

/* Fleet: every rack, then one rack's servers, then one GPU. */
VIEWS.fleet = {
  mount: () => `<div class="page">
    <div id="r-banner"></div>
    <div class="title-row"><div><h1>Fleet</h1><p class="lede" id="r-flede"></p></div><div id="r-halls"></div></div>
    <section class="card" aria-label="Racks"><div id="r-racks"></div><div class="legend" style="margin-top:14px">${legendHtml(false)}</div></section>
    <section class="card split-card" aria-labelledby="rk-h">
      <div class="pane"><div class="card-head"><h2 id="rk-h">Rack</h2><span class="hint" id="r-rkhint"></span></div><div id="r-servers"><div class="sk-line"></div><div class="sk-line"></div></div></div>
      <div class="pane" id="r-gpu"><h2 id="gp-h" class="sr-only">GPU</h2><p class="muted">Select a GPU to see its vitals.</p></div>
    </section></div>`,
  update(st) {
    const g = st.gauges;
    setRegion("fl-banner", $("#r-banner"), banner());
    const simHalls = st.halls.filter((h) => h !== "L" && h !== "R").length;
    setRegion("fl-lede", $("#r-flede"), `${fmt(st.size)} GPUs in ${fmt(st.servers)} servers and ${st.racks} racks${simHalls > 1 ? ` across ${simHalls} halls` : ""}${st.local ? ", plus this laptop's GPU" : ""}${st.real ? `, plus ${plural(st.real.servers, "real server")} (${plural(st.real.gpus, "GPU")}) in hall R` : ""}. ${fmt(g.watch)} under watch, ${fmt(g.lost)} down.`);
    if (!S.fleet.hall || !st.halls.includes(S.fleet.hall)) S.fleet.hall = st.halls[0];
    if (S.fleet.rack && S.fleet.rack[0] !== S.fleet.hall && st.halls.includes(S.fleet.rack[0])) S.fleet.hall = S.fleet.rack[0];
    setRegion("fl-halls", $("#r-halls"), st.halls.length > 1 ? `<div class="seg" role="group" aria-label="Hall">${st.halls.map((h) => `<button type="button" data-action="hall" data-id="${h}" aria-pressed="${h === S.fleet.hall}">${hallName(h)}</button>`).join("")}</div>` : "");
    const hall = st.fleet.find((h) => h.hall === S.fleet.hall) || st.fleet[0];
    if (!S.fleet.rack || !st.fleet.some((h) => h.racks.some((r) => r.id === S.fleet.rack))) S.fleet.rack = defaultRack(st);
    setRegion("fl-racks", $("#r-racks"), `<div class="racks">${hall.racks.map((r) => rackButton(r, r.id === S.fleet.rack, false)).join("")}</div>`);
    loadRack();
  },
};
function defaultRack(st) {
  const inc = st.incidents.find((i) => i.status !== "resolved");
  if (inc) return inc.rack;
  for (const h of st.fleet) for (const r of h.racks) if (r.watch || r.lost) return r.id;
  return st.fleet[0].racks[0].id;
}
let rackBusy = false;
async function loadRack() {
  if (rackBusy || S.route.name !== "fleet" || !S.fleet.rack) return;
  rackBusy = true;
  try {
    const [rack, gpu] = await Promise.all([
      api(`/api/rack/${S.fleet.rack}`),
      S.fleet.server ? api(`/api/gpu/${encodeURIComponent(S.fleet.server)}/${S.fleet.gpu ?? 0}`).catch(() => null) : null,
    ]);
    S.rack = rack;
    if (!S.fleet.server || !rack.servers.some((s) => s.name === S.fleet.server)) {
      const all = rack.servers.flatMap((s) => s.gpus.map((g) => ({ s, g })));
      const pick = all.find(({ g }) => g.lost || g.watch || g.sbe_rising || g.nvlink_rising || g.temp >= 83) || all[0] ||
        { s: rack.servers[0], g: { index: 0 } };
      S.fleet.server = pick.s.name; S.fleet.gpu = pick.g.index;
      S.gpuDetail = await api(`/api/gpu/${encodeURIComponent(S.fleet.server)}/${S.fleet.gpu}`).catch(() => null);
    } else {
      S.gpuDetail = gpu;
    }
    renderRack();
  } catch (e) { /* banner covers it */ } finally { rackBusy = false; }
}
function renderRack() {
  const rack = S.rack;
  if (!rack || S.route.name !== "fleet") return;
  $("#rk-h").textContent = rack.local ? "This computer" : rack.real ? `Real servers, rack ${rack.id}` : `Rack ${rack.id}`;
  const kube = S.state && S.state.real && S.state.real.kube;
  const flow = rack.local ? "Real readings from nvidia-smi every 2 s. Fixes need your approval." :
    rack.real ? `Real DCGM readings${kube ? " and Kubernetes. Steps need your approval and are rehearsed with a Kubernetes dry run." : ". Kubernetes isn't connected, so Nightingale only watches."}` :
    rack.flow < 90 ? `<span class="pill attn">Coolant flow ${rack.flow}%</span>` : `${rack.cdu}: flow ${rack.flow}%, return ${rack.return_temp}°C`;
  setRegion("fl-rkhint", $("#r-rkhint"), flow);
  const rows = rack.servers.map((s) => {
    let state = s.local ? shortGpu(s.model) : s.jobs.join(", ") || (s.real ? `${shortGpu(s.model)}, idle` : "Spare, ready"), cls = s.jobs.length || s.local || s.real ? "" : "ok";
    if (s.status === "drained") { state = s.real ? (s.ticket ? "Cordoned, waiting on repair" : "Cordoned") : s.ticket ? "Drained, waiting on repair" : "Drained"; cls = "attn"; }
    if (s.real && s.ready === false) { state = "NotReady in Kubernetes"; cls = "bad"; }
    if (s.status === "rebooting") { state = "Rebooting"; cls = "attn"; }
    if (s.gpus.some((g) => g.lost)) cls = "bad";
    const tiles = s.gpus.map((g) => {
      const code = s.local || s.real ? (g.lost ? "x" : g.watch ? "w" : s.status === "drained" ? "d" : "b") :
        g.lost ? "x" : (g.sbe_rising || g.nvlink_rising || g.remap_pending || g.throttle === "HW_THERMAL_SLOWDOWN" || g.temp >= 83) ? "w" : s.status === "rebooting" ? "r" : s.status === "drained" ? "d" : "b";
      const bg = cellColor(code, g.temp);
      const c = code === "x" ? RED : code === "w" ? WATCH : heatRgb(g.temp);
      const fg = code === "x" || code === "w" || lum(c) > 0.22 ? "#05070a" : "#ecf0f2";
      const sel = s.name === S.fleet.server && g.index === S.fleet.gpu;
      const label = `${s.name} GPU ${g.index}, ${g.lost ? "not responding" : `${Math.round(g.temp)} degrees, ${g.power} watts`}${code === "w" ? `, under watch${g.why ? `: ${g.why}` : ""}` : ""}`;
      return `<button class="tile" type="button" style="--c:${bg};--fg:${fg}" data-action="gpu" data-server="${s.name}" data-gpu="${g.index}" aria-pressed="${sel}" aria-label="${esc(label)}">${g.lost ? "Lost" : `${Math.round(g.temp)}°`}</button>`;
    }).join("");
    return `<div class="server"><div><div class="name">${s.local ? "Laptop" : esc(s.name)}</div><div class="state ${cls}">${esc(state)}</div></div><div class="tiles">${tiles}</div></div>`;
  }).join("");
  let extra = "";
  if (rack.local) {
    const lines = S.logs.filter((l) => l.node === LOCAL).slice(-7).reverse();
    extra = `<hr class="divider"><div class="card-head"><h3>What Nightingale watches here</h3></div>
      <dl class="watchlist">
        <div><dt>Heat slowdown</dt><dd>The driver cutting clocks because the GPU reached its temperature limit.</dd></div>
        <div><dt>Memory</dt><dd>GPU memory over 95% full while a program is running.</dd></div>
        <div><dt>Driver crashes</dt><dd>Windows display-driver errors: nvlddmkm events and Event 4101.</dd></div>
        <div><dt>A missing GPU</dt><dd>nvidia-smi losing contact with the GPU.</dd></div>
      </dl>
      <hr class="divider"><div class="card-head"><h3>Latest from this laptop</h3><a class="link small" href="#/logs">All logs</a></div>
      <div class="logbox mini">${lines.length ? lines.map((l) => `<div class="logline ${l.level}"><span class="ts">${esc(l.t)}</span> <span class="src">${esc(l.source)}:</span> ${esc(l.text)}</div>`).join("") : '<div class="logline">Nothing logged yet.</div>'}</div>`;
  }
  if (rack.real) {
    const names = new Set(rack.servers.map((s) => s.name));
    const lines = S.logs.filter((l) => names.has(l.node)).slice(-7).reverse();
    extra = `<hr class="divider"><div class="card-head"><h3>What Nightingale watches on real servers</h3></div>
      <dl class="watchlist">
        <div><dt>Xid errors</dt><dd>New driver errors DCGM reports, like 79 (GPU fell off the bus) or 48 and 94/95 (memory).</dd></div>
        <div><dt>Memory health</dt><dd>Uncorrectable ECC errors, corrected errors that keep rising, and pending row remaps.</dd></div>
        <div><dt>Heat and links</dt><dd>Heat slowdown under load, and NVLink errors that keep rising.</dd></div>
        <div><dt>Kubernetes</dt><dd>Nodes going NotReady, GPUs that stop reporting, and GPU workloads that crash or restart.</dd></div>
      </dl>
      <hr class="divider"><div class="card-head"><h3>Latest from these servers</h3><a class="link small" href="#/logs">All logs</a></div>
      <div class="logbox mini">${lines.length ? lines.map((l) => `<div class="logline ${l.level}"><span class="ts">${esc(l.t)}</span> <span class="src">${esc(l.node)} ${esc(l.source)}:</span> ${esc(l.text)}</div>`).join("") : '<div class="logline">Nothing logged yet.</div>'}</div>`;
  }
  setRegion("fl-servers", $("#r-servers"), `<div class="servers">${rows}</div>${extra}`);
  renderGpu();
}
function renderGpu() {
  const d = S.gpuDetail;
  if (!d) return;
  if (d.local) return renderLocalGpu(d);
  if (d.real) return renderRealGpu(d);
  const busy = !!d.job;
  const tLevel = d.temp >= 87 ? "crit" : d.temp >= 83 ? "watch" : "ok";
  const mg = d.lost ? [] : [
    { label: "Temperature", center: `${Math.round(d.temp)}°`, unit: "C", frac: d.temp / 100, level: tLevel },
    { label: "Power", center: fmt(d.power), unit: d.power_limit < 700 ? `W, cap ${d.power_limit}` : "W", frac: d.power / 700, level: d.power_limit < 700 ? "watch" : "ok" },
    { label: "SM clock", center: (d.clock / 1000).toFixed(2), unit: "GHz", frac: d.clock / 1980, level: busy && d.clock < 1900 ? "watch" : "ok" },
  ];
  const metrics = [["temp", "Temp", "temperature"], ["power", "Power", "power"], ["clock", "Clock", "SM clock"], ["sbe", "Memory", "corrected memory errors"], ["nvlink_crc", "NVLink", "NVLink errors"]];
  const metric = S.fleet.metric || (d.sbe ? "sbe" : d.nvlink_crc ? "nvlink_crc" : "temp");
  const m = metrics.find((x) => x[0] === metric);
  const pts = d.history.map((h) => ({ v: h.lost ? 0 : h[metric] }));
  const inc = (S.state?.incidents || []).find((i) => (i.target === d.node || i.target === d.rack) && i.status !== "resolved");
  const memory = d.incidents.length ? `<details class="plain"><summary>Fleet memory: ${plural(d.incidents.length, "earlier incident")}</summary><ul class="timeline">${d.incidents.map((h) => `<li>${esc(h.when)}: ${esc(h.title)}. ${esc(h.fix)}</li>`).join("")}</ul></details>` : `<p class="small faint">Fleet memory: no incidents on this GPU in the last 30 days.</p>`;
  setRegion("fl-gpu", $("#r-gpu"), `<div class="vitals">
    <div><h2 id="gp-h" style="font-size:17px">${d.node}, GPU ${d.index}</h2><p class="small faint">H100 SXM in rack ${d.rack}, PCI ${d.pci}. ${d.job ? `Running ${esc(d.job)}.` : d.status === "drained" ? "Drained." : "Idle."}</p></div>
    ${d.lost ? `<div class="callout bad">This GPU fell off the PCIe bus and is not reporting. ${inc ? "" : "A reboot or a repair brings it back."}</div>` : `<div class="mini-gauges">${mg.map(miniGauge).join("")}</div>`}
    <div class="stats">
      <div class="stat"><div class="k">Corrected memory errors</div><div class="v ${d.sbe_rising ? "attn" : ""}">${fmt(d.sbe)}${d.sbe_rising ? " and rising" : ""}</div></div>
      <div class="stat"><div class="k">Uncorrectable errors</div><div class="v ${d.dbe ? "bad" : ""}">${fmt(d.dbe)}${d.remap_pending ? ", remap pending" : ""}</div></div>
      <div class="stat"><div class="k">NVLink errors</div><div class="v ${d.nvlink_rising ? "attn" : ""}">${fmt(d.nvlink_crc)}${d.nvlink_rising ? " and rising" : ""}</div></div>
      <div class="stat"><div class="k">Throttle reason</div><div class="v ${d.throttle === "HW_THERMAL_SLOWDOWN" ? "attn" : ""}" style="font-size:15px">${esc(d.throttle === "none" ? "None" : d.throttle)}</div></div>
    </div>
    <div><div class="seg" role="group" aria-label="History metric">${metrics.map(([k, l]) => `<button type="button" data-action="metric" data-id="${k}" aria-pressed="${k === metric}">${l}</button>`).join("")}</div>
      <div style="margin-top:10px">${lineChart(pts, { label: `GPU ${m[2]} over the last minute`, floor: 0, minSpan: metric === "temp" ? 10 : 4, color: (metric === "sbe" && d.sbe_rising) || (metric === "nvlink_crc" && d.nvlink_rising) || (metric === "temp" && d.temp >= 83) ? "var(--watch)" : "var(--accent)" })}</div></div>
    ${memory}
    ${inc ? `<a class="btn" href="#/incidents/${inc.id}">Open the incident on ${esc(targetName(inc.target))}</a>` : ""}
  </div>`);
}

const XID_TEXT = { 13: "graphics engine exception", 31: "memory page fault", 43: "GPU stopped processing", 45: "cleanup after an earlier error",
  48: "double-bit ECC error", 61: "micro-controller breakpoint", 62: "micro-controller halt", 63: "row remap recorded", 64: "row remap failed",
  74: "NVLink error", 79: "fell off the bus", 92: "high single-bit ECC rate", 94: "contained ECC error", 95: "uncontained ECC error",
  109: "context switch timeout", 119: "GSP timeout", 120: "GSP error", 154: "recovery action changed" };
function renderRealGpu(d) {
  const thermal = (d.reasons || []).some((r) => r.includes("THERMAL")) && (d.util >= 30 || d.reasons.includes("HW_THERMAL_SLOWDOWN"));
  const maxW = d.power_max || d.power_limit || 1;
  const mg = d.lost ? [] : [
    { label: "Temperature", center: `${Math.round(d.temp)}°`, unit: "C", frac: d.temp / 100, level: d.temp >= 87 ? "crit" : d.temp >= 83 || thermal ? "watch" : "ok" },
    { label: "Power", center: fmt(d.power), unit: d.power_limit ? `W of ${d.power_limit}` : "W", frac: d.power / maxW, level: "ok" },
    { label: "SM clock", center: (d.clock / 1000).toFixed(2), unit: "GHz", frac: d.max_clock ? d.clock / d.max_clock : 0, level: thermal ? "watch" : "ok" },
  ];
  const metrics = [["temp", "Temp", "temperature"], ["power", "Power", "power"], ["clock", "Clock", "SM clock"], ["util", "Load", "load"], ["sbe", "Memory", "corrected memory errors"]];
  const metric = metrics.some((m) => m[0] === S.fleet.metric) ? S.fleet.metric : d.sbe_rising ? "sbe" : "temp";
  const m = metrics.find((x) => x[0] === metric);
  const pts = d.history.map((h) => ({ v: h.lost ? 0 : h[metric] }));
  const inc = (S.state?.incidents || []).find((i) => (i.target === d.node || i.target === d.rack) && !["resolved", "closed"].includes(i.status));
  const recent = d.xid_at && Date.now() / 1000 - d.xid_at < 180;
  const xid = d.xid ? `${d.xid} (${XID_TEXT[d.xid] || "see NVIDIA's Xid list"})${d.xid_at ? `, ${ago(d.xid_at)}` : ", before Nightingale connected"}` : "None";
  const reasons = (d.reasons || []).map((r) => REASON_TEXT[r] || r);
  const memory = d.incidents.length ? `<details class="plain"><summary>Fleet memory: ${plural(d.incidents.length, "earlier incident")}</summary><ul class="timeline">${d.incidents.map((h) => `<li>${esc(h.when)}: ${esc(h.title)}. ${esc(h.fix)}</li>`).join("")}</ul></details>` : `<p class="small faint">Fleet memory: no incidents on this GPU yet.</p>`;
  setRegion("fl-gpu", $("#r-gpu"), `<div class="vitals">
    <div><h2 id="gp-h" style="font-size:17px">${esc(d.node)}, GPU ${d.index} <span class="tag-real">Real</span></h2><p class="small faint">${esc(d.model)}${d.driver ? `, driver ${esc(d.driver)}` : ""}, rack ${esc(d.rack)}${d.pci ? `, PCI ${esc(d.pci)}` : ""}. Kubernetes node ${esc(d.kname)}${d.ready === false ? ", NotReady" : ""}. ${d.job ? `Running ${esc(d.job)}${d.pod ? ` (pod ${esc(d.pod)})` : ""}.` : d.status === "drained" ? "Cordoned." : "Idle."}</p></div>
    ${d.lost ? `<div class="callout bad">This GPU stopped reporting to DCGM. The server may have lost it (an Xid 79), or its exporter isn't answering.</div>` : `<div class="mini-gauges">${mg.map(miniGauge).join("")}</div>`}
    <div class="stats">
      <div class="stat"><div class="k">Last Xid</div><div class="v ${recent ? "bad" : ""}" style="font-size:15px">${esc(xid)}</div></div>
      <div class="stat"><div class="k">Memory errors</div><div class="v ${d.dbe ? "bad" : d.sbe_rising ? "attn" : ""}" style="font-size:15px">${fmt(d.sbe)} corrected, ${fmt(d.dbe)} uncorrectable${d.remap_pending ? ", remap pending" : ""}</div></div>
      <div class="stat"><div class="k">Load and memory</div><div class="v" style="font-size:15px">${fmt(d.util)}%${d.mem_total ? `, ${(d.mem_used / 1024).toFixed(1)} of ${(d.mem_total / 1024).toFixed(1)} GB` : ""}</div></div>
      <div class="stat"><div class="k">Slowing down because</div><div class="v ${thermal ? "attn" : ""}" style="font-size:15px">${esc(reasons.join(", ") || "Nothing")}</div></div>
    </div>
    ${d.issues && d.issues.length ? `<div class="callout">${d.issues.map(esc).join("<br>")}</div>` : ""}
    <div><div class="seg" role="group" aria-label="History metric">${metrics.map(([k, l]) => `<button type="button" data-action="metric" data-id="${k}" aria-pressed="${k === metric}">${l}</button>`).join("")}</div>
      <div style="margin-top:10px">${lineChart(pts, { label: `GPU ${m[2]} over the last minute`, floor: 0, minSpan: metric === "temp" ? 10 : 4, color: (metric === "temp" && (d.temp >= 83 || thermal)) || (metric === "sbe" && d.sbe_rising) ? "var(--watch)" : "var(--accent)" })}</div></div>
    ${memory}
    ${inc ? `<a class="btn" href="#/incidents/${inc.id}">Open the incident on ${esc(targetName(inc.target))}</a>` : ""}
  </div>`);
}

function renderLocalGpu(d) {
  const thermal = (d.reasons || []).some((r) => r.includes("THERMAL"));
  const memPct = d.mem_total ? Math.round((100 * d.mem_used) / d.mem_total) : 0;
  const mg = d.lost ? [] : [
    { label: "Temperature", center: `${Math.round(d.temp)}°`, unit: "C", frac: d.temp / 100, level: d.temp >= 90 ? "crit" : thermal ? "watch" : "ok" },
    { label: "Power", center: fmt(d.power), unit: d.power_limit ? `W of ${d.power_limit}` : "W", frac: d.power_limit ? d.power / d.power_limit : 0, level: "ok" },
    { label: "SM clock", center: (d.clock / 1000).toFixed(2), unit: "GHz", frac: d.max_clock ? d.clock / d.max_clock : 0, level: thermal ? "watch" : "ok" },
  ];
  const order = ["HW_THERMAL_SLOWDOWN", "SW_THERMAL_SLOWDOWN", "HW_POWER_BRAKE_SLOWDOWN", "HW_SLOWDOWN", "SW_POWER_CAP"];
  const rank = (r) => (order.includes(r) ? order.indexOf(r) : 9);
  const reasons = (d.reasons || []).slice().sort((a, b) => rank(a) - rank(b)).map((r) => REASON_TEXT[r] || r);
  const metrics = [["temp", "Temp", "temperature"], ["power", "Power", "power"], ["clock", "Clock", "SM clock"], ["util", "Load", "load"]];
  const metric = metrics.some((m) => m[0] === S.fleet.metric) ? S.fleet.metric : "temp";
  const m = metrics.find((x) => x[0] === metric);
  const pts = d.history.map((h) => ({ v: h.lost ? 0 : h[metric] }));
  const inc = (S.state?.incidents || []).find((i) => i.target === LOCAL && !["resolved"].includes(i.status));
  const procs = d.processes && d.processes.length ? `<div><div class="small faint" style="margin-bottom:6px">Using the GPU now</div><div class="mini-list">${d.processes.map((p) =>
    `<div><span class="what">${esc(p.name)}</span><span class="small faint">${p.mem_mib != null ? `${fmt(p.mem_mib)} MiB` : p.type === "C" ? "compute" : "graphics"}</span></div>`).join("")}</div></div>` : "";
  setRegion("fl-gpu", $("#r-gpu"), `<div class="vitals">
    <div><h2 id="gp-h" style="font-size:17px">This laptop, GPU ${d.index}</h2><p class="small faint">${esc(d.model)}${d.driver ? `, driver ${esc(d.driver)}` : ""}. Real readings from nvidia-smi every 2 seconds. <a class="link" href="#/pc">Open My computer</a> for fixes.</p></div>
    ${d.lost ? `<div class="callout bad">This GPU stopped answering the NVIDIA driver. Anything using it has probably crashed.</div>` : `<div class="mini-gauges">${mg.map(miniGauge).join("")}</div>`}
    <div class="stats">
      <div class="stat"><div class="k">Load</div><div class="v">${fmt(d.util)}%</div></div>
      <div class="stat"><div class="k">GPU memory</div><div class="v ${memPct >= 95 ? "attn" : ""}">${d.mem_total ? `${(d.mem_used / 1024).toFixed(1)} of ${(d.mem_total / 1024).toFixed(1)} GB` : "Not reported"}</div></div>
      <div class="stat"><div class="k">Fan</div><div class="v">${d.fan == null ? "Not reported" : `${d.fan}%`}</div></div>
      <div class="stat"><div class="k">Slowing down because</div><div class="v ${thermal ? "attn" : ""}" style="font-size:15px">${esc(reasons.join(", ") || "Nothing")}</div></div>
    </div>
    <div><div class="seg" role="group" aria-label="History metric">${metrics.map(([k, l]) => `<button type="button" data-action="metric" data-id="${k}" aria-pressed="${k === metric}">${l}</button>`).join("")}</div>
      <div style="margin-top:10px">${lineChart(pts, { label: `This laptop's GPU ${m[2]} over the last minute`, floor: 0, minSpan: metric === "temp" ? 10 : 4, color: metric === "temp" && thermal ? "var(--watch)" : "var(--accent)" })}</div></div>
    ${procs}
    ${inc ? `<a class="btn" href="#/incidents/${inc.id}">Open the incident on this laptop</a>` : ""}
  </div>`);
}

/* Incidents: the list, and everything about one incident. */
VIEWS.incidents = {
  mount: () => `<div class="page">
    <div id="r-banner"></div>
    <div class="title-row"><div><h1>Incidents</h1><p class="lede" id="r-ilede"></p></div></div>
    <div class="tabs" role="tablist" id="r-itabs"></div>
    <div id="r-ibody"></div></div>`,
  update(st) {
    setRegion("in-banner", $("#r-banner"), banner());
    const groups = {
      open: st.incidents.filter((i) => ["investigating", "awaiting_approval", "needs_attention", "verifying"].includes(i.status)),
      closed: st.incidents.filter((i) => ["waiting_on_technician", "resolved", "closed"].includes(i.status)),
      tickets: st.tickets,
    };
    if (S.route.id) {
      const inc = st.incidents.find((i) => i.id === S.route.id);
      if (inc) S.incTab = groups.closed.includes(inc) ? "closed" : "open";
    }
    const needs = groups.open.filter((i) => ["awaiting_approval", "needs_attention"].includes(i.status)).length;
    setRegion("in-lede", $("#r-ilede"), `${plural(groups.open.length, "open incident")}${needs ? `, ${needs} waiting for you` : ""}. ${plural(st.tickets.filter((t) => t.status === "open").length, "repair ticket")} open.`);
    const tab = (k, l) => `<button type="button" role="tab" aria-selected="${S.incTab === k}" data-action="tab" data-tab="${k}">${l}<span class="count">${groups[k].length}</span></button>`;
    setRegion("in-tabs", $("#r-itabs"), tab("open", "Open") + tab("closed", "Handled") + tab("tickets", "Repair tickets"));
    if (S.incTab === "tickets") return setRegion("in-body", $("#r-ibody"), ticketsTable(st));
    const list = groups[S.incTab];
    if (!list.length) {
      return setRegion("in-body", $("#r-ibody"), `<div class="empty" style="margin-top:4px"><b>${S.incTab === "open" ? "No open incidents." : "Nothing handled yet."}</b><span>Incidents appear here when Nemotron 3 Nano flags something. <a class="link" href="#/sources">Run a failure drill</a> to see one.</span></div>`);
    }
    const sel = list.find((i) => i.id === S.route.id) || list[0];
    const rows = list.map((i) => `<a class="inc-row" href="#/incidents/${i.id}" ${i.id === sel.id ? 'aria-current="true"' : ""}>
      <span class="t">${esc(i.title || i.trigger.signal)}</span>
      <span class="m">${statusPill(i)}<span>${esc(targetName(i.target))}</span><span>${ago(i.opened_at)}</span></span></a>`).join("");
    setRegion("in-body", $("#r-ibody"), `<div class="card inc-layout"><nav class="inc-list" aria-label="Incidents">${rows}</nav><section class="detail" aria-live="polite">${incidentDetail(sel)}</section></div>`);
  },
};

function stagesHtml(inc) {
  const st = inc.status;
  const inv = st === "investigating" ? "now" : "done";
  const dry = !inc.dry_run ? (st === "investigating" ? "" : "done") : inc.dry_run.verdict === "fail" ? "fail" : "done";
  const appr = st === "awaiting_approval" ? "now" : ["verifying", "waiting_on_technician", "resolved"].includes(st) ? "done" : st === "needs_attention" ? "fail" : "";
  const ver = st === "verifying" ? "now" : ["waiting_on_technician", "resolved"].includes(st) ? "done" : "";
  const done = inc.actions.filter((a) => a.status !== "pending").length;
  const mine = forYou(inc);
  const items = [
    ["done", "Spotted", shortModel(inc.models.triage)],
    [inv, "Investigated", inc.status === "investigating" ? `${inc.trace.length} tool calls so far` : `${inc.trace.length} tool calls, ${inc.investigation_seconds ?? "?"} s`],
    [dry, inc.dry_run && inc.dry_run.checked ? "Checked" : "Dry run", mine ? "Steps for you" : inc.dry_run ? (inc.dry_run.checked ? (inc.dry_run.verdict === "fail" ? "A step can't run" : "On your computer, nothing changed") : ({ pass: "Passed", warn: "Warnings", fail: "Failed" }[inc.dry_run.verdict]) + (inc.revised ? ", after a revision" : "")) : "Waiting"],
    [appr, mine ? "Your steps" : "Your approval", inc.actions.length ? `${done} of ${inc.actions.length} ${mine ? "done" : "steps"}` : "No steps"],
    [ver, "Verified", st === "resolved" ? "Healthy" : st === "waiting_on_technician" ? "Stable, repair open" : "After the fix"],
  ];
  return `<ol class="stages" aria-label="Progress">${items.map(([c, b, s]) => `<li class="${c}"><b>${esc(b)}</b><span>${esc(s)}</span></li>`).join("")}</ol>`;
}

function incidentDetail(inc) {
  const where = `<a class="link" href="${targetLink(inc.target)}">${esc(targetName(inc.target))}${inc.gpu !== null && inc.gpu !== undefined ? `, GPU ${inc.gpu}` : ""}</a>`;
  const facts = [`Opened ${clock(inc.opened_at)}.`];
  if (inc.investigation_seconds != null) facts.push(`Investigated in ${inc.investigation_seconds} s.`);
  if (typeof inc.confidence === "number") facts.push(`${Math.round(inc.confidence * 100)}% confident.`);
  const head = `<div style="display:grid;gap:10px"><div class="meta">${statusPill(inc)}${where}</div>
    <h2>${esc(inc.title || inc.trigger.signal)}</h2><p class="small faint">${facts.join(" ")}</p></div>${stagesHtml(inc)}`;
  const summary = inc.summary ? `<div class="prose"><p>${esc(inc.summary)}</p>${inc.root_cause ? `<p style="margin-top:8px"><b>Root cause:</b> ${esc(inc.root_cause)}</p>` : ""}</div>`
    : `<p class="prose muted">Triage spotted: ${esc(inc.trigger.signal)}. ${esc(shortModel(inc.models.diagnosis))} is investigating.</p>`;
  const trace = traceHtml(inc);
  const dry = inc.dry_run && !forYou(inc) ? dryHtml(inc) : "";  // a plan of only "for you" steps has nothing to check
  const steps = inc.actions.length ? stepsHtml(inc) : "";
  const verify = inc.verification ? `<div class="callout ${inc.status === "resolved" ? "good" : inc.status === "needs_attention" ? "bad" : ""}">${esc(inc.verification)}</div>` : "";
  const err = inc.error ? `<div class="callout bad">${esc(inc.error)}</div>` : "";
  const stuck = inc.status === "needs_attention" ? `<div class="toolbar">${stuckButtons(inc)}</div>` : "";
  const tech = inc.needs_technician && inc.technician_note ? `<div class="callout"><b>For the technician:</b> ${esc(inc.technician_note)}</div>` : "";
  const blocked = inc.rejected_actions.length ? `<details class="plain"><summary>Blocked ${plural(inc.rejected_actions.length, "invalid suggestion")} from the model</summary><ul class="timeline">${inc.rejected_actions.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></details>` : "";
  const ev = inc.evidence.length ? `<details class="plain"><summary>Evidence (${inc.evidence.length} lines)</summary><ul class="evidence">${inc.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul></details>` : "";
  const tl = `<details class="plain"><summary>Timeline</summary><ol class="timeline">${inc.timeline.map((t) => `<li>${clock(t.at)} ${esc(t.text)}</li>`).join("")}</ol></details>`;
  const situation = inc.situation && inc.status === "awaiting_approval" ? `<div class="callout ${inc.dry_run && inc.dry_run.verdict === "fail" ? "bad" : ""}">${esc(inc.situation)}</div>` : "";
  return head + err + summary + situation + verify + stuck + trace + dry + steps + tech + blocked + ev + tl;
}

// An incident Nightingale couldn't finish: ask Nemotron to look again, or close it by hand.
function stuckButtons(inc) {
  return `<button class="btn primary" type="button" data-action="retry" data-id="${inc.id}">Investigate again</button>
    <button class="btn" type="button" data-action="close" data-id="${inc.id}">Close</button>`;
}

function traceHtml(inc) {
  const items = inc.trace.map((t) => {
    const lines = String(t.result || "").split("\n");
    return `<li><span class="n">${t.n}</span><div><div class="call">${esc(callText(t))}</div>${t.thought ? `<div class="why">${esc(t.thought)}</div>` : ""}
      <details><summary>Result: ${esc(lines[0].slice(0, 110))}${lines[0].length > 110 ? "…" : ""}</summary><pre>${esc(t.result)}</pre></details></div></li>`;
  });
  if (inc.status === "investigating" || inc.thinking) {
    items.push(`<li class="live"><span class="n">${inc.trace.length + 1}</span><div class="q-live"><span class="spinner" aria-hidden="true"></span>${esc(shortModel(inc.models.diagnosis))} is deciding what to check next</div></li>`);
  }
  if (!items.length) return "";
  const n = inc.trace.length;
  return `<details class="box" open><summary>How ${esc(shortModel(inc.models.diagnosis))} investigated (${plural(n, "tool call")})</summary><ol class="trace">${items.join("")}</ol></details>`;
}

function dryHtml(inc) {
  const d = inc.dry_run;
  const word = d.kind === "kubernetes" ? (d.verdict === "fail" ? "Kubernetes refused a step in the dry run" : "Kubernetes accepted every step in a dry run")
    : d.checked ? (d.verdict === "fail" ? "A step can't run right now" : "Checked on your computer") : { pass: "Dry run passed", warn: "Dry run passed with warnings", fail: "Dry run failed" }[d.verdict];
  const lines = d.steps.map((s) => `<li class="${s.ok ? "" : "no"}"><span class="pill ${!s.ok ? "bad" : s.locked ? "serious" : "good"}">${!s.ok ? (d.checked ? "Problem" : "Blocked") : s.locked ? "Locked" : "OK"}</span><span>${esc(s.label)}. ${esc(s.result)}</span></li>`).join("");
  const first = d.first_attempt ? `<details class="plain"><summary>The first plan failed its dry run, so Nemotron revised it</summary><ul class="timeline">${d.first_attempt.lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul></details>` : "";
  return `<div class="dry ${d.verdict}"><h3>${esc(word)}</h3><p>${esc(d.summary)}</p><details class="plain"><summary>${d.kind === "kubernetes" ? "What Kubernetes said about each step" : d.checked ? "What was checked for each step" : "What happened to each step on the copy"}</summary><ul style="margin-top:6px">${lines}</ul></details>${first}</div>`;
}

function stepsHtml(inc) {
  const waiting = inc.status === "awaiting_approval";
  const nextIdx = inc.actions.findIndex((a) => a.status === "pending");
  const items = inc.actions.map((a, i) => {
    const cur = waiting && i === nextIdx;
    const cls = a.status === "done" ? "done" : a.status === "failed" ? "failed" : cur ? "now" : a.status === "pending" ? "later" : "";
    const res = a.result ? `<div class="res ${a.status === "failed" ? "bad" : ""}">${esc(a.result)}</div>` : a.status === "skipped" ? `<div class="res skip">Skipped.</div>` : "";
    const msg = a.action === "notify_owner" && a.params.message ? `<div class="why">Message: “${esc(a.params.message)}”</div>` : "";
    const yes = a.action === "recommend" ? "I've done this" : a.admin ? "Approve (Windows asks)" : "Approve";
    const locked = a.mode === "serious" && pcMode().mode !== "serious";
    let btns = "";
    if (cur && locked) btns = `<div class="act"><button class="btn primary" type="button" data-action="pc-mode" data-id="serious" data-inc="${inc.id}">Turn on serious mode</button><button class="btn" type="button" data-action="skip" data-id="${inc.id}">Skip</button></div>`;
    else if (cur) btns = `<div class="act"><button class="btn primary" type="button" data-action="approve" data-id="${inc.id}">${yes}</button><button class="btn" type="button" data-action="skip" data-id="${inc.id}">Skip</button></div>`;
    else if (a.status === "failed" && (a.action.startsWith("pc_"))) btns = `<div class="act"><button class="btn small" type="button" data-action="retry-step" data-id="${inc.id}" data-index="${i}">Try again</button></div>`;
    const note = a.status === "pending" && (a.admin || a.guided || locked) ? `<div class="why">${[locked ? "Needs serious mode." : "", a.admin ? "Windows will ask for permission." : "", a.guided ? "Nightingale opens the right window; you make the final click." : ""].filter(Boolean).join(" ")}</div>` : "";
    return `<li class="${cls}"><span class="num">${i + 1}</span><div><div class="h"><b>${esc(a.label)}</b>${riskTag(a)}</div>
      <div class="why">${esc(a.reason)}</div>${note}${msg}${res}${btns}</div></li>`;
  }).join("");
  const left = inc.actions.filter((a) => a.status === "pending").length;
  const mine = forYou(inc);
  const all = waiting && left > 1 ? `<button class="btn" type="button" data-action="approve-all" data-id="${inc.id}">${mine ? `I've done all ${left}` : `Approve all ${left} remaining steps`}</button>` : "";
  const onPc = isLocal(inc.target);
  const hint = mine ? "These steps are yours; Nightingale re-checks the GPU afterwards" : onPc ? "Each fix runs on your computer only after you approve it" : "Each step runs only after you approve it";
  return `<div><div class="card-head" style="margin-bottom:10px"><h2>${mine ? "What to do" : "Fix plan"}</h2><span class="hint">${hint}</span></div><ol class="steps">${items}</ol>${all ? `<div style="margin-top:10px">${all}</div>` : ""}</div>`;
}

function ticketsTable(st) {
  if (!st.tickets.length) return `<div class="empty"><b>No repair tickets.</b><span>When a fix needs hands on hardware, Nightingale opens a ticket here for the technician or the facilities team.</span></div>`;
  const rows = st.tickets.slice().reverse().map((t) => `<tr><td><b>${esc(t.id)}</b><span class="sub">${t.kind === "facility" ? "Facilities" : "Hardware"}</span></td>
    <td><a class="link" href="${targetLink(t.node || t.rack)}">${esc(t.node || "rack " + t.rack)}</a>${t.gpu !== null && t.gpu !== undefined ? `<span class="sub">GPU ${t.gpu}</span>` : ""}</td>
    <td class="hide-sm">${esc(t.summary)}</td>
    <td class="r">${t.status === "open" ? `<button class="btn small" type="button" data-action="repair" data-id="${esc(t.id)}" title="In this simulation, marks the on-site repair as finished">Mark repaired</button>` : '<span class="pill good">Repaired</span>'}</td></tr>`).join("");
  return `<section class="card"><div class="table-wrap"><table class="collapse"><thead><tr><th>Ticket</th><th>Where</th><th class="hide-sm">What to do</th><th class="r">Status</th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="small faint" style="margin-top:12px">In this simulation, "Mark repaired" stands in for the technician finishing the work.</p></section>`;
}

/* Logs: the live stream, and a doctor for logs from anywhere. */
const SOURCES = { all: "All", kernel: "Kernel", dcgm: "DCGM", trainer: "Training", facility: "Facility", agent: "Nightingale" };
VIEWS.logs = {
  mount: () => `<div class="page">
    <div id="r-banner"></div>
    <div class="title-row"><div><h1>Logs</h1><p class="lede">Everything Nightingale reads, as it arrives. Lines used as evidence in an open incident are marked.</p></div></div>
    <div class="grid cols">
      <section class="card span-7" aria-labelledby="lg-h">
        <div class="card-head"><h2 id="lg-h">Live stream</h2><span class="hint" id="r-lcount"></span></div>
        <div class="toolbar" style="margin-bottom:12px">
          <label class="sr-only" for="log-q">Search logs</label><input id="log-q" type="search" placeholder="Search, e.g. Xid or dgx-0053" value="${esc(S.logFilter.q)}">
          <div class="seg" role="group" aria-label="Source">${Object.entries(SOURCES).map(([k, l]) => `<button type="button" data-action="logsrc" data-id="${k}" aria-pressed="${S.logFilter.source === k}">${l}</button>`).join("")}</div>
          <label class="check"><input type="checkbox" id="log-warn" ${S.logFilter.warnOnly ? "checked" : ""}> Warnings and errors only</label>
          <button class="btn small quiet" type="button" data-action="pause" id="log-pause">${S.logFilter.paused ? "Resume" : "Pause"}</button>
        </div>
        <div class="logbox" id="logbox" role="log" aria-label="Log stream" tabindex="0"></div>
      </section>
      <section class="card fill span-5" aria-labelledby="dx-h">
        <div class="card-head"><h2 id="dx-h">Diagnose your own logs</h2></div>
        <p class="small muted" style="margin-bottom:12px">Paste dmesg, nvidia-smi, DCGM or training logs from any GPU server. Long logs are filtered by Nemotron 3 Nano first, then diagnosed by Nemotron 3 Ultra. Steps are advice only.</p>
        <label class="sr-only" for="paste">Logs to diagnose</label>
        <textarea id="paste" rows="9" spellcheck="false" placeholder="Paste logs here"></textarea>
        <div class="toolbar" style="margin-top:10px"><button class="btn primary" type="button" data-action="analyze" id="an-btn">Diagnose logs</button><button class="btn quiet" type="button" data-action="sample">Load a sample</button><span class="small faint" id="an-status"></span></div>
        <div id="r-analysis" style="margin-top:16px">${READS_HTML}</div>
      </section>
    </div></div>`,
  update(st) {
    setRegion("lg-banner", $("#r-banner"), banner());
    renderLogs();
  },
};
function evidenceTexts() {
  const ev = [];
  for (const i of S.state?.incidents || []) if (!["resolved"].includes(i.status)) for (const e of [...i.evidence, ...i.trigger.evidence]) ev.push(e);
  return ev;
}
function logMatches(l) {
  const f = S.logFilter;
  if (f.warnOnly && l.level === "info") return false;
  if (f.source !== "all") {
    const src = l.source;
    const group = src === "bms" || l.node === "facility" ? "facility" : ["nightingale", "scheduler", "technician", "facilities"].includes(src) ? "agent" : src === "torchrun" ? "trainer" : src === "nvidia-smi" ? "kernel" : src;
    if (group !== f.source) return false;
  }
  if (f.q && !`${l.node} ${l.source} ${l.text}`.toLowerCase().includes(f.q.toLowerCase())) return false;
  return true;
}
let logSig = "";
function renderLogs(force) {
  const box = $("#logbox");
  if (!box) return;
  const sig = JSON.stringify(S.logFilter) + S.lastLogId;
  if (!force && (sig === logSig || S.logFilter.paused)) return;
  logSig = sig;
  const ev = evidenceTexts();
  const lines = S.logs.filter(logMatches).slice(-500);
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  box.innerHTML = lines.length ? lines.map((l) => {
    const mark = ev.some((e) => e.includes(l.text.slice(0, 60)));
    return `<div class="logline ${l.level} ${mark ? "ev" : ""}"><span class="ts">${esc(l.t)}</span> ${esc(l.node)} <span class="src">${esc(l.source)}:</span> ${esc(l.text)}</div>`;
  }).join("") : `<div class="logline">No lines match. Clear the search or choose another source.</div>`;
  if (near || force) box.scrollTop = box.scrollHeight;
  const c = $("#r-lcount");
  if (c) c.textContent = `${fmt(lines.length)} of the last ${fmt(S.logs.length)} lines`;
}
const READS_HTML = `<div class="reads">
  <h3>What it reads</h3>
  <dl>
    <div><dt>Kernel log</dt><dd><code>dmesg</code> or <code>journalctl -k</code>. Xid 79 (GPU fell off the bus), 48 (double-bit memory error), 74 (NVLink), 31 (a fault in your own code).</dd></div>
    <div><dt>nvidia-smi -q</dt><dd>Temperatures, throttle reasons, memory error counters and row remaps.</dd></div>
    <div><dt>DCGM</dt><dd><code>dcgmi diag</code> results and health-watch output.</dd></div>
    <div><dt>Training logs</dt><dd>NCCL timeouts, lost ranks, loss spikes.</dd></div>
  </dl>
  <p>The diagnosis replaces this panel: the root cause in plain English, the lines that prove it, and the steps Nemotron would take, safest first.</p>
</div>`;

function analysisHtml(d) {
  const steps = d.actions.length ? `<ol class="steps">${d.actions.map((a, i) => `<li><span class="num">${i + 1}</span><div><div class="h"><b>${esc(a.label || a.action)}</b><span class="risk ${a.risk}">${a.risk} risk</span></div><div class="why">${esc(a.reason)}</div></div></li>`).join("")}</ol>` : "";
  return `<div class="detail" style="gap:12px">
    <p class="small faint">${esc(d.route.join(". "))}.</p>
    <h3 style="font-size:18px">${esc(d.title || "Diagnosis")}</h3>
    <div class="prose"><p>${esc(d.summary)}</p>${d.root_cause ? `<p style="margin-top:8px"><b>Root cause:</b> ${esc(d.root_cause)}</p>` : ""}</div>
    ${steps}
    ${d.evidence.length ? `<details class="plain"><summary>Evidence (${d.evidence.length} lines)</summary><ul class="evidence">${d.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul></details>` : ""}
  </div>`;
}

/* Data sources: what Nightingale watches, plus the simulated data center's drills. */
function sourcesHtml(st, full) {
  const loc = st.local;
  if (!full) {  // the compact list on Overview
    const bad = loc && (loc.state === "lost" || loc.state === "error");
    const lapRight = loc ? `<span class="pill ${bad ? "bad" : "solid"}">${loc.state === "lost" ? "Not answering" : bad ? "Error" : "Connected"}</span>`
      : `<button class="btn small primary" type="button" data-action="local-on">${S.localBusy ? "Connecting…" : "Connect"}</button>`;
    return `<div class="mini-list">
      <div><span class="what"><b>Simulated data center</b><span class="sub">${fmt(st.size)} GPUs, failure drills</span></span><span class="pill solid">Connected</span></div>
      <div><span class="what"><b>This laptop's GPU</b><span class="sub">${loc ? esc(shortGpu(loc.name)) + ", fixes need your approval" : S.localError ? esc(S.localError) : "Real readings from nvidia-smi"}</span></span>${lapRight}</div>
      <div><span class="what"><b>Real GPU servers</b><span class="sub">${st.real ? `${plural(st.real.servers, "server")}, ${plural(st.real.gpus, "GPU")} through DCGM${st.real.kube ? " and Kubernetes" : ", watch only"}` : "DCGM and Kubernetes, from a real data center"}</span></span>${st.real ? `<span class="pill ${st.real.state === "ok" ? "solid" : "bad"}">${st.real.state === "ok" ? "Connected" : "Reading error"}</span>` : '<a class="btn small" href="#/sources">Connect</a>'}</div>
    </div>`;
  }
  const choice = S.sizeChoice || st.size;
  const size = full ? `<div class="toolbar"><div class="seg" role="group" aria-label="GPUs">${st.sizes.map((n) => `<button type="button" data-action="size" data-id="${n}" aria-pressed="${n === choice}">${fmt(n)} GPUs</button>`).join("")}</div>
      <button class="btn small" type="button" data-action="reset">${choice === st.size ? "Reset the demo" : `Restart with ${fmt(choice)} GPUs`}</button></div>
      <p class="small faint">Changing the size restarts the simulation and clears incidents.</p>` :
    `<p class="small faint">${fmt(st.size)} GPUs in ${fmt(st.servers)} servers and ${st.racks} racks.</p>`;
  const sim = `<div class="srcard on"><div class="srcard-head"><div><b>Simulated data center</b><span>Realistic telemetry and failure drills</span></div><span class="pill solid">Connected</span></div>${size}</div>`;
  let lap;
  if (loc) {
    const bad = loc.state === "lost" || loc.state === "error";
    lap = `<div class="srcard ${bad ? "bad" : "on"}"><div class="srcard-head"><div><b>This laptop's GPU</b><span>${esc(loc.name || "NVIDIA GPU")}${loc.driver ? `, driver ${esc(loc.driver)}` : ""}</span></div>
      <span class="pill ${bad ? "bad" : "solid"}">${loc.state === "lost" ? "Not answering" : bad ? "Error" : "Connected"}</span></div>
      <p class="small faint">${bad ? esc(loc.error) : "Real readings from nvidia-smi every 2 s" + (loc.events_supported ? ", plus Windows display-driver events" : "") + ". Fixes run only after you approve them, in light or serious mode."}</p>
      <div class="toolbar"><a class="btn small" href="#/fleet?rack=L-01&server=laptop&gpu=0">Open its vitals</a><button class="btn small quiet" type="button" data-action="local-off">Disconnect</button></div></div>`;
  } else {
    lap = `<div class="srcard"><div class="srcard-head"><div><b>This laptop's GPU</b><span>Reads nvidia-smi every 2 s. Watch and diagnose only.</span></div></div>
      ${S.localError ? `<p class="small src-error">${esc(S.localError)}</p>` : ""}
      <div class="toolbar"><button class="btn small primary" type="button" data-action="local-on">${S.localBusy ? "Connecting…" : "Connect"}</button></div></div>`;
  }
  return `<div class="sources ${full ? "full" : ""}">${sim}${lap}${realCard(st)}</div>`;
}
function realCard(st) {
  const r = st.real;
  if (r) {
    const bad = r.state !== "ok";
    const when = r.read_at ? `last read ${ago(r.read_at)}` : "";
    return `<div class="srcard ${bad ? "bad" : "on"}"><div class="srcard-head"><div><b>Real GPU servers</b><span>${plural(r.servers, "server")}, ${plural(r.gpus, "GPU")}: DCGM from ${esc(r.where)}</span></div>
      <span class="pill ${bad ? "bad" : "solid"}">${bad ? "Reading error" : "Connected"}</span></div>
      <p class="small faint">${bad ? esc(r.error) : `${r.kube ? `Kubernetes at ${esc(r.kube_where)}: approved plans can cordon nodes, move jobs and run nvidia-smi, each rehearsed with a Kubernetes dry run first.` : `Watching only. ${esc(r.kube_note || "Kubernetes isn't connected.")}`} ${when ? `Every ${r.interval} s, ${when}.` : ""}`}${r.kube_error ? ` <span class="src-error">${esc(r.kube_error)}</span>` : ""}</p>
      <div class="toolbar"><a class="btn small" href="#/fleet?rack=${(r.racks || [])[0] || "R-01"}">Open in Fleet</a>${st.fleet_ui_connect ? '<button class="btn small quiet" type="button" data-action="fleet-off">Disconnect</button>' : ""}</div></div>`;
  }
  if (!st.fleet_ui_connect) {
    return `<div class="srcard"><div class="srcard-head"><div><b>Real GPU servers</b><span>DCGM readings and Kubernetes from a real data center</span></div></div>
      <p class="small faint">Connecting real servers is turned off on this deployment. Run Nightingale yourself with <code>FLEET_DCGM</code> set (see the README).</p></div>`;
  }
  return `<div class="srcard"><div class="srcard-head"><div><b>Real GPU servers</b><span>DCGM readings from a real data center, plus Kubernetes so approved plans can act</span></div></div>
    <form class="fleet-form" data-form="fleet">
      <label for="fleet-url">Where the DCGM readings are</label>
      <input id="fleet-url" name="url" type="text" spellcheck="false" autocomplete="off" placeholder="http://127.0.0.1:9400/metrics">
      <p class="small faint">A dcgm-exporter <code>/metrics</code> URL (several, comma separated), a Prometheus URL like <code>http://prometheus:9090</code>, or <code>auto</code> to find every dcgm-exporter in the Kubernetes cluster.</p>
      <label for="fleet-kube">Kubernetes</label>
      <select id="fleet-kube" name="kube"><option value="auto">Use it if a kubeconfig works (approved plans can act)</option><option value="off">Off: watch only</option></select>
      ${S.fleetError ? `<p class="small src-error">${esc(S.fleetError)}</p>` : ""}
      <div class="toolbar"><button class="btn small primary" type="submit">${S.fleetBusy ? "Connecting…" : "Connect"}</button></div>
    </form>
    <details class="plain"><summary>Try it on this laptop, or on a rented NVIDIA GPU</summary>
      <p class="small">On this laptop: run <code>python -m app.dcgm_bridge</code> in the Nightingale folder. It serves your GPU in DCGM's format at <code>http://127.0.0.1:9400/metrics</code>. Paste that above with Kubernetes off.</p>
      <p class="small">On a real GPU server: <code>deploy/nebius/setup.sh</code> installs Kubernetes, NVIDIA's GPU Operator (with DCGM) and Nightingale on one rented Nebius AI Cloud GPU and connects them with <code>FLEET_DCGM=auto</code>.</p></details></div>`;
}
const DRILL_TEXT = {
  thermal: "One GPU's cooling starts failing. It heats up, throttles, and slows its whole training job.",
  ecc: "One GPU's memory starts throwing corrected errors. About two minutes later it crashes the job, unless something moves it first.",
  bus: "One GPU drops off the PCIe bus (Xid 79). The training job it belongs to crashes at once.",
  nvlink: "One GPU's NVLink connection degrades. All-reduce slows, and later the job times out.",
  rack_cooling: "The coolant loop that feeds one rack loses flow. Every busy server in that rack heats up together.",
  app_bug: "A bug in a team's training code reads memory it does not own. The hardware is fine.",
};
VIEWS.sources = {
  mount: () => `<div class="page">
    <div id="r-banner"></div>
    <div class="title-row"><div><h1>Data sources</h1><p class="lede">Every source feeds the same agent. Mix them freely: the simulated data center for drills, this laptop's real GPU, and real GPU servers through DCGM and Kubernetes.</p></div></div>
    <section class="card" aria-labelledby="src-h"><div class="card-head"><h2 id="src-h">Sources</h2></div><div id="r-srcs"></div></section>
    <section class="card" aria-labelledby="dr-h"><div class="card-head"><h2 id="dr-h">Failure drills</h2><span class="hint">In the simulated data center. Then open Overview to follow it.</span></div>
      <div class="drills" id="r-drills"></div><hr class="divider"><div class="card-head"><h3>Drills run</h3></div><div id="r-dhist"></div></section></div>`,
  update(st) {
    setRegion("si-banner", $("#r-banner"), banner());
    setRegion("si-srcs", $("#r-srcs"), sourcesHtml(st, true));
    const fi = $("#fleet-url");
    if (fi && S.fleetUrl && !fi.value) fi.value = S.fleetUrl;
    setRegion("si-drills", $("#r-drills"), Object.entries(st.scenarios).map(([k, label]) => `<div class="drill"><b>${esc(label)}</b><p>${esc(DRILL_TEXT[k] || "")}</p>
      <button class="btn" type="button" data-action="drill" data-id="${k}">Start drill</button></div>`).join(""));
    const drills = st.drills.slice().reverse();
    setRegion("si-dhist", $("#r-dhist"), drills.length ? `<div class="mini-list">${drills.map((d) => `<div><span class="what"><b>${esc(d.label)}</b> on <a class="link" href="${targetLink(d.target)}">${esc(targetName(d.target))}${d.gpu !== null && d.gpu !== undefined ? `, GPU ${d.gpu}` : ""}</a></span><span class="pill ${d.status === "resolved" ? "good" : ""}">${d.status === "resolved" ? "Fixed" : "Active"}</span></div>`).join("")}</div>`
      : `<p class="muted small">No drills yet. Start one above.</p>`);
  },
};

/* Ask your data center: questions answered from live data with read-only tools. */
const ASK_EXAMPLES = ["Is anything wrong right now?", "Which rack is running hottest?", "How is llm-pretrain-405b doing?",
  "Which GPUs have memory errors?", "How much power are we using?", "How many spare servers do we have?"];
VIEWS.ask = {
  mount: () => `<div class="page">
    <div id="r-banner"></div>
    <div class="title-row"><div><h1>Ask your data center</h1><p class="lede">Nemotron answers from live data. It reads the same read-only tools it uses in investigations, and shows what it checked. It can't change anything from here.</p></div></div>
    <section class="card ask-card" aria-labelledby="ask-h"><h2 id="ask-h" class="sr-only">Conversation</h2>
      <div class="chips" role="group" aria-label="Example questions" id="r-chips"></div>
      <div class="thread" id="r-thread" aria-live="polite"></div>
      <form class="ask-form" id="ask-form"><label for="ask-q" class="sr-only">Ask a question about your GPUs</label>
        <input id="ask-q" type="text" autocomplete="off" maxlength="500" placeholder="Ask anything about your GPUs, racks, jobs or incidents">
        <button class="btn primary" type="submit" id="ask-btn">Ask</button></form></section></div>`,
  update(st) {
    setRegion("ask-banner", $("#r-banner"), banner());
    const ex = st.local ? ["Is my laptop GPU OK?", ...ASK_EXAMPLES.slice(0, 5)] : ASK_EXAMPLES;
    setRegion("ask-chips", $("#r-chips"), ex.map((q) => `<button type="button" class="chip-btn" data-action="ask-ex" data-q="${esc(q)}">${esc(q)}</button>`).join(""));
    renderThread();
  },
};
function linkRefs(text, refs) {
  const hrefs = {};
  (refs || []).forEach((r) => {
    const href = /^dgx-\d{4}$|^laptop$/.test(r) ? targetLink(r) : /^[A-H]-\d{2}$/.test(r) ? `#/fleet?rack=${r}` : null;
    if (href) hrefs[r] = href;
  });
  const names = Object.keys(hrefs).sort((a, b) => b.length - a.length);
  if (!names.length) return esc(text);
  // One pass over the plain text, so a link is never inserted inside another link.
  const re = new RegExp(`\\b(${names.map((n) => n.replace(/-/g, "\\-")).join("|")})\\b`, "g");
  let out = "", last = 0, m;
  const t = String(text);
  while ((m = re.exec(t))) {
    out += esc(t.slice(last, m.index)) + `<a class="link" href="${hrefs[m[1]]}">${esc(m[1])}</a>`;
    last = m.index + m[1].length;
  }
  return out + esc(t.slice(last));
}
function renderThread() {
  const el = $("#r-thread");
  if (!el) return;
  const items = S.asks.map((e) => {
    const calls = e.trace.map((t) => `<code>${esc(callText(t))}</code>`).join(", ");
    let a;
    if (e.status === "thinking") {
      const last = e.trace[e.trace.length - 1];
      a = `<div class="bubble a"><div class="q-live"><span class="spinner" aria-hidden="true"></span>${last ? `Checked ${e.trace.length}: <code>${esc(callText(last))}</code>` : `${esc(shortModel(e.model))} is deciding what to check`}</div></div>`;
    } else if (e.status === "error") {
      a = `<div class="bubble a bad"><p>${esc(e.error)}</p><p class="small faint">Try again, or ask it a different way.</p></div>`;
    } else {
      const reads = e.trace.length ? `<details class="plain"><summary>What it read (${plural(e.trace.length, "tool call")})</summary><ol class="trace">${e.trace.map((t) => `<li><span class="n">${t.n}</span><div><div class="call">${esc(callText(t))}</div>${t.thought ? `<div class="why">${esc(t.thought)}</div>` : ""}<pre>${esc(t.result)}</pre></div></li>`).join("")}</ol></details>` : "";
      a = `<div class="bubble a"><p>${linkRefs(e.answer, e.refs)}</p><p class="checked">Checked ${calls || "nothing"} · ${e.seconds ?? "?"} s · ${esc(shortModel(e.model))}</p>${reads}</div>`;
    }
    return `<div class="exchange"><div class="bubble q">${esc(e.question)}</div>${a}</div>`;
  });
  const html = items.length ? items.join("") : `<div class="empty"><b>Ask about anything Nightingale can see.</b><span>Servers like dgx-0053, racks like A-07, training jobs, incidents, power, spare capacity${S.state && S.state.local ? ", or your own laptop GPU" : ""}. Pick an example above to start.</span></div>`;
  if (S.regions.thread !== html) {
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    S.regions.thread = html;
    el.innerHTML = html;
    if (atBottom || S.asking) el.scrollTop = el.scrollHeight;
  }
  const btn = $("#ask-btn");
  if (btn) btn.disabled = S.asks.some((e) => e.status === "thinking");
}
async function pollAsks() {
  try { S.asks = (await api("/api/ask")).asks; } catch (e) { return; }
  if (S.route.name === "ask") renderThread();
  if (S.route.name === "pc" && S.state) VIEWS.pc.update(S.state);
  if (S.asks.some((e) => e.status === "thinking")) setTimeout(pollAsks, 800);
  else S.asking = false;
}
async function askQuestion(q) {
  q = String(q || "").trim();
  if (q.length < 2) return;
  S.asking = true;
  try {
    await api("/api/ask", { method: "POST", body: { question: q } });
    const input = $("#ask-q");
    if (input) input.value = "";
  } catch (e) { S.asking = false; toast(e.message); }
  await pollAsks();
}
async function connectFleet(form) {
  if (S.fleetBusy) return;
  S.fleetUrl = form.url.value.trim();
  S.fleetBusy = true; S.fleetError = ""; render();
  try {
    const r = await api("/api/fleet", { method: "POST", body: { url: S.fleetUrl, kube: form.kube.value } });
    toast(r.message); S.fleetUrl = "";
  } catch (e2) { S.fleetError = e2.message; }
  S.fleetBusy = false;
  await poll();
}
document.addEventListener("input", (e) => {
  if (e.target && e.target.id === "fleet-url") S.fleetUrl = e.target.value;
  const k = e.target && e.target.dataset && e.target.dataset.param;
  if (k) S.pcParams[k] = e.target.value;
});
document.addEventListener("submit", (e) => {
  if (e.target.dataset && e.target.dataset.form === "fleet") { e.preventDefault(); connectFleet(e.target); return; }
  if (e.target.id !== "ask-form") return;
  e.preventDefault();
  askQuestion($("#ask-q").value);
});

/* Benchmark: failure drills on fresh data centers, scored end to end. */
VIEWS.benchmark = {
  mount: () => `<div class="page">
    <div id="r-banner"></div>
    <div class="title-row"><div><h1>Benchmark</h1><p class="lede">Runs failure drills on fresh copies of the data center and scores Nightingale end to end: did it find the broken part, name the right cause, fix it, and stay safe? The benchmark approves every step itself, so it also tests whether the plans are good.</p></div></div>
    <section class="card" aria-labelledby="bn-h"><div class="card-head"><h2 id="bn-h">Run</h2><span class="hint" id="r-bnmode"></span></div><div id="r-bnrun"></div></section>
    <div id="r-bnres"></div></div>`,
  update(st) {
    setRegion("bn-banner", $("#r-banner"), banner());
    const b = S.bench;
    const live = st.mode === "live";
    setRegion("bn-mode", $("#r-bnmode"), live ? `Live: ${esc(shortModel(st.models.triage))} and ${esc(shortModel(st.models.diagnosis))} on Nebius Token Factory` : "Demo mode: offline stand-in models");
    const counts = (b && b.counts) || [6, 12, 30], sizes = (b && b.sizes) || [256, 1024];
    const running = b && b.running;
    const est = live ? `Uses Token Factory credits: roughly $0.03 to $0.06 per drill, so about ${money(S.benchCount * 0.03)} to ${money(S.benchCount * 0.06)}. Takes a few minutes.` :
      "Demo mode runs in seconds and measures the pipeline, not Nemotron: the stand-in models follow fixed rules. Add your key for real numbers.";
    const progress = running ? `<div class="bn-progress"><div class="q-live"><span class="spinner" aria-hidden="true"></span>Drill ${Math.min(b.done + 1, b.total)} of ${b.total} running${b.done ? `, ${b.done} scored` : ""}</div><span class="bar"><i style="width:${Math.round(100 * b.done / Math.max(1, b.total))}%"></i></span></div>` : "";
    setRegion("bn-run", $("#r-bnrun"), `<div class="bn-controls">
        <div><span class="k">Drills</span><div class="seg" role="group" aria-label="Number of drills">${counts.map((n) => `<button type="button" data-action="bn-count" data-id="${n}" aria-pressed="${n === S.benchCount}" ${running ? "disabled" : ""}>${n}</button>`).join("")}</div></div>
        <div><span class="k">Data center</span><div class="seg" role="group" aria-label="Data center size">${sizes.map((n) => `<button type="button" data-action="bn-size" data-id="${n}" aria-pressed="${n === S.benchSize}" ${running ? "disabled" : ""}>${fmt(n)} GPUs</button>`).join("")}</div></div>
        <button class="btn primary" type="button" data-action="bn-run" ${running ? "disabled" : ""}>${running ? "Running…" : b && b.results ? "Run the benchmark again" : "Run the benchmark"}</button></div>
      <p class="small faint" style="margin-top:12px">${est} Every drill type gets an equal share. Your live data center isn't touched.</p>${progress}
      ${b && b.error ? `<div class="callout bad" style="margin-top:12px">${esc(b.error)}</div>` : ""}`);
    setRegion("bn-res", $("#r-bnres"), b && (b.results || (b.cases && b.cases.length)) ? benchResults(b) : "");
  },
};
function pctTile(label, v, sub) {
  const cls = v === null || v === undefined ? "" : v >= 90 ? "" : v >= 70 ? "attn" : "bad";
  return `<div class="stat"><div class="k">${esc(label)}</div><div class="v ${cls}">${v === null || v === undefined ? "–" : v + "%"}</div>${sub ? `<div class="s">${esc(sub)}</div>` : ""}</div>`;
}
function benchResults(b) {
  const r = b.results;
  const head = r ? `<section class="card" aria-labelledby="br-h"><div class="card-head"><h2 id="br-h">Results</h2><span class="hint">${plural(r.n, "drill")} on ${fmt(b.size)}-GPU data centers, ${b.mode === "live" ? "live Nemotron" : "demo models"}, ${b.finished_at ? clock(b.finished_at) : ""} · <a class="link" href="/api/benchmark/results.json" download>Download JSON</a></span></div>
    <div class="bench-stats">${pctTile("Found the broken part", r.found)}${pctTile("Named the right cause", r.cause)}${pctTile("Plan passed the rehearsal", r.rehearsal)}${pctTile("Fixed", r.fixed)}${pctTile("Stayed safe", r.safe, r.false_alarms ? `${r.false_alarms} false alarms` : "No false alarms")}</div>
    <div class="bench-stats small-stats"><div class="stat"><div class="k">Median time to spot</div><div class="v">${r.detect_seconds ?? "–"} s</div><div class="s">simulated time</div></div>
      <div class="stat"><div class="k">Median time to diagnose</div><div class="v">${r.diagnose_seconds ?? "–"} s</div><div class="s">Ultra's investigation</div></div>
      <div class="stat"><div class="k">Cost per drill</div><div class="v">${money(r.cost_per_drill)}</div><div class="s">${money(r.cost)} in all${b.mode === "live" ? "" : ", estimated"}</div></div>
      <div class="stat"><div class="k">Model calls</div><div class="v">${fmt(r.calls.triage + r.calls.diagnosis)}</div><div class="s">${fmt(r.calls.triage)} Nano, ${fmt(r.calls.diagnosis)} Ultra</div></div></div>
    <hr class="divider"><div class="card-head"><h3>By failure type</h3></div>
    <div class="table-wrap"><table class="collapse"><thead><tr><th>Drill</th><th class="r">Runs</th><th class="r">Found</th><th class="r">Right cause</th><th class="r">Fixed</th><th class="r">Safe</th></tr></thead><tbody>
    ${r.by_kind.map((k) => `<tr><td><b>${esc(k.label)}</b></td><td class="r">${k.n}</td>${["found", "cause", "fixed", "safe"].map((m) => `<td class="r pct-cell" data-label="${{ found: "Found", cause: "Right cause", fixed: "Fixed", safe: "Safe" }[m]}"><span class="pct">${k[m] ?? "–"}%</span><span class="bar"><i class="${k[m] >= 90 ? "" : k[m] >= 70 ? "attn" : "bad"}" style="width:${k[m] ?? 0}%"></i></span></td>`).join("")}</tr>`).join("")}
    </tbody></table></div></section>` : "";
  const cases = (b.cases || []).map((c) => {
    const ok = c.found && c.cause && c.fixed && c.safe;
    return `<tr><td class="r hide-sm">${c.n}</td><td><b>${esc(c.label)}</b><span class="sub">on ${esc(c.target)}${c.gpu !== null && c.gpu !== undefined ? `, GPU ${c.gpu}` : ""}</span></td>
      <td class="what">${c.incident ? `${esc(c.incident.title)}<span class="sub">${esc(c.incident.status.replace(/_/g, " "))}, ${plural(c.incident.tool_calls, "tool call")}${c.incident.revised ? ", plan revised" : ""}</span>` : '<span class="muted">No incident</span>'}</td>
      <td class="score"><span class="pill ${ok ? "good" : "bad"}">${ok ? "Pass" : "Miss"}</span>${c.why ? `<span class="sub">${esc(c.why)}</span>` : ""}</td></tr>`;
  }).join("");
  return head + `<section class="card" aria-labelledby="bc-h"><div class="card-head"><h2 id="bc-h">Every drill</h2><span class="hint">${b.running ? "Filling in as drills finish" : ""}</span></div>
    <div class="table-wrap"><table class="collapse"><thead><tr><th class="r">#</th><th>Drill</th><th>What Nightingale did</th><th>Score</th></tr></thead><tbody>${cases}</tbody></table></div></section>`;
}
async function pollBench() {
  try { S.bench = await api("/api/benchmark"); } catch (e) { return; }
  if (S.route.name === "benchmark" && S.state) VIEWS.benchmark.update(S.state);
  if (S.bench.running) setTimeout(pollBench, 1500);
}

/* My computer: this laptop's real GPU, its health, history, and the fixes Nightingale can run. */
const PC_NAMES = {
  pc_close_program: "Close a program", pc_power_plan: "Switch the power plan", pc_clear_shader_cache: "Clear the shader cache",
  pc_restart_gpu_driver: "Restart the GPU driver", pc_uncap_gpu_clock: "Remove the clock cap", pc_create_restore_point: "Make a restore point",
  pc_cap_gpu_clock: "Cap the GPU clock", pc_set_power_limit: "Set the power limit", pc_performance_mode: "Fan and performance mode",
  pc_update_driver: "Update the NVIDIA driver", pc_rollback_driver: "Roll back the NVIDIA driver", pc_bios_update: "BIOS and firmware updates",
};
const CHECKUP_PREFIX = "Give my computer's GPU a full check-up";
function pcMode() { return (S.state && S.state.pc_mode) || { mode: "light" }; }
function minutesLeft(m) { return m.seconds_left == null ? "" : `${Math.max(1, Math.ceil(m.seconds_left / 60))} min left`; }
function modeSwitch(compact) {
  const m = pcMode();
  const serious = m.mode === "serious";
  if (compact) return `<a class="chip mode-chip ${serious ? "serious" : ""}" href="#/pc" title="Fix mode on this computer">${serious ? `Serious mode · ${minutesLeft(m)}` : "Light mode"}</a>`;
  return `<div class="mode-switch" role="group" aria-label="Fix mode on this computer">
    <button type="button" data-action="pc-mode" data-id="light" aria-pressed="${!serious}"><b>Light</b><span>Quick, reversible fixes</span></button>
    <button type="button" class="serious" data-action="pc-mode" data-id="serious" aria-pressed="${serious}"><b>Serious${serious ? ` · ${minutesLeft(m)}` : ""}</b><span>Clock caps, power, drivers, BIOS</span></button></div>`;
}
function pcVitals(d) {
  if (!d || d.lost) return "";
  const thermal = (d.reasons || []).some((r) => r.includes("THERMAL"));
  const memPct = d.mem_total ? Math.round((100 * d.mem_used) / d.mem_total) : 0;
  const reasons = (d.reasons || []).map((r) => REASON_TEXT[r] || r);
  return [
    { label: "Temperature", center: `${Math.round(d.temp)}°`, unit: "C", frac: d.temp / 100, level: d.temp >= 90 ? "crit" : thermal ? "watch" : "ok",
      value: thermal ? "Slowing down for heat" : d.temp >= 87 ? "Hot" : "Normal", sub: "Laptops run 75–87°C under load", subLevel: thermal ? "watch" : "" },
    { label: "GPU clock", center: (d.clock / 1000).toFixed(2), unit: "GHz", frac: d.max_clock ? d.clock / d.max_clock : 0, level: thermal ? "watch" : "ok",
      value: `of ${(d.max_clock / 1000).toFixed(2)} GHz max`, sub: reasons.length ? `Held back: ${reasons.join(", ")}` : (d.policy_flags || []).length ? "Not held back (idle power-saving limit)" : "Not held back", subLevel: thermal ? "watch" : "" },
    { label: "Power", center: fmt(d.power), unit: "W", frac: d.power_limit ? d.power / d.power_limit : 0, level: "ok",
      value: d.power_limit ? `of a ${fmt(d.power_limit)} W limit` : "Limit not reported", sub: `${fmt(d.util)}% load` },
    { label: "GPU memory", center: `${memPct}%`, unit: "used", frac: memPct / 100, level: memPct >= 95 ? "watch" : "ok",
      value: d.mem_total ? `${(d.mem_used / 1024).toFixed(1)} of ${(d.mem_total / 1024).toFixed(1)} GB` : "Not reported", sub: memPct >= 95 ? "Almost full" : "" , subLevel: memPct >= 95 ? "watch" : "" },
    { label: "Fan", center: d.fan == null ? "–" : `${d.fan}%`, unit: d.fan == null ? "" : "speed", frac: d.fan == null ? 0 : d.fan / 100, level: "ok",
      value: d.fan == null ? "Not reported by this laptop" : "GPU fan", sub: "Armoury Crate sets the fan mode" },
  ];
}
function pcVerdict(p) {
  const d = p.gpu, sum = p.summary || {};
  const hist = sum.samples ? `In the last 24 hours: hottest ${Math.round(sum.max_temp)}°C, ${plural(sum.heat_minutes, "minute")} slowing down for heat.` : "History builds up as Nightingale runs.";
  if (d.lost) return `<div class="verdict bad"><b>Your GPU stopped answering the driver.</b><span>Anything using it has probably crashed. Nightingale has opened an incident with fixes below.</span></div>`;
  if (p.issues && p.issues.length) return `<div class="verdict watch"><b>Needs a look.</b><span>${esc(p.issues.join("; "))}.</span><span class="faint">${esc(hist)}</span></div>`;
  return `<div class="verdict good"><b>Healthy.</b><span>${Math.round(d.temp)}°C at ${fmt(d.util)}% load, nothing is holding the GPU back, and Windows hasn't logged driver or hardware errors.</span><span class="faint">${esc(hist)}</span></div>`;
}
function pcCheckup() {
  const e = S.asks.filter((a) => a.question.startsWith(CHECKUP_PREFIX)).pop();
  if (!e) return "";
  if (e.status === "thinking") {
    const last = e.trace[e.trace.length - 1];
    return `<div class="bubble a"><div class="q-live"><span class="spinner" aria-hidden="true"></span>${last ? `Checked ${e.trace.length}: <code>${esc(callText(last))}</code>` : `${esc(shortModel(e.model))} is starting the check-up`}</div></div>`;
  }
  if (e.status === "error") return `<div class="bubble a bad"><p>${esc(e.error)}</p></div>`;
  return `<div class="bubble a"><p>${linkRefs(e.answer, e.refs)}</p><p class="checked">Checked ${e.trace.map((t) => `<code>${esc(callText(t))}</code>`).join(", ")} · ${e.seconds ?? "?"} s · ${esc(shortModel(e.model))} · ${ago(e.at)}</p></div>`;
}
function fixRow(f, p) {
  const locked = f.mode === "serious" && pcMode().mode !== "serious";
  const key = f.action;
  const confirm = S.pcConfirm === key;
  const g = p.gpu || {};
  let input = "";
  if (f.action === "pc_power_plan") input = `<div class="seg" role="group" aria-label="Power plan">${["balanced", "high_performance", "power_saver"].map((k) => `<button type="button" data-action="pc-param" data-fix="${key}" data-id="${k}" aria-pressed="${(S.pcParams[key] || "balanced") === k}">${{ balanced: "Balanced", high_performance: "High performance", power_saver: "Power saver" }[k]}</button>`).join("")}</div>`;
  if (f.action === "pc_performance_mode") input = `<div class="seg" role="group" aria-label="Armoury Crate mode">${["Silent", "Performance", "Turbo"].map((k) => `<button type="button" data-action="pc-param" data-fix="${key}" data-id="${k}" aria-pressed="${(S.pcParams[key] || "Turbo") === k}">${k}</button>`).join("")}</div>`;
  if (f.action === "pc_cap_gpu_clock") input = `<label class="num-input">Cap at <input type="number" min="300" max="${g.max_clock || 3000}" step="15" data-param="${key}" value="${Math.round(((g.max_clock || 1800) * 0.85) / 15) * 15}"> MHz <span class="faint">(max ${fmt(g.max_clock || 0)})</span></label>`;
  if (f.action === "pc_set_power_limit") input = `<label class="num-input">Limit <input type="number" min="5" max="${g.power_max || 200}" step="5" data-param="${key}" value="${g.power_limit || 80}"> W</label>`;
  if (f.action === "pc_close_program") return "";  // closing a program lives next to each program
  const tags = `${f.admin ? '<span class="pill">Windows asks</span>' : ""}${f.guided ? '<span class="pill">You finish it</span>' : ""}<span class="risk ${f.risk}">${f.risk} risk</span>`;
  const btn = locked ? `<button class="btn small" type="button" disabled>Needs serious mode</button>`
    : `<button class="btn small ${confirm ? "primary" : ""}" type="button" data-action="pc-fix" data-id="${key}">${confirm ? "Click again to run" : "Run"}</button>`;
  return `<div class="fix ${locked ? "locked" : ""}"><div class="fix-head"><b>${esc(PC_NAMES[key] || key)}</b>${btn}</div><p>${esc(f.desc)}</p>${input}<div class="fix-tags">${tags}</div></div>`;
}
VIEWS.pc = {
  mount: () => `<div class="page">
    <div id="r-banner"></div>
    <div class="title-row"><div><h1>My computer</h1><p class="lede" id="r-pclede"></p></div><div id="r-pcmode"></div></div>
    <div id="r-pcconnect"></div>
    <div id="r-pcbody" hidden>
    <section class="vitals-strip" aria-label="This computer's GPU" id="r-pcvitals"></section>
    <div class="grid cols" style="margin-top:var(--gap)">
      <div class="span-7 stack">
        <section class="card" aria-labelledby="ph-h"><div class="card-head"><h2 id="ph-h">Health</h2><button class="btn small primary" type="button" data-action="pc-checkup" id="pc-check">Check my GPU with Nemotron</button></div>
          <div id="r-pcverdict"></div><div id="r-pccheck" style="margin-top:12px"></div></section>
        <section class="card" aria-labelledby="pq-h"><div class="card-head"><h2 id="pq-h">Needs you</h2></div><div id="r-pcqueue"></div></section>
        <section class="card" aria-labelledby="pg-h"><div class="card-head"><h2 id="pg-h">History</h2><div class="toolbar" id="r-pchctl"></div></div><div id="r-pchist"></div></section>
        <section class="card" aria-labelledby="pt-h"><div class="card-head"><h2 id="pt-h">Self-tests</h2><span class="hint">Run on your GPU with PyTorch</span></div><div id="r-pctests"></div></section>
        <section class="card" aria-labelledby="pp-h"><div class="card-head"><h2 id="pp-h">Programs on the GPU</h2></div><div id="r-pcprocs"></div></section>
        <section class="card" aria-labelledby="pd-h"><div class="card-head"><h2 id="pd-h">Driver and Windows</h2></div><div id="r-pcdriver"></div></section>
      </div>
      <div class="span-5 stack">
        <section class="card" aria-labelledby="pf-h"><div class="card-head"><h2 id="pf-h">Fixes</h2><span class="hint">Run one yourself</span></div><div id="r-pcfixes"></div></section>
        <section class="card" aria-labelledby="pb-h"><div class="card-head"><h2 id="pb-h">In the background</h2></div><div id="r-pcbg"></div></section>
        <section class="card" aria-labelledby="pl-h"><div class="card-head"><h2 id="pl-h">Fixes that ran</h2></div><div id="r-pcfixlog"></div></section>
      </div>
    </div></div></div>`,
  update(st) {
    setRegion("pc-banner", $("#r-banner"), banner());
    setRegion("pc-mode", $("#r-pcmode"), modeSwitch(false));
    const p = S.pc;
    if (!p) return;
    const body = $("#r-pcbody");
    if (!p.connected) {
      body.hidden = true;
      setRegion("pc-lede", $("#r-pclede"), "This computer's GPU isn't connected yet.");
      setRegion("pc-connect", $("#r-pcconnect"), `<section class="card"><div class="empty"><b>Connect your NVIDIA GPU.</b>
        <span>Nightingale reads it through nvidia-smi every 2 seconds, watches for heat, memory and driver problems, and offers fixes that run only after you approve them.</span>
        ${S.localError ? `<span class="src-error">${esc(S.localError)}</span>` : ""}
        <div class="toolbar" style="margin-top:6px"><button class="btn primary" type="button" data-action="local-on">${S.localBusy ? "Connecting…" : "Connect this computer's GPU"}</button></div></div></section>`);
      return;
    }
    body.hidden = false;
    setRegion("pc-connect", $("#r-pcconnect"), "");
    const d = p.gpu;
    setRegion("pc-lede", $("#r-pclede"), `<strong>${esc(shortGpu(d.model))}</strong>${d.driver ? `, driver ${esc(d.driver)}` : ""}. ${p.status && p.status.demo ? "<b>Demo laptop:</b> simulated readings and a pretend Windows, so you can try every fix safely." : "Real readings every 2 seconds."} Fixes run only after you approve them.${p.windows ? "" : " Fixes need Windows; this machine can only be watched."}`);
    setRegion("pc-vitals", $("#r-pcvitals"), d.lost ? `<div class="callout bad" style="grid-column:1/-1">This GPU stopped answering the NVIDIA driver.</div>` : pcVitals(d).map(vital).join(""));
    setRegion("pc-verdict", $("#r-pcverdict"), pcVerdict(p));
    setRegion("pc-check", $("#r-pccheck"), pcCheckup());
    const btn = $("#pc-check");
    if (btn) btn.disabled = S.asks.some((e) => e.status === "thinking");
    const local = st.incidents.filter((i) => i.target === LOCAL);
    const q = queueHtml({ ...st, incidents: local, tickets: [] }, { empty: `<div class="empty"><b>Nothing needs you.</b><span>Free local checks run every 2 seconds. Nemotron is only called when something looks wrong, or when you press Check my GPU.</span></div>` });
    setRegion("pc-queue", $("#r-pcqueue"), q);
    const spans = [["1h", "Hour"], ["24h", "Day"], ["7d", "Week"]], metrics = [["temp", "Temp"], ["clock", "Clock"], ["power", "Power"], ["util", "Load"]];
    setRegion("pc-hctl", $("#r-pchctl"), `<div class="seg" role="group" aria-label="Time span">${spans.map(([k, l]) => `<button type="button" data-action="pc-span" data-id="${k}" aria-pressed="${S.pcSpan === k}">${l}</button>`).join("")}</div>
      <div class="seg" role="group" aria-label="Metric">${metrics.map(([k, l]) => `<button type="button" data-action="pc-metric" data-id="${k}" aria-pressed="${S.pcMetric === k}">${l}</button>`).join("")}</div>`);
    const h = S.pcHist && S.pcHist.span === S.pcSpan ? S.pcHist.points : [];
    const pts = h.map((x) => ({ v: x[S.pcMetric] }));
    const label = { "1h": "1 hour ago", "24h": "24 hours ago", "7d": "7 days ago" }[S.pcSpan];
    const hot = h.filter((x) => x.heat_slowdown).length;
    setRegion("pc-hist", $("#r-pchist"), pts.length > 1 ? `${lineChart(pts, { label: `GPU ${S.pcMetric} over the last ${S.pcSpan}`, floor: 0, minSpan: S.pcMetric === "temp" ? 10 : 4, leftLabel: label })}
      <p class="small faint" style="margin-top:8px">${hot ? `Slowed down for heat in ${plural(hot, "stretch", "stretches")} of this period.` : "No heat slowdowns in this period."} Saved on this computer for 7 days.</p>`
      : `<p class="small faint">History builds up while Nightingale runs: one reading every 10 seconds, kept for 7 days.</p>`);
    const light = p.fixes.filter((f) => f.mode === "light").map((f) => fixRow(f, p)).join("");
    const serious = p.fixes.filter((f) => f.mode === "serious").map((f) => fixRow(f, p)).join("");
    setRegion("pc-fixes", $("#r-pcfixes"), `<div class="fix-group"><h3>Light</h3>${light}</div><div class="fix-group"><h3>Serious ${pcMode().mode === "serious" ? `<span class="pill serious">On · ${minutesLeft(pcMode())}</span>` : '<span class="pill">Off</span>'}</h3>${serious}</div>
      ${p.clock_cap ? `<p class="callout watch small">The GPU clock is capped at ${fmt(p.clock_cap)} MHz. Use Remove the clock cap to undo it.</p>` : ""}`);
    const procs = p.processes || [];
    setRegion("pc-procs", $("#r-pcprocs"), procs.length ? `<div class="mini-list">${procs.map((x) => `<div><span class="what"><b>${esc(x.name)}</b><span class="sub">pid ${x.pid} · ${x.mem_mib != null ? `${fmt(x.mem_mib)} MiB` : x.type === "C" ? "compute" : "graphics"}</span></span>
      <button class="btn small ${S.pcConfirm === `close-${x.pid}` ? "primary" : ""}" type="button" data-action="pc-close" data-id="${x.pid}" data-name="${esc(x.name)}">${S.pcConfirm === `close-${x.pid}` ? "Click again to close" : "Close"}</button></div>`).join("")}</div>
      <p class="small faint" style="margin-top:8px">Save your work before closing a program. Windows' own processes can't be closed from here.</p>` : `<p class="muted small">No programs are using the GPU right now (some drivers don't report them).</p>`);
    const drv = p.driver || {};
    const ev = (p.events || []).slice(-5).reverse();
    setRegion("pc-driver", $("#r-pcdriver"), `<div class="stats">
        <div class="stat"><div class="k">NVIDIA driver</div><div class="v" style="font-size:17px">${esc(d.driver || "–")}</div>${drv.date ? `<div class="s">from ${esc(drv.date)}</div>` : ""}</div>
        <div class="stat"><div class="k">Power plan</div><div class="v" style="font-size:17px">${esc(p.power_plan || "–")}</div></div></div>
      <div style="margin-top:12px"><div class="small faint" style="margin-bottom:6px">Driver and hardware errors (last 24 hours)</div>
      ${ev.length ? `<ul class="evidence">${ev.map((e) => `<li>${esc((e.time || "").slice(5, 16))} ${esc(e.provider)} ${e.id ? `event ${e.id}` : ""}: ${esc(e.message)}</li>`).join("")}</ul>` : `<p class="muted small">${p.windows ? "None. Windows hasn't logged NVIDIA driver crashes or hardware errors." : "Windows event logs aren't available on this system."}</p>`}</div>`);
    setRegion("pc-tests", $("#r-pctests"), selfTestsHtml(p));
    setRegion("pc-bg", $("#r-pcbg"), backgroundHtml());
    const log = p.recent_fixes || [];
    setRegion("pc-fixlog", $("#r-pcfixlog"), log.length ? `<div class="mini-list">${log.slice(0, 8).map((x) => `<div><span class="what"><b>${esc(x.label)}</b><span class="sub">${esc(x.result)}</span></span><span class="pill ${x.ok ? "good" : "bad"}">${x.ok ? "Done" : "Failed"}</span></div>`).join("")}</div>`
      : `<p class="muted small">No fixes have run on this computer yet.</p>`);
  },
};
const TEST_INFO = {
  quick: ["Quick check", "About 20 seconds. Does the GPU compute correctly, and how fast?"],
  memory: ["Memory test", "1 to 2 minutes. Writes patterns to most of the free GPU memory and reads them back."],
  stress: ["Stress test", "Full load for a few minutes. Does speed hold up, and do results stay correct?"],
};
function selfTestsHtml(p) {
  const t = S.st;
  if (!t) return `<p class="small faint">Loading…</p>`;
  if (t.running) {
    const tf = t.tflops || [];
    const chart = tf.length > 1 ? lineChart(tf.map((v) => ({ v })), { label: "Speed during the test", floor: 0, minSpan: 2, leftLabel: "start" }) : "";
    const g = p.gpu || {};
    return `<div class="st-run"><div class="q-live"><span class="spinner" aria-hidden="true"></span><b>${esc(t.label)}</b>${t.minutes ? ` (${t.minutes} min)` : ""}: ${esc(t.message)}</div>
      <span class="bar"><i style="width:${t.pct || 0}%"></i></span>
      <p class="small faint">${t.elapsed ?? 0} s · GPU at ${Math.round(g.temp || 0)}°C, ${fmt(g.util || 0)}% load${t.errors ? ` · <span class="watch-text">${t.errors} errors so far</span>` : ""}. Stops by itself at 95°C.</p>
      ${chart ? `<div><div class="small faint" style="margin-bottom:4px">Speed, TFLOPS</div>${chart}</div>` : ""}
      <div><button class="btn small" type="button" data-action="st-stop">Stop the test</button></div></div>`;
  }
  const last = t.result || (t.error ? { ok: false, summary: t.error } : null);
  const res = last ? `<div class="callout ${last.ok ? "good" : "bad"}" style="font-weight:400"><b>${esc(t.label || "Last test")}: ${last.ok ? "passed" : "didn't pass"}.</b> ${esc(last.summary)}</div>` : "";
  const rows = Object.entries(TEST_INFO).map(([k, [name, desc]]) => `<div class="fix"><div class="fix-head"><b>${name}</b>
      <button class="btn small" type="button" data-action="st-run" data-id="${k}" ${p.connected ? "" : "disabled"}>Run</button></div><p>${desc}</p>
      ${k === "stress" ? `<div class="seg" role="group" aria-label="Stress test length">${[1, 3, 10].map((m) => `<button type="button" data-action="st-min" data-id="${m}" aria-pressed="${S.stMinutes === m}">${m} min</button>`).join("")}</div>` : ""}</div>`).join("");
  const hist = (t.history || []).slice(0, 4);
  const past = hist.length ? `<div class="mini-list" style="margin-top:12px">${hist.map((h) => `<div><span class="what"><b>${esc(h.label)}</b><span class="sub">${esc(h.summary)}</span></span><span class="pill ${h.ok ? "good" : h.stopped ? "" : "bad"}">${h.stopped ? "Stopped" : h.ok ? "Passed" : "Failed"}</span></div>`).join("")}</div>` : "";
  return `${res}<div class="fix-group" style="margin-top:${res ? 12 : 0}px">${rows}</div>
    <p class="small faint" style="margin-top:10px">Save your work and close games first. A failed test opens an incident, and Nemotron investigates it.</p>${past}`;
}
function backgroundHtml() {
  const a = S.auto;
  if (!a) return `<p class="small faint">Loading…</p>`;
  const recent = (a.recent_alerts || []).slice(0, 3);
  return `<div class="mini-list">
    <div><span class="what"><b>Start with Windows</b><span class="sub">${a.supported ? "Runs quietly in the tray after you sign in" : "Windows only"}</span></span>
      <button class="btn small ${a.enabled ? "primary" : ""}" type="button" data-action="auto-toggle" ${a.supported ? "" : "disabled"}>${a.enabled ? "On" : "Off"}</button></div>
    <div><span class="what"><b>Alerts</b><span class="sub">${a.tray ? "Windows notifications from the tray icon" : "Shown when Nightingale runs in the tray (Nightingale.cmd)"}</span></span>
      <button class="btn small ${a.alerts ? "primary" : ""}" type="button" data-action="alerts-toggle">${a.alerts ? "On" : "Off"}</button></div>
    <div><span class="what"><b>Alerts for the simulated data center</b><span class="sub">Off keeps drills quiet</span></span>
      <button class="btn small ${a.alerts_datacenter ? "primary" : ""}" type="button" data-action="alerts-dc">${a.alerts_datacenter ? "On" : "Off"}</button></div></div>
    ${recent.length ? `<div class="small faint" style="margin:12px 0 6px">Latest alerts</div><ul class="timeline">${recent.map((r) => `<li>${clock(r.at)} ${esc(r.title)}: ${esc(r.message)}</li>`).join("")}</ul>` : ""}`;
}
async function pollPc(withHistory) {
  try { S.pc = await api("/api/pc"); } catch (e) { return; }
  try { S.st = await api("/api/pc/selftest"); } catch (e) { /* keep */ }
  if (withHistory || !S.auto) { try { S.auto = await api("/api/pc/autostart"); } catch (e) { /* keep */ } }
  if (withHistory || !S.pcHist || Date.now() - (S.pcHistAt || 0) > 30000 || S.pcHist.span !== S.pcSpan) {
    try { S.pcHist = await api(`/api/pc/history?span=${S.pcSpan}`); S.pcHistAt = Date.now(); } catch (e) { /* keep the old one */ }
  }
  if (S.route.name === "pc" && S.state) VIEWS.pc.update(S.state);
}
let stTimer = null;
function stLoop() {  // faster updates while a self-test runs
  clearTimeout(stTimer);
  stTimer = setTimeout(async () => { await pollPc(); if (S.st && S.st.running) stLoop(); }, 1000);
}
async function runPcFix(action, params) {
  try {
    const r = await api("/api/pc/fix", { method: "POST", body: { action, params } });
    toast(`${r.ok ? "" : "Didn't work: "}${r.message}`);
  } catch (e) { toast(e.message); }
  S.pcConfirm = null;
  await pollPc();
}

/* --------------------------------------------------------------- router */
function parseRoute() {
  const h = location.hash.replace(/^#\/?/, "") || "pc";
  const [path, query] = h.split("?");
  const [name, id] = path.split("/");
  const q = Object.fromEntries(new URLSearchParams(query || ""));
  const alias = name === "simulation" ? "sources" : name;
  return { name: VIEWS[alias] ? alias : "pc", id: id ? Number(id) : null, q };
}
function onRoute() {
  const prev = S.route.name;
  S.route = parseRoute();
  if (S.route.name === "fleet") {
    if (S.route.q.rack) { S.fleet.rack = S.route.q.rack; S.fleet.hall = S.route.q.rack[0]; }
    if (S.route.q.server) { S.fleet.server = S.route.q.server; S.fleet.gpu = S.route.q.gpu !== undefined ? Number(S.route.q.gpu) : null; S.fleet.metric = null; }
  }
  closeMenu();
  if (prev !== S.route.name || !$("#main").dataset.view || $("#main").dataset.view !== S.route.name) {
    S.regions = { nav: S.regions.nav, foot: S.regions.foot, topright: S.regions.topright };
    $("#main").innerHTML = VIEWS[S.route.name].mount();
    $("#main").dataset.view = S.route.name;
    if (S.route.name === "logs") { bindLogControls(); renderLogs(true); if (S.analyze) $("#r-analysis").innerHTML = analysisHtml(S.analyze); }
    if (S.route.name === "ask") { S.regions.thread = null; pollAsks(); }
    if (S.route.name === "benchmark") pollBench();
    if (S.route.name === "pc") { pollPc(true); pollAsks(); }
    window.scrollTo(0, 0);
  }
  renderShell();
  render();
}
function render() {
  if (!S.state) {
    $("#main").dataset.view = "";
    if (S.firstLoad) $("#main").innerHTML = `<div class="page"><div class="title-row"><div><div class="sk-line" style="width:180px;height:24px"></div><div class="sk-line" style="width:340px"></div></div></div><div class="grid"><div class="card span-8"><div class="skeleton" style="height:260px"></div></div><div class="card span-4"><div class="skeleton" style="height:260px"></div></div></div></div>`;
    return;
  }
  if ($("#main").dataset.view !== S.route.name) {
    $("#main").innerHTML = VIEWS[S.route.name].mount(); $("#main").dataset.view = S.route.name;
    if (S.route.name === "logs") bindLogControls();
    if (S.route.name === "ask") { S.regions.thread = null; pollAsks(); }
    if (S.route.name === "benchmark") pollBench();
    if (S.route.name === "pc") { pollPc(true); pollAsks(); }
  }
  VIEWS[S.route.name].update(S.state);
}

function bindLogControls() {
  $("#log-q")?.addEventListener("input", (e) => { S.logFilter.q = e.target.value; renderLogs(true); });
  $("#log-warn")?.addEventListener("change", (e) => { S.logFilter.warnOnly = e.target.checked; renderLogs(true); });
}

/* --------------------------------------------------------------- events */
document.addEventListener("click", async (ev) => {
  const el = ev.target.closest("[data-action]");
  if (!el) return;
  const a = el.dataset.action, id = el.dataset.id;
  try {
    if (a === "rack") { S.fleet.rack = id; S.fleet.server = null; S.fleet.gpu = null; S.fleet.metric = null; VIEWS.fleet.update(S.state); }
    else if (a === "gpu") { S.fleet.server = el.dataset.server; S.fleet.gpu = Number(el.dataset.gpu); S.fleet.metric = null; renderRack(); S.gpuDetail = await api(`/api/gpu/${S.fleet.server}/${S.fleet.gpu}`); renderGpu(); }
    else if (a === "metric") { S.fleet.metric = id; renderGpu(); }
    else if (a === "hall") { S.fleet.hall = id; const h = S.state.fleet.find((x) => x.hall === id); S.fleet.rack = h.racks.find((r) => r.watch || r.lost)?.id || h.racks[0].id; S.fleet.server = null; VIEWS.fleet.update(S.state); }
    else if (a === "tab") { S.incTab = el.dataset.tab; S.route.id = null; if (S.route.name === "incidents") { ev.preventDefault(); if (location.hash !== "#/incidents") history.replaceState(null, "", "#/incidents"); VIEWS.incidents.update(S.state); } }
    else if (a === "approve" || a === "approve-all") { el.disabled = true; const inc = await api(`/api/incidents/${id}/approve`, { method: "POST", body: { all: a === "approve-all" } }); const last = inc.actions.filter((x) => x.status !== "pending").pop(); if (last) toast(last.status === "failed" ? `Blocked: ${last.result}` : last.result); await poll(); }
    else if (a === "skip") { el.disabled = true; await api(`/api/incidents/${id}/skip`, { method: "POST" }); await poll(); }
    else if (a === "retry") { el.disabled = true; await api(`/api/incidents/${id}/retry`, { method: "POST" }); toast("Nemotron is investigating again."); await poll(); }
    else if (a === "close") { el.disabled = true; await api(`/api/incidents/${id}/close`, { method: "POST" }); toast("Incident closed."); await poll(); }
    else if (a === "repair") { el.disabled = true; const r = await api(`/api/tickets/${id}/repair`, { method: "POST" }); toast(r.message); await poll(); }
    else if (a === "drill") { el.disabled = true; const r = await api("/api/inject", { method: "POST", body: { kind: id } }); toast(`Drill started: ${r.label} on ${targetName(r.target)}${r.gpu !== null ? `, GPU ${r.gpu}` : ""}. Nightingale doesn't know which.`); await poll(); el.disabled = false; }
    else if (a === "size") { S.sizeChoice = Number(id); VIEWS.sources.update(S.state); }
    else if (a === "local-on") { S.localBusy = true; S.localError = ""; render(); try { const r = await api("/api/local", { method: "POST", body: { enabled: true } }); toast(r.message); } catch (e2) { S.localError = e2.message; } S.localBusy = false; await poll(); }
    else if (a === "fleet-off") { el.disabled = true; await api("/api/fleet", { method: "POST", body: { enabled: false } }); toast("Disconnected from the real servers."); if (S.fleet.hall === "R") S.fleet = { hall: null, rack: null, server: null, gpu: null, metric: null }; await poll(); }
    else if (a === "local-off") { el.disabled = true; await api("/api/local", { method: "POST", body: { enabled: false } }); toast("Disconnected from this laptop's GPU."); if (S.fleet.hall === "L") S.fleet = { hall: null, rack: null, server: null, gpu: null, metric: null }; await poll(); }
    else if (a === "reset") { el.disabled = true; const r = await api("/api/reset", { method: "POST", body: { size: S.sizeChoice || S.state.size } }); S.sizeChoice = null; S.logs = []; S.lastLogId = 0; S.fleet = { hall: null, rack: null, server: null, gpu: null, metric: null }; toast(`Restarted with ${fmt(r.size)} GPUs. Everything is healthy again.`); await poll(); }
    else if (a === "logsrc") { S.logFilter.source = id; document.querySelectorAll('[data-action="logsrc"]').forEach((b) => b.setAttribute("aria-pressed", b.dataset.id === id)); renderLogs(true); }
    else if (a === "pause") { S.logFilter.paused = !S.logFilter.paused; el.textContent = S.logFilter.paused ? "Resume" : "Pause"; if (!S.logFilter.paused) renderLogs(true); }
    else if (a === "sample") { const res = await fetch("/static/sample.log"); $("#paste").value = await res.text(); }
    else if (a === "analyze") { await analyze(); }
    else if (a === "pc-mode") {
      const inc = el.dataset.inc ? Number(el.dataset.inc) : null;
      await api("/api/pc/mode", { method: "POST", body: { mode: id, incident: inc } });
      toast(id === "serious" ? "Serious mode is on for 30 minutes. Every fix still needs your approval." : "Back to light mode.");
      await poll();
    }
    else if (a === "pc-fix") {
      if (S.pcConfirm !== id) { S.pcConfirm = id; VIEWS.pc.update(S.state); setTimeout(() => { if (S.pcConfirm === id) { S.pcConfirm = null; if (S.route.name === "pc") VIEWS.pc.update(S.state); } }, 4000); return; }
      el.disabled = true; el.textContent = "Running…";
      const input = document.querySelector(`[data-param="${id}"]`);
      const params = id === "pc_power_plan" ? { plan: S.pcParams[id] || "balanced" } : id === "pc_performance_mode" ? { mode: S.pcParams[id] || "Turbo" }
        : id === "pc_cap_gpu_clock" ? { mhz: Number(input && input.value) } : id === "pc_set_power_limit" ? { watts: Number(input && input.value) } : {};
      await runPcFix(id, params);
    }
    else if (a === "pc-param") { S.pcParams[el.dataset.fix] = id; VIEWS.pc.update(S.state); }
    else if (a === "pc-close") {
      const key = `close-${id}`;
      if (S.pcConfirm !== key) { S.pcConfirm = key; VIEWS.pc.update(S.state); setTimeout(() => { if (S.pcConfirm === key) { S.pcConfirm = null; if (S.route.name === "pc") VIEWS.pc.update(S.state); } }, 4000); return; }
      el.disabled = true; el.textContent = "Closing…";
      await runPcFix("pc_close_program", { pid: Number(id), name: el.dataset.name });
    }
    else if (a === "pc-span") { S.pcSpan = id; S.pcHist = null; await pollPc(true); }
    else if (a === "pc-metric") { S.pcMetric = id; VIEWS.pc.update(S.state); }
    else if (a === "pc-checkup") { el.disabled = true; try { await api("/api/pc/checkup", { method: "POST" }); } catch (e2) { toast(e2.message); } await pollAsks(); VIEWS.pc.update(S.state); }
    else if (a === "retry-step") { el.disabled = true; await api(`/api/incidents/${id}/retry-step`, { method: "POST", body: { index: Number(el.dataset.index) } }); await poll(); }
    else if (a === "st-run") {
      el.disabled = true;
      try { S.st = await api("/api/pc/selftest", { method: "POST", body: { test: id, minutes: S.stMinutes } }); toast(`${TEST_INFO[id][0]} started.`); } catch (e2) { toast(e2.message); }
      await pollPc(); stLoop();
    }
    else if (a === "st-stop") { el.disabled = true; await api("/api/pc/selftest/stop", { method: "POST" }); await pollPc(); }
    else if (a === "st-min") { S.stMinutes = Number(id); VIEWS.pc.update(S.state); }
    else if (a === "auto-toggle") { el.disabled = true; try { const r = await api("/api/pc/autostart", { method: "POST", body: { enabled: !S.auto.enabled } }); toast(r.message); } catch (e2) { toast(e2.message); } await pollPc(true); }
    else if (a === "alerts-toggle") { await api("/api/pc/alerts", { method: "POST", body: { enabled: !S.auto.alerts } }); await pollPc(true); }
    else if (a === "alerts-dc") { await api("/api/pc/alerts", { method: "POST", body: { enabled: S.auto.alerts, datacenter: !S.auto.alerts_datacenter } }); await pollPc(true); }
    else if (a === "ask-ex") { const input = $("#ask-q"); if (input) input.value = el.dataset.q; await askQuestion(el.dataset.q); }
    else if (a === "bn-count") { S.benchCount = Number(id); VIEWS.benchmark.update(S.state); }
    else if (a === "bn-size") { S.benchSize = Number(id); VIEWS.benchmark.update(S.state); }
    else if (a === "bn-run") { el.disabled = true; S.bench = await api("/api/benchmark", { method: "POST", body: { n: S.benchCount, size: S.benchSize } }); VIEWS.benchmark.update(S.state); pollBench(); }
  } catch (err) {
    toast(err.message);
    el.disabled = false;
  }
});

async function analyze() {
  const text = $("#paste").value.trim(), status = $("#an-status"), btn = $("#an-btn");
  if (text.length < 20) { status.textContent = "Paste at least a few log lines first."; return; }
  btn.disabled = true;
  status.innerHTML = `<span class="spinner" style="display:inline-block;vertical-align:-2px"></span> Diagnosing. Large models can take 10 to 30 seconds.`;
  try {
    S.analyze = await api("/api/analyze", { method: "POST", body: { logs: text } });
    $("#r-analysis").innerHTML = analysisHtml(S.analyze);
    status.textContent = "";
  } catch (e) {
    status.textContent = `${e.message} Your logs are still in the box; try again.`;
  } finally { btn.disabled = false; }
}

$("#search").addEventListener("submit", (e) => {
  e.preventDefault();
  const v = $("#search-input").value.trim();
  if (!v || !S.state) return;
  const server = v.match(/dgx-?0*(\d+)/i);
  const rack = v.match(/\b([A-H])-?(\d{1,2})\b/i);
  if (server) { const name = `dgx-${String(server[1]).padStart(4, "0")}`; location.hash = `#/fleet?rack=${rackOfServer(name)}&server=${name}`; }
  else if (rack) location.hash = `#/fleet?rack=${rack[1].toUpperCase()}-${rack[2].padStart(2, "0")}`;
  else if (/^(laptop|this (laptop|computer)|local|my computer)$/i.test(v)) location.hash = "#/pc";
  else if (isReal(v)) location.hash = `#/fleet?rack=${realRack(v)}&server=${encodeURIComponent(v)}`;
  else if (S.state.jobs.some((j) => j.id === v)) location.hash = "#/overview";
  else return toast(`Nothing called "${v}". Try a server like dgx-0053, a rack like A-07${S.state.local ? ", or laptop" : ""}.`);
  $("#search-input").value = "";
});

function closeMenu() { $("#sidebar").classList.remove("open"); $("#scrim").hidden = true; $("#menu-btn").setAttribute("aria-expanded", "false"); }
$("#menu-btn").addEventListener("click", () => { const open = !$("#sidebar").classList.contains("open"); $("#sidebar").classList.toggle("open", open); $("#scrim").hidden = !open; $("#menu-btn").setAttribute("aria-expanded", String(open)); });
$("#scrim").addEventListener("click", closeMenu);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
window.addEventListener("hashchange", onRoute);

/* ---------------------------------------------------------------- polling */
let polling = false;
async function poll() {
  if (polling) return;
  polling = true;
  try {
    S.state = await api("/api/state");
    S.ok = true; S.lastOk = Date.now(); S.firstLoad = false;
  } catch (e) {
    S.ok = false;
  } finally { polling = false; }
  renderShell();
  render();
  if (S.route.name === "pc") pollPc();
}
async function pollLogs() {
  try {
    const { logs } = await api(`/api/logs?since=${S.lastLogId}`);
    if (logs.length) {
      if (logs[0].id < S.lastLogId) S.logs = [];
      S.logs.push(...logs);
      S.lastLogId = logs[logs.length - 1].id;
      if (S.logs.length > 1500) S.logs.splice(0, S.logs.length - 1500);
      if (S.route.name === "logs") renderLogs();
    }
  } catch (e) { /* the state poll shows the banner */ }
}

onRoute();
poll();
pollLogs();
setInterval(poll, 2000);
setInterval(pollLogs, 1500);
