// RiskFabric console: polls the read-only /api routes and draws small inline SVG charts. No libraries.
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, d = 1) => (v == null || Number.isNaN(v) ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: 0 }));
const kv = (pairs) => pairs.map(([k, v]) => `<div><span>${esc(k)}</span><b>${v}</b></div>`).join("");
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

async function get(path) {
  try { const r = await fetch(path, { cache: "no-store" }); return r.ok ? await r.json() : null; } catch (e) { return null; }
}

function bars(series, keys, colors, height = 90) {
  const w = 600, n = Math.max(series.length, 1), bw = w / n;
  const max = Math.max(1, ...series.map((s) => keys.reduce((a, k) => a + (s[k] || 0), 0)));
  let rects = "";
  series.forEach((s, i) => {
    let y = height;
    keys.forEach((k, j) => {
      const h = ((s[k] || 0) / max) * (height - 12);
      if (h > 0) { y -= h; rects += `<rect x="${(i * bw).toFixed(1)}" y="${y.toFixed(1)}" width="${Math.max(bw - 1, 1).toFixed(1)}" height="${h.toFixed(1)}" fill="${colors[j]}"><title>${esc(k)}: ${s[k]}</title></rect>`; }
    });
  });
  return `<svg viewBox="0 0 ${w} ${height}" width="100%" height="${height}" preserveAspectRatio="none" role="img" aria-label="bar chart">${rects}</svg><div class="muted" style="font-size:11px">peak ${fmt(max, 0)} requests/s</div>`;
}

async function loadPanel() {
  const d = await get("/api/threat?n=120");
  if (!d) return;
  const entries = d.entries || [];
  const last = entries[entries.length - 1];
  const level = last ? last.level : "NORMAL";
  const badge = $("level"); badge.textContent = level; badge.className = "badge lvl-" + level;
  const color = { NORMAL: css("--normal"), SURGE: css("--surge"), COORDINATED: css("--coord") };
  const w = 600, h = 34, bw = w / Math.max(entries.length, 1);
  const rects = entries.map((e, i) => `<rect x="${(i * bw).toFixed(1)}" y="0" width="${Math.max(bw - 0.5, 0.5).toFixed(1)}" height="${h}" fill="${color[e.level] || color.NORMAL}"><title>${new Date(e.ts * 1000).toLocaleTimeString()} ${e.level}, ${e.n_clusters} cluster(s)</title></rect>`).join("");
  $("load-chart").innerHTML = entries.length ? `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" preserveAspectRatio="none" role="img" aria-label="load level over the last ${entries.length} seconds">${rects}</svg>` : `<p class="muted">No threat entries yet (Sentinel writes one per second).</p>`;
  const cl = last ? last.clusters : [];
  $("clusters").innerHTML = cl.length ? "Active clusters: " + cl.map((c) => `<span class="mono">${esc(c.id)}</span> ${esc(c.key_type)} → <b>${esc(c.action)}</b> (share ${fmt(100 * (c.share || 0))}%, risk ${fmt(c.risk, 2)})`).join(" · ") : "No active clusters.";
}

async function edgePanel() {
  const d = await get("/api/edge");
  if (!d) return;
  $("edge-chart").innerHTML = d.series.length ? bars(d.series, ["passed", "throttled", "blocked"], [css("--bar1"), css("--bar2"), css("--bar3")]) : `<p class="muted">No edge traffic in the last 60 s.</p>`;
  const t = d.totals;
  const all = t.passed + t.throttled + t.blocked;
  $("edge-totals").innerHTML = kv([["passed", fmt(t.passed, 0)], ["throttled", fmt(t.throttled, 0)], ["blocked", fmt(t.blocked, 0)], ["shed share", all ? fmt((100 * (t.throttled + t.blocked)) / all) + "%" : "–"]]);
}

async function decisionsPanel() {
  const d = await get("/api/decisions");
  if (!d) return;
  const c = d.counts;
  $("decisions").innerHTML = kv([["allow", fmt(c.ALLOW, 0)], ["verify", fmt(c.VERIFY, 0)], ["hold", fmt(c.HOLD, 0)], ["stand-in", fmt(d.stand_in, 0)]]);
  $("paths").textContent = "Paths: " + (Object.entries(d.paths).map(([k, v]) => `${k} ${v}`).join(", ") || "none yet") + ` (last ${d.total} decisions)`;
  $("hold-alert").innerHTML = d.hold_alert ? `<span class="alert">Hold rate ${fmt(d.hold_rate_60s_pct, 2)}% over the last 60 s is above the cap of ${fmt(d.hold_cap_pct)}%.</span>` : `<span class="muted">Hold rate ${fmt(d.hold_rate_60s_pct, 2)}% (cap ${fmt(d.hold_cap_pct)}%).</span>`;
  const l = d.latency_ms;
  $("latency").innerHTML = kv([["p50", fmt(l.p50) + " ms"], ["p95", fmt(l.p95) + " ms"], ["p99", fmt(l.p99) + " ms"], ["deadline misses", fmt(d.deadline_miss, 0)]]);
}

async function bankPanel() {
  const d = await get("/api/bank");
  if (!d) return;
  const b = d.books || {};
  $("bank").innerHTML = kv([["posted", fmt(b.posted_count, 0)], ["posted value", fmt(b.posted_value, 0)], ["timeout fallback (books)", fmt(b.fail_open_count, 0)], ["engine p99", fmt(d.engine_p99_ms) + " ms"]]);
  $("bank-note").textContent = d.reachable ? `Recent bank timeouts: ${d.recent_timeouts}; recent postings by the timeout fallback: ${d.recent_fail_open}.` : "Bank not reachable on its books endpoint.";
}

async function holdsPanel() {
  const d = await get("/api/holds?n=50");
  if (!d) return;
  if (d.challenger_label) $("challenger-label").textContent = d.challenger_label;
  const rows = (d.holds || []).map((x) => {
    const ch = x.challenger ? (x.challenger.status === "ok" ? fmt(x.challenger.score, 3) : `<span class="muted">${esc(x.challenger.status)}</span>`) : `<span class="muted">–</span>`;
    return `<tr><td><span class="mono">${esc(x.txn_id)}</span><br><span class="muted">${esc(x.from_acct)} → ${esc(x.to_acct)}</span></td>
      <td class="num">${fmt(x.amount, 2)} ${esc(x.currency)}</td>
      <td class="num">${x.stand_in ? '<span class="muted">stand-in</span>' : fmt(x.score, 3)}</td>
      <td class="num">${ch}</td><td>${esc(x.note)}</td></tr>`;
  });
  $("holds").innerHTML = rows.length ? rows.join("") : `<tr><td colspan="5" class="muted">No held payments yet.</td></tr>`;
}

async function ledgerPanel() {
  const d = await get("/api/ledger");
  if (!d) return;
  $("ledger").innerHTML = kv([["records", fmt(d.records, 0)], ["head", `<span class="mono">${esc((d.head || "–").slice(0, 12))}…</span>`]]);
}

async function shieldPanel() {
  const d = await get("/api/shield");
  if (!d) return;
  $("shield").innerHTML = kv([["compared", fmt(d.pairs, 0)], ["agreement", d.agreement == null ? "–" : fmt(100 * d.agreement) + "%"], ["shadow would hold", fmt(d.shadow_would_hold, 0)], ["shadow would release", fmt(d.shadow_would_release, 0)]]);
}

$("verify-btn").addEventListener("click", async () => {
  $("verify-result").textContent = "checking…";
  try {
    const r = await fetch("/api/ledger/verify", { method: "POST" });
    const d = await r.json();
    $("verify-result").innerHTML = d.ok ? `<span class="pass">Valid: ${fmt(d.records, 0)} records</span>` : `<span class="fail">Broken at seq ${esc(d.bad_seq)}: ${esc(d.reason)}</span>`;
  } catch (e) { $("verify-result").textContent = "verify failed to run"; }
});

function tick() { loadPanel(); edgePanel(); decisionsPanel(); bankPanel(); ledgerPanel(); }
tick(); holdsPanel(); shieldPanel();
setInterval(tick, 1000);
setInterval(holdsPanel, 2000);
setInterval(shieldPanel, 5000);
