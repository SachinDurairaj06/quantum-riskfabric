// RiskFabric console: polls the read-only /api routes and draws small inline SVG charts. No libraries.
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, d = 1) => (v == null || Number.isNaN(v) ? "-" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: 0 }));
const compact = (v) => (v == null || Number.isNaN(v) ? "-" : Number(v).toLocaleString(undefined, { notation: "compact", maximumFractionDigits: 1 }));
const kv = (pairs) => pairs.map(([k, v]) => `<div><span>${esc(k)}</span><b>${v}</b></div>`).join("");
const timeStr = (sec) => new Date(sec * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
// severity class for a metric against its target bands (null leaves it unstyled)
const sev = (v, okMax, warnMax) => (v == null ? "" : v <= okMax ? "m-ok" : v <= warnMax ? "m-warn" : "m-bad");
const sevWrap = (text, cls) => (cls ? `<span class="${cls}">${text}</span>` : text);
const meter = (rows) => rows.map(([label, n, total, cls]) => `<div class="row"><span>${esc(label)}</span><b>${fmt(n, 0)}</b></div>
  <div class="track" style="margin-bottom:12px"><div class="fill ${cls}" style="width:${total ? Math.max(n ? 1.5 : 0, (100 * n) / total).toFixed(1) : 0}%"></div></div>`).join("");

// ---- selected time window (seconds), driven by the segmented control ----
let WINDOW = 60;
let LAST_HOLDS = [];   // latest hold queue, shared with the quantum panels
const LAST = { edge: null, dec: null, threat: null };
const WIN_LABELS = { 60: "last 60 s", 300: "last 5 min", 900: "last 15 min", 3600: "last 1 h" };
function setWindowLabels() {
  const label = WIN_LABELS[WINDOW] || `last ${WINDOW} s`;
  document.querySelectorAll(".win").forEach((el) => { el.textContent = label; });
}

async function get(path) {
  try { const r = await fetch(path, { cache: "no-store" }); return r.ok ? await r.json() : null; } catch (e) { return null; }
}

// Group a series into at most n columns by summing a value function over each group.
function groups(series, n, val) {
  if (!series.length) return [];
  const per = Math.ceil(series.length / n), out = [];
  for (let i = 0; i < series.length; i += per) {
    const chunk = series.slice(i, i + per);
    out.push({ ts: chunk[0].ts, v: chunk.reduce((a, s) => a + val(s), 0) });
  }
  return out;
}

// ---------------- payment funnel ----------------
const FUNNEL = { cols: [], active: null, hover: null };
function funnelData(edge, dec) {
  const t = (edge && edge.totals) || { passed: 0, throttled: 0, blocked: 0 };
  const c = (dec && dec.counts) || { ALLOW: 0, VERIFY: 0, HOLD: 0 };
  return [["Reached the edge", t.passed + t.throttled + t.blocked], ["Passed the edge", t.passed],
    ["Decided", (dec && dec.total) || 0], ["Not held", c.ALLOW + c.VERIFY], ["Allowed", c.ALLOW]];
}
function niceMax(v) {
  if (v <= 0) return 10;
  const p = Math.pow(10, Math.floor(Math.log10(v))), m = v / p;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p;
}
function drawFunnel() {
  const cols = FUNNEL.cols;
  const svg = $("funnel-svg"), box = $("funnel");
  const W = Math.max(320, box.clientWidth), H = 200, AX = 34, base = H - 2, top = 10;
  const colW = (W - AX) / cols.length;
  const max = niceMax(Math.max(1, ...cols.map((c) => c[1])));
  const Y = (v) => base - (v / max) * (base - top);
  // default highlight: the step with the biggest drop-off, else the decided column
  let def = 2, worst = -1;
  for (let i = 1; i < cols.length; i++) {
    const prev = cols[i - 1][1], drop = prev ? 1 - cols[i][1] / prev : 0;
    if (drop > worst + 1e-9) { worst = drop; def = i; }
  }
  const act = FUNNEL.hover != null ? FUNNEL.hover : def;
  FUNNEL.active = act;
  let s = `<defs>
    <pattern id="hatch" width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="7" height="7" fill="#dfe8ff"/><line x1="0" y1="0" x2="0" y2="7" stroke="#3d6cf6" stroke-width="2.2"/></pattern>
    <linearGradient id="solid" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3d6cf6"/><stop offset="1" stop-color="#1f4fe0"/></linearGradient>
    <linearGradient id="flow" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#cfdcff" stop-opacity=".9"/><stop offset="1" stop-color="#eef3ff" stop-opacity=".5"/></linearGradient>
    <linearGradient id="colhl" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#e9efff" stop-opacity="0"/><stop offset="1" stop-color="#e9efff"/></linearGradient></defs>`;
  for (let k = 0; k <= 4; k++) {
    const v = (max * k) / 4, y = Y(v);
    s += `<line x1="${AX}" x2="${W}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" stroke="#eef0f4" stroke-width="1"/>`;
    if (k) s += `<text x="0" y="${(y + 4).toFixed(1)}" fill="#8b90a0" font-size="10.5">${esc(compact(v))}</text>`;
  }
  s += `<rect x="${(AX + act * colW).toFixed(1)}" y="0" width="${colW.toFixed(1)}" height="${H}" fill="url(#colhl)"/>`;
  cols.forEach(([, v], i) => {
    const x0 = AX + i * colW, bw = colW * 0.66, y = Y(v);
    if (i < cols.length - 1) {
      const nx = AX + (i + 1) * colW, ny = Y(cols[i + 1][1]);
      s += `<path d="M${(x0 + bw).toFixed(1)} ${y.toFixed(1)} C ${(x0 + bw + (nx - x0 - bw) * 0.5).toFixed(1)} ${y.toFixed(1)}, ${(x0 + bw + (nx - x0 - bw) * 0.5).toFixed(1)} ${ny.toFixed(1)}, ${nx.toFixed(1)} ${ny.toFixed(1)} L ${nx.toFixed(1)} ${base} L ${(x0 + bw).toFixed(1)} ${base} Z" fill="url(#flow)"/>`;
    }
    const h = Math.max(0, base - y);
    s += `<rect x="${x0.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" fill="${i === act ? "url(#solid)" : "url(#hatch)"}"/>`;
    if (h > 0) s += `<rect x="${(x0 + bw / 2 - 11).toFixed(1)}" y="${(y - 3).toFixed(1)}" width="22" height="6" rx="3" fill="${i === act ? "#fff" : "#b9cbff"}" stroke="#3d6cf6" stroke-opacity=".35"/>`;
  });
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.innerHTML = s;
  // update the column labels in place so keyboard focus survives the once-a-second refresh
  const head = $("funnel-cols");
  if (head.children.length !== cols.length + 1) {
    head.innerHTML = `<div></div>` + cols.map((_, i) => `<div class="fc" tabindex="0" data-i="${i}"><span></span><b></b></div>`).join("");
  }
  cols.forEach(([label, v], i) => {
    const fc = head.children[i + 1];
    fc.classList.toggle("on", i === act);
    fc.firstChild.textContent = label;
    fc.lastChild.textContent = compact(v);
  });
  const tip = $("funnel-tip");
  const [, v] = cols[act], prev = act ? cols[act - 1][1] : null;
  if (cols.some((c) => c[1] > 0)) {
    const conv = prev ? v / prev : null;
    tip.innerHTML = `<b>${esc(compact(v))}</b> ${act >= 2 ? "payments" : "requests"}` + (conv == null ? "" : ` · Conversion: <b>${fmt(100 * conv, 0)}%</b> · Drop-off: <b>${conv >= 1 ? "0" : "-" + fmt(100 * (1 - conv), 0)}%</b>`);
    tip.hidden = false;
    // centred over the bar, but kept inside the card on narrow screens
    const half = tip.offsetWidth / 2, cx = ((AX + act * colW + colW * 0.33) / W) * box.clientWidth;
    tip.style.left = `${Math.min(Math.max(cx, half), box.clientWidth - half)}px`;
    tip.style.top = `${$("funnel-cols").offsetHeight + Y(v) - 10}px`;
  } else tip.hidden = true;
}
function bindFunnel() {
  const box = $("funnel");
  const pick = (clientX) => {
    const r = box.getBoundingClientRect(), colW = (r.width - 34) / FUNNEL.cols.length;
    const i = Math.floor((clientX - r.left - 34) / colW);
    return i >= 0 && i < FUNNEL.cols.length ? i : null;
  };
  box.addEventListener("mousemove", (e) => { const i = pick(e.clientX); if (i !== FUNNEL.hover) { FUNNEL.hover = i; drawFunnel(); } });
  box.addEventListener("mouseleave", () => { FUNNEL.hover = null; drawFunnel(); });
  $("funnel-cols").addEventListener("focusin", (e) => { const el = e.target.closest("[data-i]"); if (el && FUNNEL.hover !== +el.dataset.i) { FUNNEL.hover = +el.dataset.i; drawFunnel(); } });
  $("funnel-cols").addEventListener("focusout", (e) => { if (!$("funnel-cols").contains(e.relatedTarget)) { FUNNEL.hover = null; drawFunnel(); } });
}

// ---------------- traffic: stepped line with a vertical hatch ----------------
function drawTraffic() {
  const d = LAST.edge;
  const el = $("traffic-step");
  const series = d ? groups(d.series, 26, (s) => s.passed + s.throttled + s.blocked) : [];
  if (!series.length) { el.innerHTML = `<p class="note">No edge traffic in this window.</p>`; $("traffic-axis").innerHTML = ""; return; }
  const W = Math.max(200, el.clientWidth), H = 210, top = 34, base = H - 4;
  const max = Math.max(1, ...series.map((s) => s.v));
  const step = W / series.length;
  const Y = (v) => base - (v / max) * (base - top) * 0.92;
  let path = "", hatch = "", peak = 0;
  series.forEach((s, i) => {
    const x0 = i * step, x1 = x0 + step, y = Y(s.v);
    path += (i ? ` L ${x0.toFixed(1)} ${y.toFixed(1)}` : `M 0 ${y.toFixed(1)}`) + ` L ${x1.toFixed(1)} ${y.toFixed(1)}`;
    for (let x = x0 + 2; x < x1; x += 4) hatch += `<line x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${(y + 3).toFixed(1)}" y2="${base}"/>`;
    if (s.v > series[peak].v) peak = i;
  });
  const px = peak * step + step / 2, py = Y(series[peak].v);
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Requests at the edge per time bin">
    <g stroke="#f4b8cf" stroke-width="1">${hatch}</g>
    <path d="${path}" fill="none" stroke="#e8488a" stroke-width="2.2" stroke-linejoin="round"/>
    <line x1="${px}" x2="${px}" y1="${py}" y2="${base}" stroke="#e8488a" stroke-width="1" stroke-dasharray="2 3"/>
    <circle cx="${px}" cy="${py}" r="4" fill="#fff" stroke="#e8488a" stroke-width="2"/>
    <g transform="translate(${Math.min(Math.max(px, 30), W - 30)},${Math.max(py - 24, 12)})"><rect x="-28" y="-11" width="56" height="20" rx="10" fill="#fff" stroke="#ebecf0"/><text text-anchor="middle" y="3" font-size="10.5" font-weight="600" fill="#111318">${esc(compact(series[peak].v))}</text></g>
  </svg>`;
  $("traffic-axis").innerHTML = `<span>${timeStr(series[0].ts)}</span><span>peak / bin</span><span>now</span>`;
}

// ---------------- dot matrices ----------------
function dots(el, series, color, label) {
  const COLS = 14, ROWS = 5, gap = 12.5, r = 4.2;
  const g = groups(series, COLS, (s) => s.v);
  if (!g.length || !g.some((x) => x.v > 0)) { el.innerHTML = `<span class="note">nothing in this window</span>`; return; }
  const max = Math.max(...g.map((x) => x.v));
  let peak = 0;
  g.forEach((x, i) => { if (x.v > g[peak].v) peak = i; });
  const W = g.length * gap, H = ROWS * gap;
  let s = "";
  g.forEach((x, i) => {
    const n = x.v > 0 ? Math.max(1, Math.round((x.v / max) * ROWS)) : 0;
    for (let k = 0; k < ROWS; k++) {
      const on = k < n, cx = i * gap + gap / 2, cy = H - k * gap - gap / 2;
      s += `<circle cx="${cx}" cy="${cy}" r="${i === peak && on ? r + 0.6 : r}" fill="${on ? color : "#eef0f4"}" opacity="${on ? (i === peak ? 1 : 0.42) : 0.9}"/>`;
    }
  });
  el.innerHTML = `<span class="peak" style="left:calc(50% - ${(W / 2).toFixed(1)}px + ${(peak * gap + gap / 2).toFixed(1)}px)">${esc(label)}: <b>${timeStr(g[peak].ts).slice(0, 5)}</b></span>
    <svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(label)} per time bin">${s}</svg>`;
}

// ---------------- panels ----------------
async function loadPanel() {
  const d = await get(`/api/threat?n=${WINDOW}`);
  if (!d) return;
  LAST.threat = d;
  let entries = d.entries || [];
  const last = entries[entries.length - 1];
  const level = last ? last.level : "NORMAL";
  const badge = $("level"); badge.textContent = level; badge.className = "tag lvl-" + level;
  // Keep the strip readable: collapse to at most ~200 columns, each the worst level in its bin.
  const RANK = { NORMAL: 0, SURGE: 1, COORDINATED: 2 }, NAME = ["NORMAL", "SURGE", "COORDINATED"];
  if (entries.length > 200) {
    const binw = Math.ceil(entries.length / 200), binned = [];
    for (let i = 0; i < entries.length; i += binw) {
      const chunk = entries.slice(i, i + binw);
      const worst = chunk.reduce((a, e) => Math.max(a, RANK[e.level] || 0), 0);
      binned.push({ ts: chunk[0].ts, level: NAME[worst], n_clusters: chunk[chunk.length - 1].n_clusters });
    }
    entries = binned;
  }
  const color = { NORMAL: "#1fa463", SURGE: "#e59a1a", COORDINATED: "#e0453f" };
  const w = 600, h = 30, bw = w / Math.max(entries.length, 1);
  const rects = entries.map((e, i) => `<rect x="${(i * bw).toFixed(1)}" y="0" width="${Math.max(bw - 0.4, 0.4).toFixed(1)}" height="${h}" fill="${color[e.level] || color.NORMAL}" opacity="${e.level === "NORMAL" ? 0.55 : 0.95}"><title>${new Date(e.ts * 1000).toLocaleTimeString()} ${e.level}, ${e.n_clusters} cluster(s)</title></rect>`).join("");
  $("load-chart").innerHTML = entries.length ? `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="load level timeline">${rects}</svg>` : `<p class="note">No threat entries yet (Sentinel writes one per second).</p>`;
  $("load-axis").innerHTML = entries.length ? `<span>${timeStr(entries[0].ts)}</span><span>now</span>` : "";
  const cl = last ? last.clusters : [];
  $("clusters").innerHTML = cl.length ? "Active clusters: " + cl.map((c) => `<span class="mono">${esc(c.id)}</span> ${esc(c.key_type)} → <b>${esc(c.action)}</b> (share ${fmt(100 * (c.share || 0))}%, risk ${fmt(c.risk, 2)})`).join(",&nbsp; ") : "No active clusters.";
  sentinelLine();
}

// one plain sentence for the funnel card, from the newest Sentinel reading and the biggest drop in the funnel
function sentinelLine() {
  const d = LAST.threat;
  const last = d && d.entries && d.entries[d.entries.length - 1];
  if (!last) { $("sentinel-line").textContent = "No Sentinel reading yet (it writes one per second)."; return; }
  const cls = { NORMAL: "blue", SURGE: "", COORDINATED: "red" }[last.level] || "";
  let s = `Load is <span class="hl ${cls}">${esc(last.level)}</span>`;
  const cl = last.clusters || [];
  s += cl.length ? ` with ${last.n_clusters} active cluster${last.n_clusters === 1 ? "" : "s"}: ` + cl.slice(0, 2).map((c) => `<b>${esc(c.id)}</b> (${esc(c.key_type)}, ${esc(c.action)}, ${fmt(100 * (c.share || 0), 0)}% of traffic)`).join(", ") + "." : " and no coordinated cluster is active.";
  const cols = FUNNEL.cols, i = FUNNEL.active;
  if (cols.length && i > 0 && cols[i - 1][1] > 0 && cols[i][1] < cols[i - 1][1]) {
    s += ` Biggest drop: <span class="hl">${esc(cols[i - 1][0].toLowerCase())} → ${esc(cols[i][0].toLowerCase())}</span>, ${fmt(100 * (1 - cols[i][1] / cols[i - 1][1]), 0)}%.`;
  }
  $("sentinel-line").innerHTML = s;
}

const SYSTEM_NAMES = { bank_only: "Bank alone", bank_realistic: "Realistic bank", shield: "Bank + Shield",
  standalone: "Standalone", submitted_design: "Downgrade baseline" };

async function metaBadge() {
  const d = await get("/api/meta");
  const name = d && d.system ? (SYSTEM_NAMES[d.system] || d.system) : "not set";
  $("system").textContent = name;
  $("sys-line").textContent = d && d.system ? `Running: ${name}` : "System not set";
}

async function perimeterPanel() {
  const d = await get("/api/perimeter");
  if (!d || !d.reachable) {
    $("perimeter").innerHTML = "";
    $("perimeter-note").textContent = "No perimeter running: traffic enters at the edge.";
    return;
  }
  const s = d.stopped || {};
  const count = (layer) => Object.values(s[layer] || {}).reduce((a, b) => a + b, 0);
  $("perimeter").innerHTML = kv([["received", fmt(d.received, 0)], ["passed on", fmt(d.passed, 0)],
    ["stopped", fmt(count("cdn") + count("waf") + count("gateway"), 0)],
    ["passed share", d.received ? fmt((100 * d.passed) / d.received) + "%" : "-"]]);
  const reasons = Object.entries(s).flatMap(([layer, r]) => Object.entries(r).map(([why, n]) => `${layer}: ${why} ${fmt(n, 0)}`));
  $("perimeter-note").textContent = (reasons.length ? reasons.join(", ") + ". " : "")
    + "It can't link identities, so bots that each stay under the per-IP rate pass on to the edge.";
}

async function edgePanel() {
  const d = await get(`/api/edge?seconds=${WINDOW}`);
  if (!d) return;
  LAST.edge = d;
  const t = d.totals;
  const all = t.passed + t.throttled + t.blocked;
  $("edge-totals").innerHTML = kv([["passed", fmt(t.passed, 0)], ["throttled", fmt(t.throttled, 0)], ["blocked", fmt(t.blocked, 0)], ["shed share", all ? fmt((100 * (t.throttled + t.blocked)) / all) + "%" : "-"]]);
  $("edge-meter").innerHTML = meter([["Passed", t.passed, all, "g"], ["Throttled (429)", t.throttled, all, "a"], ["Blocked (403)", t.blocked, all, "p"]]);
  $("edge-passed").textContent = compact(t.passed);
  $("edge-shed").textContent = (t.throttled + t.blocked ? "+" : "") + fmt(t.throttled + t.blocked, 0);
  dots($("edge-dots"), d.series.map((s) => ({ ts: s.ts, v: s.passed })), "#1fa463", "Peak");
  drawTraffic();
  FUNNEL.cols = funnelData(LAST.edge, LAST.dec); drawFunnel();
}

async function decisionsPanel() {
  const d = await get(`/api/decisions?seconds=${WINDOW}`);
  if (!d) return;
  LAST.dec = d;
  const c = d.counts;
  $("dec-total").textContent = compact(d.total);
  const hr = $("dec-hold-rate");
  hr.textContent = `hold ${fmt(d.hold_rate_60s_pct, 2)}% · cap ${fmt(d.hold_cap_pct)}%`;
  hr.className = "delta " + (d.hold_alert ? "alert" : "");
  hr.title = d.hold_alert ? "Hold rate over the last 60 s is above the cap" : "Hold rate over the last 60 s, within the cap";
  $("dec-meter").innerHTML = meter([["Allow", c.ALLOW, d.total, "g"], ["Verify (step-up)", c.VERIFY, d.total, "b"], ["Hold", c.HOLD, d.total, "p"], ["Stand-in rules", d.stand_in, d.total, "a"]]);
  $("dec-paths").textContent = "Paths: " + (Object.entries(d.paths).map(([k, v]) => `${k} ${fmt(v, 0)}`).join(", ") || "none yet") + `. Deadline misses: ${fmt(d.deadline_miss, 0)}.`;
  $("held-count").textContent = compact(c.HOLD);
  $("held-standin").textContent = fmt(d.stand_in, 0);
  dots($("held-dots"), (d.series || []).map((s) => ({ ts: s.ts, v: s.HOLD || 0 })), "#2f62f5", "Highest");
  const l = d.latency_ms;
  // p99 vs the 50 ms client target / 100 ms deadline; any deadline miss is amber, none is green
  $("latency").innerHTML = kv([["p50", fmt(l.p50) + " ms"],
    ["p95", sevWrap(fmt(l.p95) + " ms", sev(l.p95, 50, 100))],
    ["p99", sevWrap(fmt(l.p99) + " ms", sev(l.p99, 50, 100))],
    ["deadline misses", sevWrap(fmt(d.deadline_miss, 0), d.deadline_miss == null ? "" : d.deadline_miss === 0 ? "m-ok" : "m-warn")]]);
  FUNNEL.cols = funnelData(LAST.edge, LAST.dec); drawFunnel();
}

async function bankPanel() {
  const d = await get("/api/bank");
  if (!d) return;
  const b = d.books || {};
  $("bank").innerHTML = kv([["posted", fmt(b.posted_count, 0)], ["posted value", compact(b.posted_value)], ["timeout fallback", fmt(b.fail_open_count, 0)], ["engine p99", fmt(d.engine_p99_ms) + " ms"]]);
  $("bank-note").textContent = d.reachable ? `Recent bank timeouts: ${d.recent_timeouts}; recent postings by the timeout fallback: ${d.recent_fail_open}.` : "Bank not reachable on its books endpoint.";
}

// Reason strings become chips; cluster and stand-in chips are tinted. The case note is the plain-language line.
function reasonChips(reasons, standIn) {
  const chips = (reasons || []).slice(0, 6).map((r) => {
    const cls = /^cluster /i.test(r) ? " c-cluster" : /stand-in/i.test(r) ? " c-standin" : "";
    return `<span class="chip${cls}">${esc(r)}</span>`;
  });
  if (standIn && !(reasons || []).some((r) => /stand-in/i.test(r))) chips.unshift(`<span class="chip c-standin">stand-in</span>`);
  return chips.length ? `<div class="chips">${chips.join("")}</div>` : "";
}

async function holdsPanel() {
  const d = await get("/api/holds?n=50");
  if (!d) return;
  LAST_HOLDS = d.holds || [];
  if (d.challenger_label) $("challenger-label").textContent = d.challenger_label;
  const rows = LAST_HOLDS.map((x) => {
    const ch = x.challenger ? (x.challenger.status === "ok" ? `<span class="score-pill q">${fmt(x.challenger.score, 3)}</span><div class="cell-note">${esc(x.challenger.model || "")}</div>` : `<span class="muted">${esc(x.challenger.status)}</span>`) : `<span class="muted">-</span>`;
    return `<tr><td><span class="mono">${esc(x.txn_id)}</span><div class="cell-note">${esc(x.from_acct)} → ${esc(x.to_acct)}</div></td>
      <td class="num">${fmt(x.amount, 2)} ${esc(x.currency)}</td>
      <td class="num">${x.stand_in ? '<span class="muted">stand-in</span>' : `<span class="score-pill">${fmt(x.score, 3)}</span>`}</td>
      <td class="num">${ch}</td>
      <td>${reasonChips(x.reasons, x.stand_in)}<div class="cell-note">${esc(x.note)}</div></td></tr>`;
  });
  $("holds").innerHTML = rows.length ? rows.join("") : `<tr><td colspan="5" class="muted">No held payments yet.</td></tr>`;
}

async function ledgerPanel() {
  const d = await get("/api/ledger");
  if (!d) return;
  $("ledger").innerHTML = kv([["records", fmt(d.records, 0)], ["head", `<span class="mono">${esc((d.head || "-").slice(0, 12))}…</span>`]]);
}

async function shieldPanel() {
  const d = await get("/api/shield");
  if (!d) return;
  $("shield").innerHTML = kv([["compared", fmt(d.pairs, 0)], ["agreement", d.agreement == null ? "-" : fmt(100 * d.agreement) + "%"], ["shadow would hold", fmt(d.shadow_would_hold, 0)], ["shadow would release", fmt(d.shadow_would_release, 0)]]);
}

// ---------------- quantum: insight card + results section ----------------
const SLIDES = { list: [], i: 0, key: "", timer: null, MS: 6000 };
function showSlide(i) {
  const L = SLIDES.list;
  if (!L.length) return;
  SLIDES.i = ((i % L.length) + L.length) % L.length;
  const s = L[SLIDES.i];
  $("insight-slide").outerHTML = `<div class="slide" id="insight-slide"><div class="ins-big">${esc(s.big)}</div><h3>${esc(s.title)}</h3><p>${esc(s.text)}</p></div>`;
  $("insight-bars").innerHTML = L.map((_, k) => `<button type="button" role="tab" aria-label="Insight ${k + 1}" aria-selected="${k === SLIDES.i}" class="${k < SLIDES.i ? "done" : k === SLIDES.i ? "run" : ""}" data-k="${k}"><i></i></button>`).join("");
  clearTimeout(SLIDES.timer);
  SLIDES.timer = setTimeout(() => showSlide(SLIDES.i + 1), SLIDES.MS);
}
let QSUM = null;
async function quantumPanel() {
  const d = await get("/api/quantum/summary");
  QSUM = d && !d.empty ? d : null;
  const list = window.RFQuantum.slides(QSUM, LAST_HOLDS);
  const key = JSON.stringify(list);
  if (key !== SLIDES.key) {
    const first = !SLIDES.list.length;
    SLIDES.list = list; SLIDES.key = key;
    if (first) showSlide(0);
    else if (SLIDES.i < list.length) { const s = list[SLIDES.i]; const el = $("insight-slide"); el.querySelector(".ins-big").textContent = s.big; el.querySelector("h3").textContent = s.title; el.querySelector("p").textContent = s.text; }
    else showSlide(0);
  }
  window.RFQuantum.renderResults($("quantum-results"), QSUM, LAST_HOLDS);
}
$("insight-bars").addEventListener("click", (e) => { const b = e.target.closest("button[data-k]"); if (b) showSlide(+b.dataset.k); });

$("verify-btn").addEventListener("click", async () => {
  $("verify-result").textContent = "checking…";
  try {
    const r = await fetch("/api/ledger/verify", { method: "POST" });
    const d = await r.json();
    $("verify-result").innerHTML = d.ok ? `<span class="pass">Valid: ${fmt(d.records, 0)} records</span>` : `<span class="fail">Broken at seq ${esc(d.bad_seq)}: ${esc(d.reason)}</span>`;
  } catch (e) { $("verify-result").textContent = "verify failed to run"; }
});

// Window selector: switch the window, repaint the windowed panels at once.
$("window-seg").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-win]");
  if (!btn) return;
  WINDOW = parseInt(btn.dataset.win, 10) || 60;
  $("window-seg").querySelectorAll("button").forEach((b) => b.classList.toggle("is-active", b === btn));
  setWindowLabels();
  loadPanel(); edgePanel(); decisionsPanel();
});

// Nav pill follows the section in view.
(function navSpy() {
  const links = [...document.querySelectorAll("#nav a[href^='#']")];
  const secs = links.map((a) => document.querySelector(a.getAttribute("href"))).filter(Boolean);
  if (!("IntersectionObserver" in window) || !secs.length) return;
  const io = new IntersectionObserver((ents) => {
    ents.forEach((en) => {
      if (en.isIntersecting) links.forEach((a) => a.classList.toggle("is-active", a.getAttribute("href") === "#" + en.target.id));
    });
  }, { rootMargin: "-30% 0px -60% 0px" });
  secs.forEach((s) => io.observe(s));
})();

let resizeT = null;
window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => { drawFunnel(); drawTraffic(); }, 120); });

function tick() { loadPanel(); perimeterPanel(); edgePanel(); decisionsPanel(); bankPanel(); ledgerPanel(); }
setWindowLabels();
FUNNEL.cols = funnelData(null, null);
bindFunnel(); drawFunnel();
tick(); holdsPanel().then(quantumPanel); shieldPanel(); metaBadge();
setInterval(metaBadge, 5000);
setInterval(tick, 1000);
setInterval(holdsPanel, 2000);
setInterval(shieldPanel, 5000);
setInterval(quantumPanel, 3000);
