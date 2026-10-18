// Scoreboard (prd/02 section 9): renders eval/results/summary.json and quantum/results/summary.json as they are.
// It computes no metrics; the only arithmetic is the display difference between two shown values.
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const SYSTEMS = [["bank_only", "Bank alone"], ["bank_realistic", "Realistic bank"], ["shield", "Bank + Shield"], ["standalone", "Standalone"], ["submitted_design", "Downgrade baseline"]];
const SCENARIOS = [
  ["S1", "Normal day: replay at the base rate."], ["S2", "Payday surge (3R) with network delay and CPU stress."],
  ["S3", "Coordinated abusive clusters at 4R plus card testers."], ["S4", "Smokescreen: S3 plus laundering sent through the attacker's infrastructure."],
  ["S4b", "Smokescreen with laundering from clean identities."], ["S5", "Bank engine brownout: service time x5 for 90 s."],
  ["S6", "Borrowed fingerprint: bots use the bank app's own fingerprint, with laundering sent through them."],
];
const num = (d) => (v) => (v == null ? null : Number(v).toLocaleString(undefined, { maximumFractionDigits: d }));
const pct = (v) => (v == null ? null : (100 * Number(v)).toLocaleString(undefined, { maximumFractionDigits: 2 }) + "%");
const ms = (v) => (v == null ? null : Number(v).toLocaleString(undefined, { maximumFractionDigits: 1 }) + " ms");
const yesno = (v) => (v == null ? null : v ? "valid" : "BROKEN");
const ROWS = [
  ["Scores (0 to 100)"],
  ["shield_score", "Shield score (mean of the three below)", num(1)], ["protection", "Protection", num(1)],
  ["customer", "Customer", num(1)], ["timeliness", "Timeliness", num(1)], ["integrity", "Integrity", num(0)],
  ["Security"],
  ["fraud_value_sent", "M-01 Fraud value sent", num(0)], ["fraud_value_through", "M-02 Fraud value posted by the bank", num(0)],
  ["fraud_count_through", "M-03 Fraud payments posted", num(0)], ["fail_open_value", "M-04 Bank fallback value (fail-open or bounded)", num(0)],
  ["attack_shed_share", "M-05 Attack shed at the edge", pct], ["attack_reached_bank", "M-06 Attack requests reaching the bank", num(0)],
  ["Customers"],
  ["legit_completed_rate", "M-07 Legit payments completed", pct], ["legit_held_per_1000", "M-08 Legit held per 1,000", num(2)],
  ["legit_blocked_per_1000", "M-09 Legit blocked per 1,000", num(2)], ["legit_verify_rate", "M-10 Legit step-up rate", pct],
  ["Performance"],
  ["p50_ms", "M-11 Client p50", ms], ["p95_ms", "M-11 Client p95", ms], ["p99_ms", "M-11 Client p99", ms],
  ["deadline_miss_rate", "M-12 Deadline misses", pct], ["shield_overhead_ms", "M-13 Shield overhead (S1)", ms],
  ["bank_engine_p99_ms", "M-14 Bank engine p99", ms], ["bank_timeouts", "M-15 Bank timeouts", num(0)],
  ["Resilience"],
  ["stand_in_count", "M-16 Stand-in decisions", num(0)], ["time_to_detect_s", "M-17 Time to detect (s)", num(1)],
  ["false_coordinated_s", "M-18 False COORDINATED seconds", num(0)],
  ["Integrity"],
  ["ledger_ok", "M-19 Ledger verify", yesno], ["reconcile_mismatch", "M-20 Books vs decisions mismatches", num(0)],
  ["Cost (CPU microseconds per request)"],
  ["cpu_edge", "M-21 Edge", num(1)], ["cpu_decision", "M-21 Decision", num(1)], ["cpu_sentinel", "M-21 Sentinel", num(1)],
  ["cpu_bank", "M-21 Bank", num(1)], ["cpu_quantum", "M-21 Quantum worker", num(1)],
  ["Shadow uplift (Shield runs)"],
  ["shadow_fraud_count", "M-22 Fraud posted that our shadow score would have held", num(0)],
  ["shadow_fraud_value", "M-22 Value of that fraud", num(0)], ["shadow_legit_would_hold_per_1000", "M-22 Legit it would wrongly hold, per 1,000", num(2)],
];

async function get(path) {
  try { const r = await fetch(path, { cache: "no-store" }); return r.ok ? await r.json() : null; } catch (e) { return null; }
}
// like get(), but keeps the body of a 404 (the summary route explains what is missing)
async function getAny(path) {
  try { const r = await fetch(path, { cache: "no-store" }); return await r.json(); } catch (e) { return null; }
}

let SUMMARY = null, QUANTUM = null, CURRENT = "S4";

// Not applicable by design (prd/02 sections 4 and 5): the two bank systems have no ledger and no Sentinel.
const BANK_SYSTEMS = ["bank_only", "bank_realistic"];
const NA_FOR_BANK = { integrity: "n/a, no ledger", ledger_ok: "n/a, no ledger", time_to_detect_s: "n/a, no Sentinel" };

function cellHTML(cell, key, f, system) {
  const ran = cell && cell.median;
  if (ran && BANK_SYSTEMS.includes(system) && NA_FOR_BANK[key] && cell.median[key] == null) return `<td class="num na">${esc(NA_FOR_BANK[key])}</td>`;
  if (!ran || cell.median[key] === undefined || cell.median[key] === null) return `<td class="num na">not run</td>`;
  const v = f(cell.median[key]);
  const lo = cell.min && cell.min[key] != null ? f(cell.min[key]) : null, hi = cell.max && cell.max[key] != null ? f(cell.max[key]) : null;
  const t = cell.targets && cell.targets[key];
  const mark = t ? (t.pass === true ? ` <span class="pass">✓ pass</span>` : t.pass === false ? ` <span class="fail">✗ fail</span>` : "") : "";
  const range = lo != null && hi != null && lo !== hi ? `<span class="range">${esc(lo)} to ${esc(hi)}</span>` : "";
  const target = t ? `<span class="range">target: ${esc(t.target)}</span>` : "";
  const runs = cell.valid_runs != null ? `${cell.valid_runs} valid run(s)` : "";
  const tip = `${runs}${cell.runs ? ": " + cell.runs.join(", ") : ""}\n${cell.command || ""}`;
  return `<td class="num" tabindex="0" title="${esc(tip)}">${esc(v)}${mark}${range}${target}</td>`;
}

function renderMatrix() {
  const scen = SUMMARY.matrix[CURRENT] || {};
  let html = `<thead><tr><th>Metric</th>${SYSTEMS.map(([, n]) => `<th class="num">${esc(n)}</th>`).join("")}</tr></thead><tbody>`;
  for (const row of ROWS) {
    if (row.length === 1) { html += `<tr class="group-row"><td colspan="${SYSTEMS.length + 1}">${esc(row[0])}</td></tr>`; continue; }
    const [key, label, f] = row;
    html += `<tr><td>${esc(label)}</td>${SYSTEMS.map(([id]) => cellHTML(scen[id], key, f, id)).join("")}</tr>`;
  }
  $("matrix").innerHTML = html + "</tbody>";
}

// Headline card: bank + Shield as the big number, the two bank set-ups beneath, and the change from the fairest
// baseline that was run (the realistic bank, else the bank alone).
function card(title, key, f, betterLow) {
  const scen = SUMMARY.matrix[CURRENT] || {};
  const val = (id) => (scen[id] && scen[id].median && scen[id].median[key] != null ? scen[id].median[key] : null);
  const a = val("bank_only"), r = val("bank_realistic"), b = val("shield");
  const [base, baseName] = r != null ? [r, "realistic bank"] : [a, "bank alone"];
  let delta = "";
  if (base != null && b != null) {
    const d = b - base;
    const good = betterLow ? d < 0 : d > 0;
    const rel = base !== 0 ? ` (${(100 * d / Math.abs(base)).toLocaleString(undefined, { maximumFractionDigits: 1 })}%)` : "";
    delta = `<div class="d"><span class="tag ${d === 0 ? "plain" : good ? "ok" : "bad"}">vs ${esc(baseName)}: ${d > 0 ? "+" : ""}${esc(f(d))}${esc(rel)}</span></div>`;
  }
  const show = (label, v) => `<span>${esc(label)}</span><b>${esc(v == null ? "not run" : f(v))}</b>`;
  return `<div class="card sc-card"><h3>${esc(title)}</h3><div class="ours-v" title="Bank + Shield">${esc(b == null ? "not run" : f(b))}</div>
    <div class="rows">${show("Bank alone", a)}${show("Realistic bank", r)}</div>${delta}</div>`;
}

function renderCards() {
  $("cards").innerHTML = [card("Shield score", "shield_score", num(1), false), card("Fraud value posted", "fraud_value_through", num(0), true),
    card("Legit completed", "legit_completed_rate", pct, false), card("Client p99", "p99_ms", ms, true),
    card("Bank engine p99", "bank_engine_p99_ms", ms, true)].join("");
}

function renderTabs() {
  $("tabs").innerHTML = SCENARIOS.map(([id]) => `<button role="tab" id="tab-${id}" aria-selected="${id === CURRENT}" tabindex="${id === CURRENT ? 0 : -1}">${id}</button>`).join("");
  SCENARIOS.forEach(([id], i) => {
    const el = $("tab-" + id);
    el.addEventListener("click", () => select(id));
    el.addEventListener("keydown", (e) => {
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        const next = SCENARIOS[(i + (e.key === "ArrowRight" ? 1 : SCENARIOS.length - 1)) % SCENARIOS.length][0];
        select(next); $("tab-" + next).focus();
      }
    });
  });
}

function select(id) {
  CURRENT = id;
  renderTabs();
  $("scenario-desc").textContent = (SCENARIOS.find((s) => s[0] === id) || [, ""])[1];
  renderCards(); renderMatrix();
}

function renderCapacity() {
  const c = SUMMARY.capacity;
  if (!c) { $("capacity").innerHTML = `<p class="note">not run</p>`; return; }
  const max = Math.max(c.cores_score_all || 0, c.cores_cascade || 0, 1);
  const bar = (label, v, cls) => `<div class="row"><span>${esc(label)}</span><b>${esc(num(1)(v))} cores</b></div>
    <div class="track" style="margin-bottom:12px"><div class="fill ${cls}" style="width:${(100 * (v || 0) / max).toFixed(1)}%"></div></div>`;
  const saving = c.cores_score_all ? 100 * (1 - c.cores_cascade / c.cores_score_all) : null;
  const per = c.cpu_us_per_req || {};
  $("capacity").innerHTML = (saving == null ? "" : `<div class="big-row"><span class="big">${esc(num(1)(saving))}%</span><span class="tag ${saving >= 60 ? "ok" : "warn"}">cores saved · target ≥ 60%</span></div>`) +
    `<div class="meter">${bar("Score everything", c.cores_score_all, "p")}${bar("Cascade (RiskFabric)", c.cores_cascade, "b")}</div>` +
    `<p class="note">CPU µs per request: ${Object.entries(per).map(([k, v]) => `${esc(k)} ${esc(num(1)(v))}`).join(", ")}</p>` +
    `<p class="note mono">${esc(c.command || "")}</p>`;
}

// Model quality: the bank's rules alone (what a bank runs today), the plain model and ours, on the same test rows.
function renderModel() {
  const m = SUMMARY.model;
  if (!m || !m.gfp_gbm) { $("model").innerHTML = `<p class="note">not run: <span class="mono">python -m scoring train --source hi_small --path …</span></p>`; return; }
  const f3 = num(3);
  const row = (n, x, ours) => `<tr><td>${ours ? `<b>${esc(n)}</b>` : esc(n)}</td><td class="num">${esc(pct(x.precision) || "-")}</td><td class="num">${esc(pct(x.recall) || "-")}</td>
    <td class="num">${esc(f3(x.f1) || "-")}</td><td class="num">${esc(f3(x.pr_auc) || "-")}</td><td class="num">${esc(pct(x.recall_at_1pct) || "-")}</td></tr>`;
  const rules = m.rules_only ? row("Bank rules alone (R1 to R3)", m.rules_only) : "";
  const lift = m.rules_only && m.rules_only.recall != null && m.gfp_gbm.recall != null
    ? `<span class="tag ${m.gfp_gbm.recall > m.rules_only.recall ? "ok" : "plain"}">recall ${esc(pct(m.gfp_gbm.recall))} vs rules ${esc(pct(m.rules_only.recall))}</span>` : "";
  $("model").innerHTML = `<div class="scroll"><table><thead><tr><th>Model</th><th class="num">Precision</th><th class="num">Recall</th><th class="num">F1</th><th class="num">PR-AUC</th><th class="num">Recall at top 1%</th></tr></thead>
    <tbody>${rules}${m.raw_gbm ? row("raw-gbm-v1 (baseline, the bank's model)", m.raw_gbm) : ""}${row("gfp-gbm-v1 (ours, graph features)", m.gfp_gbm, true)}</tbody></table></div>
    <p class="note">${lift} Precision, recall and F1 at the tuned hold threshold; the rules give a decision, not a ranking, so they have no PR-AUC. ${m.synthetic ? "<b>Synthetic data: checks the pipeline only.</b> " : ""}Inference p99: ${esc(ms(m.p99_ms) || "-")} · source ${esc(m.source || "")}</p>`;
}

// No scenario runs yet: say what is missing and how to fill the page, and show the model report if it exists.
function renderSetup(info) {
  const step = (done, title, cmd, note) => `<li><span><b>${esc(title)}</b>${done ? ' <span class="tag ok">done</span>' : ""}<br><code>${esc(cmd)}</code>${note ? `<br><span class="cell-note">${esc(note)}</span>` : ""}</span></li>`;
  const haveModel = !!(info.model && info.model.gfp_gbm);
  $("empty").hidden = false;
  $("empty").className = "";
  $("empty").innerHTML = `<div class="bento">
    <section class="card s-7"><div class="card-h"><h2>No scenario results yet <small>the matrix fills from eval/results/summary.json</small></h2></div>
      <ol class="steps">
        ${step(haveModel, "Train the classical model", "python -m scoring train --source hi_small --path $RF_DATA/replay/HI-Small_Trans.csv --max-rows 500000")}
        ${step(false, "Export the quantum candidates", "python -m scoring export --source hi_small --path $RF_DATA/replay/HI-Small_Trans.csv --max-rows 500000")}
        ${step(false, "Run the scenario matrix", "eval/scenarios/run_all.sh 200", info.deploy_present === false ? "Needs deploy/start.sh, stop.sh and reset.sh, which are not in this repo yet." : "")}
        ${step(false, "Rebuild this page's data", "python -m eval.scenarios.summary")}
      </ol></section>
    <section class="card s-5"><div class="card-h"><h2>Model quality <small>from scoring/report.json</small></h2></div><div id="model"></div></section></div>`;
  SUMMARY = { model: info.model };
  renderModel();
}

async function main() {
  [SUMMARY, QUANTUM] = await Promise.all([getAny("/api/results/summary"), get("/api/quantum/summary")]);
  window.RFQuantum.renderResults($("quantum-results"), QUANTUM && !QUANTUM.empty ? QUANTUM : null);
  if (!SUMMARY || !SUMMARY.matrix) { renderSetup(SUMMARY || {}); return; }
  $("content").hidden = false;
  $("host").textContent = `${SUMMARY.host || "?"} · ${SUMMARY.arch || "?"} · AI accelerator: ${SUMMARY.nnpa ? "present" : "not present"} · commit ${SUMMARY.commit || "?"} · generated ${SUMMARY.generated_ts ? new Date(SUMMARY.generated_ts * 1000).toLocaleString() : "?"}`;
  $("footer").innerHTML = `Every number comes from <span class="mono">eval/results/</span>. Rebuild with <span class="mono">python -m eval.scenarios.summary</span>; full tables in <span class="mono">eval/RESULTS.md</span>.`;
  select(SUMMARY.matrix.S4 ? "S4" : Object.keys(SUMMARY.matrix)[0] || "S4");
  renderCapacity(); renderModel();
}
main();
