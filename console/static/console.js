// RiskFabric console: polls the read-only /api routes and draws small inline SVG charts. No libraries.
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, d = 1) => (v == null || Number.isNaN(v) ? "-" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: 0 }));
const kv = (pairs) => pairs.map(([k, v]) => `<div><span>${esc(k)}</span><b>${v}</b></div>`).join("");
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// time-axis labels under a chart: left = window start, right = now; optional peak in the middle
const timeStr = (sec) => new Date(sec * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const axisRow = (peak) => `<div class="axis"><span>${timeStr(Date.now() / 1000 - WINDOW)}</span>${peak == null ? "" : `<span>peak ${fmt(peak, 0)} / bin</span>`}<span>now</span></div>`;
// severity class for a metric against its target bands (null leaves it unstyled)
const sev = (v, okMax, warnMax) => (v == null ? "" : v <= okMax ? "m-ok" : v <= warnMax ? "m-warn" : "m-bad");
const sevWrap = (text, cls) => (cls ? `<span class="${cls}">${text}</span>` : text);

// ---- selected time window (seconds), driven by the toolbar segmented control ----
let WINDOW = 60;
let LAST_HOLDS = [];   // latest hold queue, so the quantum panel can show live shadow-rescoring coverage
const WIN_LABELS = { 60: "last 60 s", 300: "last 5 min", 900: "last 15 min", 3600: "last 1 h" };
function setWindowLabels() {
  const label = WIN_LABELS[WINDOW] || `last ${WINDOW} s`;
  document.querySelectorAll(".win").forEach((el) => { el.innerHTML = label; });
}

async function get(path) {
  try { const r = await fetch(path, { cache: "no-store" }); return r.ok ? await r.json() : null; } catch (e) { return null; }
}

// Stacked bar chart from a series of {key: n} rows. Wider windows return fewer, time-binned columns.
function bars(series, keys, colors, height = 92) {
  const w = 600, n = Math.max(series.length, 1), bw = w / n;
  const max = Math.max(1, ...series.map((s) => keys.reduce((a, k) => a + (s[k] || 0), 0)));
  const grid = css("--line-2");
  let rects = "";
  series.forEach((s, i) => {
    let y = height;
    keys.forEach((k, j) => {
      const h = ((s[k] || 0) / max) * (height - 14);
      if (h > 0) { y -= h; rects += `<rect x="${(i * bw).toFixed(1)}" y="${y.toFixed(1)}" width="${Math.max(bw - 1, 0.6).toFixed(1)}" height="${h.toFixed(1)}" fill="${colors[j]}" rx="1"><title>${esc(k)}: ${s[k] || 0}</title></rect>`; }
    });
  });
  // faint mid gridline + solid baseline for scale reference
  const grids = `<line x1="0" y1="${((height - 14) / 2).toFixed(1)}" x2="${w}" y2="${((height - 14) / 2).toFixed(1)}" stroke="${grid}" stroke-width="0.5" stroke-dasharray="2 3" opacity="0.6"/><line x1="0" y1="${height - 0.5}" x2="${w}" y2="${height - 0.5}" stroke="${grid}" stroke-width="1"/>`;
  return `<svg viewBox="0 0 ${w} ${height}" width="100%" height="${height}" preserveAspectRatio="none" role="img" aria-label="time series">${grids}${rects}</svg>${axisRow(max)}`;
}

async function loadPanel() {
  const d = await get(`/api/threat?n=${WINDOW}`);
  if (!d) return;
  let entries = d.entries || [];
  const last = entries[entries.length - 1];
  const level = last ? last.level : "NORMAL";
  const badge = $("level"); badge.textContent = level; badge.className = "badge lvl-" + level;
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
  const color = { NORMAL: css("--normal"), SURGE: css("--surge"), COORDINATED: css("--coord") };
  const w = 600, h = 34, bw = w / Math.max(entries.length, 1);
  const rects = entries.map((e, i) => `<rect x="${(i * bw).toFixed(1)}" y="0" width="${Math.max(bw - 0.3, 0.4).toFixed(1)}" height="${h}" fill="${color[e.level] || color.NORMAL}"><title>${new Date(e.ts * 1000).toLocaleTimeString()} ${e.level}, ${e.n_clusters} cluster(s)</title></rect>`).join("");
  $("load-chart").innerHTML = entries.length ? `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" preserveAspectRatio="none" role="img" aria-label="load level timeline">${rects}</svg>${axisRow(null)}` : `<p class="muted">No threat entries yet (Sentinel writes one per second).</p>`;
  const cl = last ? last.clusters : [];
  $("clusters").innerHTML = cl.length ? "Active clusters: " + cl.map((c) => `<span class="mono">${esc(c.id)}</span> ${esc(c.key_type)} → <b>${esc(c.action)}</b> (share ${fmt(100 * (c.share || 0))}%, risk ${fmt(c.risk, 2)})`).join(",&nbsp; ") : "No active clusters.";
}

const SYSTEM_NAMES = { bank_only: "Bank alone", bank_realistic: "Realistic bank", shield: "Bank + Shield",
  standalone: "Standalone", submitted_design: "Downgrade baseline" };

async function metaBadge() {
  const d = await get("/api/meta");
  $("system").textContent = d && d.system ? (SYSTEM_NAMES[d.system] || d.system) : "not set";
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
  $("edge-chart").innerHTML = d.series.length ? bars(d.series, ["passed", "throttled", "blocked"], [css("--bar1"), css("--bar2"), css("--bar3")]) : `<p class="muted">No edge traffic in this window.</p>`;
  const t = d.totals;
  const all = t.passed + t.throttled + t.blocked;
  $("edge-totals").innerHTML = kv([["passed", fmt(t.passed, 0)], ["throttled", fmt(t.throttled, 0)], ["blocked", fmt(t.blocked, 0)], ["shed share", all ? fmt((100 * (t.throttled + t.blocked)) / all) + "%" : "-"]]);
}

async function decisionsPanel() {
  const d = await get(`/api/decisions?seconds=${WINDOW}`);
  if (!d) return;
  $("dec-chart").innerHTML = (d.series && d.series.length) ? bars(d.series, ["ALLOW", "VERIFY", "HOLD"], [css("--ok"), css("--warn"), css("--bad")], 80) : `<p class="muted">No decisions in this window.</p>`;
  const c = d.counts;
  $("decisions").innerHTML = kv([["allow", fmt(c.ALLOW, 0)], ["verify", fmt(c.VERIFY, 0)], ["hold", fmt(c.HOLD, 0)], ["stand-in", fmt(d.stand_in, 0)]]);
  $("paths").textContent = "Paths: " + (Object.entries(d.paths).map(([k, v]) => `${k} ${v}`).join(", ") || "none yet") + ` (${fmt(d.total, 0)} decisions in window)`;
  $("hold-alert").innerHTML = d.hold_alert ? `<span class="alert">Hold rate ${fmt(d.hold_rate_60s_pct, 2)}% over the last 60 s is above the cap of ${fmt(d.hold_cap_pct)}%.</span>` : `<span class="muted">Hold rate ${fmt(d.hold_rate_60s_pct, 2)}% over the last 60 s (cap ${fmt(d.hold_cap_pct)}%).</span>`;
  const l = d.latency_ms;
  // p99 vs the 50 ms client target / 100 ms deadline; any deadline miss is amber, none is green
  $("latency").innerHTML = kv([["p50", fmt(l.p50) + " ms"],
    ["p95", sevWrap(fmt(l.p95) + " ms", sev(l.p95, 50, 100))],
    ["p99", sevWrap(fmt(l.p99) + " ms", sev(l.p99, 50, 100))],
    ["deadline misses", sevWrap(fmt(d.deadline_miss, 0), d.deadline_miss == null ? "" : d.deadline_miss === 0 ? "m-ok" : "m-warn")]]);
}

async function bankPanel() {
  const d = await get("/api/bank");
  if (!d) return;
  const b = d.books || {};
  $("bank").innerHTML = kv([["posted", fmt(b.posted_count, 0)], ["posted value", fmt(b.posted_value, 0)], ["timeout fallback", fmt(b.fail_open_count, 0)], ["engine p99", fmt(d.engine_p99_ms) + " ms"]]);
  $("bank-note").textContent = d.reachable ? `Recent bank timeouts: ${d.recent_timeouts}; recent postings by the timeout fallback: ${d.recent_fail_open}.` : "Bank not reachable on its books endpoint.";
}

// Reason strings become code chips; cluster and stand-in chips are tinted. The case note is the plain-language line.
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
  const rows = (d.holds || []).map((x) => {
    const ch = x.challenger ? (x.challenger.status === "ok" ? `${fmt(x.challenger.score, 3)} <span class="muted">${esc(x.challenger.model || "")}</span>` : `<span class="muted">${esc(x.challenger.status)}</span>`) : `<span class="muted">-</span>`;
    return `<tr><td><span class="mono">${esc(x.txn_id)}</span><br><span class="muted">${esc(x.from_acct)} → ${esc(x.to_acct)}</span></td>
      <td class="num">${fmt(x.amount, 2)} ${esc(x.currency)}</td>
      <td class="num">${x.stand_in ? '<span class="muted">stand-in</span>' : fmt(x.score, 3)}</td>
      <td class="num">${ch}</td>
      <td>${reasonChips(x.reasons, x.stand_in)}<div class="note">${esc(x.note)}</div></td></tr>`;
  });
  $("holds").innerHTML = rows.length ? rows.join("") : `<tr><td colspan="5" class="muted">No held payments yet.</td></tr>`;
}

async function ledgerPanel() {
  const d = await get("/api/ledger");
  if (!d) return;
  $("ledger").innerHTML = kv([["records", fmt(d.records, 0)], ["head", `<span class="mono">${esc((d.head || "-").slice(0, 12))}…</span>`]]);
}

// Quantum challenger result: the pre-registered gate verdict (quantum/results/summary.json) plus live
// shadow re-scoring coverage read from the current hold queue. Never implies the challenger changes a decision.
async function quantumPanel() {
  const d = await get("/api/quantum/summary");
  const scored = LAST_HOLDS.filter((h) => h.challenger && h.challenger.status === "ok" && h.challenger.score != null);
  const avg = scored.length ? scored.reduce((a, h) => a + h.challenger.score, 0) / scored.length : null;
  const model = (scored[0] && scored[0].challenger.model) || "qp-1";
  const backend = (scored[0] && scored[0].challenger.backend) || "statevector";
  const cov = LAST_HOLDS.length ? `${scored.length} of ${LAST_HOLDS.length} held payments re-scored in shadow` : "no held payments in the queue right now";
  const tile = (label, val) => `<div class="q-tile"><span>${esc(label)}</span><b>${val}</b></div>`;
  if (d && !d.empty && d.verdict) {
    const rows = d.rows || [];
    const best = rows[0] || {};
    $("q-verdict").textContent = d.verdict;
    $("q-big").textContent = best.recall_at_cap != null ? fmt(100 * best.recall_at_cap, 1) + "%" : (avg != null ? fmt(avg, 3) : "-");
    $("q-sub").textContent = d.statement || "Verdict from the pre-registered gate against matched classical controls.";
    const ci = Array.isArray(best.diff_ci95) ? ` [${fmt(best.diff_ci95[0], 3)}, ${fmt(best.diff_ci95[1], 3)}]` : "";
    $("q-tiles").innerHTML = [
      tile("model", esc(best.model || model)),
      best.recall_at_cap != null ? tile("recall @ cap", fmt(100 * best.recall_at_cap, 1) + "%") : "",
      best.control ? tile("vs control " + esc(best.control), (best.diff_vs_control != null ? fmt(best.diff_vs_control, 3) : "-") + ci) : "",
      tile("held re-scored", `${scored.length} / ${LAST_HOLDS.length}`),
    ].join("");
    $("q-note").textContent = cov + ". Shadow only; the quantum challenger never changes a decision.";
  } else {
    $("q-verdict").textContent = "Live shadow re-scoring";
    $("q-big").textContent = avg != null ? fmt(avg, 3) : "-";
    $("q-sub").textContent = "The pre-registered gate verdict is not published yet. Held payments are being re-scored by the challenger in shadow right now.";
    $("q-tiles").innerHTML = [
      tile("held re-scored", `${scored.length} / ${LAST_HOLDS.length}`),
      tile("avg challenger", avg != null ? fmt(avg, 3) : "-"),
      tile("model", esc(model)),
      tile("backend", esc(backend)),
    ].join("");
    $("q-note").textContent = "Run the gate on the VM to publish the verdict. Shadow only; the challenger never changes a decision.";
  }
}

async function shieldPanel() {
  const d = await get("/api/shield");
  if (!d) return;
  $("shield").innerHTML = kv([["compared", fmt(d.pairs, 0)], ["agreement", d.agreement == null ? "-" : fmt(100 * d.agreement) + "%"], ["shadow would hold", fmt(d.shadow_would_hold, 0)], ["shadow would release", fmt(d.shadow_would_release, 0)]]);
}

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

function tick() { loadPanel(); perimeterPanel(); edgePanel(); decisionsPanel(); bankPanel(); ledgerPanel(); }
setWindowLabels();
tick(); holdsPanel(); shieldPanel(); metaBadge(); quantumPanel();
setInterval(metaBadge, 5000);
setInterval(tick, 1000);
setInterval(holdsPanel, 2000);
setInterval(shieldPanel, 5000);
setInterval(quantumPanel, 3000);
