// Quantum challenger results, shared by the console and the scoreboard. Renders quantum/results/summary.json as it
// is (prd/05 Part 7): per-model means over the seeds, the matched comparisons with their 95% intervals, and the gate's
// own checks. The only arithmetic is averaging the seeds for display; the verdict always comes from the gate.
"use strict";
(function () {
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pct = (v, d = 1) => (v == null || Number.isNaN(v) ? "-" : (100 * v).toFixed(d) + "%");
  const pts = (v, d = 2) => (v == null ? "-" : (v >= 0 ? "+" : "") + (100 * v).toFixed(d) + " pts");
  const num = (v, d = 3) => (v == null || Number.isNaN(v) ? "-" : Number(v).toFixed(d));
  const mean = (a) => { const x = a.filter((v) => v != null && !Number.isNaN(v)); return x.length ? x.reduce((s, v) => s + v, 0) / x.length : null; };

  // What each model is (quantum/research/models.py), and which kind of bar it gets.
  const INFO = {
    "QP-1": ["quantum", "Projected quantum features + classical score"],
    "QK-1": ["quantum", "Fidelity quantum kernel, SVM"],
    "QP-1a": ["quantum", "Projected quantum features only"],
    "K4": ["classical", "Classical score + 15 random Fourier features (matched to QP-1)"],
    "K1": ["classical", "RBF-kernel SVM (matched to QK-1)"],
    "K2": ["classical", "15 random Fourier features (matched to QP-1a)"],
    "K0": ["classical", "Logistic regression on the 8 inputs (floor)"],
    "K3": ["classical", "The classical score alone: today's analyst order"],
    "N1": ["control", "QP-1 on shuffled labels (negative control)"],
  };
  const ORDER = ["QP-1", "K4", "QK-1", "K1", "QP-1a", "K2", "K3", "K0", "N1"];
  const PAIRS = [["QP-1", "K4", "the gate"], ["QK-1", "K1", ""], ["QP-1a", "K2", ""]];
  const kindCls = (k) => (k === "quantum" ? "q" : k === "classical" ? "c" : "n");

  function verdictTone(v) {
    if (!v) return "plain";
    if (v === "PASS") return "ok";
    if (v.startsWith("VOID")) return "bad";
    return "warn";
  }

  // Group the per-seed rows, and recover the gate's checks from the rows when an older summary lacks them.
  function digest(S) {
    const rows = (S && S.rows) || [];
    const by = {};
    rows.forEach((r) => { (by[r.model] = by[r.model] || []).push(r); });
    const models = Object.keys(by).sort((a, b) => (ORDER.indexOf(a) + 99 * (ORDER.indexOf(a) < 0)) - (ORDER.indexOf(b) + 99 * (ORDER.indexOf(b) < 0)));
    const agg = models.map((m) => {
      const rs = by[m];
      return { model: m, kind: (INFO[m] || ["classical"])[0], what: (INFO[m] || [, ""])[1], seeds: rs.map((r) => r.seed),
        inputs: rs[0].inputs, backend: rs[0].backend, control: rs[0].control,
        recall: mean(rs.map((r) => r.recall_at_cap)), precision: mean(rs.map((r) => r.precision_at_cap)),
        pr_auc: mean(rs.map((r) => r.pr_auc)), ms: mean(rs.map((r) => r.ms_per_item)),
        cmp: rs.filter((r) => r.diff_vs_control != null).map((r) => ({ seed: r.seed, diff: r.diff_vs_control, ci: r.diff_ci95 })) };
    });
    const get = (m) => agg.find((a) => a.model === m);
    const minGain = S && S.min_gain != null ? S.min_gain : 0.02;
    const tol = S && S.sanity_tolerance != null ? S.sanity_tolerance : 0.03;
    const beats = (m) => { const a = get(m); return !!(a && a.cmp.length && a.cmp.every((c) => c.diff >= minGain && c.ci && c.ci[0] > 0)); };
    const n1 = (S && S.n1_recall) || (by["N1"] || []).map((r) => r.recall_at_cap);
    const rand = S ? S.random_order_recall : null;
    const commit = (S && S.protocol_commit) || (rows[0] && rows[0].protocol_commit) || null;
    const gate = Object.assign({ qp1_beats: beats("QP-1"), qk1_beats: beats("QK-1"),
      sanity_ok: n1.length > 0 && rand != null && n1.every((x) => Math.abs(x - rand) <= tol),
      protocol_committed: !!commit && commit !== "UNCOMMITTED" }, (S && S.gate) || {});
    return { agg, get, gate, n1, rand, commit, minGain, tol };
  }

  // Forest plot: each matched comparison, one line per seed, the difference in recall at capacity with its 95% interval.
  function forest(D) {
    const items = [];
    PAIRS.forEach(([a, b, note]) => { const g = D.get(a); if (g && g.cmp.length) items.push({ a, b, note, cmp: g.cmp }); });
    if (!items.length) return `<p class="note">No matched comparisons in this run.</p>`;
    let lo = -D.minGain, hi = D.minGain * 2;
    items.forEach((it) => it.cmp.forEach((c) => { const l = c.ci ? c.ci[0] : c.diff, h = c.ci ? c.ci[1] : c.diff; lo = Math.min(lo, l, c.diff); hi = Math.max(hi, h, c.diff); }));
    const pad = (hi - lo) * 0.08 || 0.01; lo -= pad; hi += pad;
    const W = 560, L = 118, R = 16, rowH = 18, gap = 16;
    let y = 22, body = "";
    const X = (v) => L + ((v - lo) / (hi - lo)) * (W - L - R);
    items.forEach((it) => {
      const top = y;
      body += `<text class="lbl" x="0" y="${y + 9}">${esc(it.a)} vs ${esc(it.b)}</text>${it.note ? `<text x="0" y="${y + 23}">${esc(it.note)}</text>` : ""}`;
      it.cmp.forEach((c) => {
        const cy = y + 6, l = c.ci ? c.ci[0] : c.diff, h = c.ci ? c.ci[1] : c.diff;
        const good = c.diff >= D.minGain && c.ci && c.ci[0] > 0;
        const col = good ? "var(--green)" : it.a.startsWith("Q") ? "var(--violet)" : "var(--blue)";
        body += `<g><title>${esc(it.a)} vs ${esc(it.b)}, seed ${c.seed}: ${pts(c.diff)} [${c.ci ? pts(c.ci[0]) + ", " + pts(c.ci[1]) : "-"}]</title>
          <line x1="${X(l).toFixed(1)}" x2="${X(h).toFixed(1)}" y1="${cy}" y2="${cy}" stroke="${col}" stroke-width="2" stroke-linecap="round" opacity=".55"/>
          <circle cx="${X(c.diff).toFixed(1)}" cy="${cy}" r="4.5" fill="#fff" stroke="${col}" stroke-width="2"/>
          <text x="${W}" y="${cy + 3.5}" text-anchor="end">s${esc(c.seed)}</text></g>`;
        y += rowH;
      });
      y += gap;
      body = `<rect x="${L - 6}" y="${top - 4}" width="${W - L - R + 12}" height="${y - top - gap + 4}" rx="8" fill="var(--soft)"/>` + body;
    });
    const H = y + 8;
    const zero = X(0), thr = X(D.minGain);
    const axis = `<line x1="${zero}" x2="${zero}" y1="14" y2="${H - 22}" stroke="var(--ink-2)" stroke-width="1"/>
      <line x1="${thr}" x2="${thr}" y1="14" y2="${H - 22}" stroke="var(--green)" stroke-width="1" stroke-dasharray="3 3"/>
      <text x="${zero}" y="10" text-anchor="middle">0</text><text x="${thr}" y="10" text-anchor="middle" style="fill:var(--green)">+${(100 * D.minGain).toFixed(0)} pts needed</text>
      <text x="${L}" y="${H - 6}">${pts(lo, 1)}</text><text x="${W - R}" y="${H - 6}" text-anchor="end">${pts(hi, 1)}</text>`;
    return `<div class="forest"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Matched comparisons: difference in recall at capacity with 95% intervals">${axis}${body}</svg></div>
      <div class="legend"><span><i style="background:var(--green)"></i>meets the rule (≥ +${(100 * D.minGain).toFixed(0)} pts, interval above 0)</span><span><i style="background:var(--violet)"></i>quantum model short of the rule</span></div>`;
  }

  // Leaderboard: per-model means over the seeds. Bars are scaled to the best value shown in each column.
  function leaderboard(D) {
    const maxR = Math.max(1e-9, ...D.agg.map((a) => a.recall || 0), D.rand || 0);
    const maxP = Math.max(1e-9, ...D.agg.map((a) => a.pr_auc || 0));
    const bar = (v, max, cls, txt) => `<div class="hbar"><div class="t"><div class="f ${cls}" style="width:${v == null ? 0 : Math.max(2, (100 * v) / max).toFixed(1)}%"></div></div><em>${txt}</em></div>`;
    const rows = D.agg.map((a) => {
      const k = kindCls(a.kind);
      return `<tr title="${esc(a.what)}"><td><b>${esc(a.model)}</b><span class="kind ${k}">${esc(a.kind)}</span><div class="cell-note">${esc(a.what)}</div></td>
        <td class="bar-cell">${bar(a.recall, maxR, k, pct(a.recall))}</td>
        <td class="num">${pct(a.precision)}</td>
        <td class="bar-cell">${bar(a.pr_auc, maxP, k, num(a.pr_auc))}</td>
        <td class="num">${a.ms == null ? "-" : num(a.ms, 2)}</td></tr>`;
    }).join("");
    const randRow = D.rand == null ? "" : `<tr><td><b>Random order</b><span class="kind n">baseline</span><div class="cell-note">What capacity catches with no ranking at all</div></td>
      <td class="bar-cell">${bar(D.rand, maxR, "n", pct(D.rand))}</td><td class="num">-</td><td class="bar-cell"></td><td class="num">-</td></tr>`;
    return `<div class="scroll lb"><table><thead><tr><th>Model</th><th>Recall at capacity</th><th class="num">Precision at capacity</th><th>PR-AUC</th><th class="num">ms / item</th></tr></thead>
      <tbody>${rows}${randRow}</tbody></table></div><p class="note">Means over seeds ${esc([...new Set(D.agg.flatMap((a) => a.seeds))].join(", "))}, on the test split. Hover a row for what the model is.</p>`;
  }

  function checks(D, S) {
    const w = S && S.worker;
    const items = S ? [
      [D.gate.sanity_ok, "Negative control is sane", "N1 (shuffled labels) lands near a random ordering",
        D.n1.length ? `${D.n1.map((x) => pct(x)).join(" · ")} vs ${pct(D.rand)}` : "-"],
      [D.gate.protocol_committed, "Protocol committed before testing", "quantum/PROTOCOL.md, checked by the code", D.commit ? esc(D.commit) : "-"],
      [D.gate.qp1_beats, "QP-1 beats K4 on every seed", `≥ +${(100 * D.minGain).toFixed(0)} pts of recall at capacity, interval above 0`, D.get("QP-1") ? pts(mean(D.get("QP-1").cmp.map((c) => c.diff))) + " avg" : "-"],
      [D.gate.qk1_beats, "QK-1 beats K1 on every seed", "Same rule, reported alongside", D.get("QK-1") ? pts(mean(D.get("QK-1").cmp.map((c) => c.diff))) + " avg" : "-"],
      [S.rerank_allowed ? true : "off", "Live re-ranking allowed", "Only on a PASS with worker p99 ≤ 1,000 ms", w && w.p99_ms != null ? `p99 ${num(w.p99_ms, 1)} ms` : "shadow only"],
    ] : [
      [null, "Negative control is sane", "N1 within 3 pts of a random ordering", "not run"],
      [null, "Protocol committed before testing", "quantum/PROTOCOL.md", "not run"],
      [null, "QP-1 beats K4 on every seed", "≥ +2 pts of recall at capacity, interval above 0", "not run"],
      [null, "Live re-ranking allowed", "Only on a PASS", "shadow only"],
    ];
    return `<div class="checks">${items.map(([ok, t, s, v]) => `<div class="check ${ok == null ? "wait" : ok === "off" ? "off" : ok ? "ok" : "no"}"><span class="ico" aria-hidden="true">${ok == null ? "…" : ok === "off" ? "–" : ok ? "✓" : "✕"}</span><div><b>${esc(t)}</b><span>${esc(s)}</span></div><em>${v}</em></div>`).join("")}</div>`;
  }

  // Live shadow re-scoring, from the hold queue the console already reads.
  function liveStats(holds) {
    holds = holds || [];
    const scored = holds.filter((h) => h.challenger && h.challenger.status === "ok" && h.challenger.score != null);
    const both = scored.filter((h) => h.score != null);
    const avg = mean(scored.map((h) => h.challenger.score));
    // how often the two scores put a held payment on the same side of their own medians
    let agree = null;
    if (both.length >= 4) {
      const med = (a) => { const s = [...a].sort((x, y) => x - y); return s[Math.floor(s.length / 2)]; };
      const mc = med(both.map((h) => h.score)), mq = med(both.map((h) => h.challenger.score));
      agree = both.filter((h) => (h.score >= mc) === (h.challenger.score >= mq)).length / both.length;
    }
    return { n: holds.length, scored: scored.length, avg, agree, model: (scored[0] && scored[0].challenger.model) || "qp1-v1",
      backend: (scored[0] && scored[0].challenger.backend) || "numpy-statevector" };
  }

  function renderResults(el, S, holds) {
    if (!el) return;
    const D = digest(S);
    const L = liveStats(holds);
    const tone = verdictTone(S && S.verdict);
    const live = holds === undefined ? "" : `<section class="card s-12" aria-label="Live shadow re-scoring">
        <div class="card-h"><h2>Live shadow re-scoring <small>the worker re-scores held payments; it never changes a decision</small></h2></div>
        <div class="kv"><div><span>held re-scored</span><b>${L.scored} / ${L.n}</b></div><div><span>avg challenger score</span><b>${num(L.avg)}</b></div>
        <div><span>agrees with classical</span><b title="Share of held payments both scores put on the same side of their medians">${L.agree == null ? "-" : pct(L.agree, 0)}</b></div>
        <div><span>model · backend</span><b style="font-size:15px">${esc(L.model)} · ${esc(L.backend)}</b></div></div></section>`;
    if (!S || !S.verdict) {
      el.innerHTML = `<div class="bento"><section class="card s-12"><div class="q-hero"><div>
          <div class="q-tags"><span class="tag plain">gate not run</span><span class="tag violet">8 qubits · 2 layers · exact statevector</span></div>
          <div class="q-verdict">Awaiting the quantum gate</div>
          <p class="q-statement">No test-split result is published yet. The verdict is computed by the pre-registered gate, never written by hand. Run these on the VM, in order, once the classical export exists:</p>
          <ol class="steps" style="margin-top:12px">
            <li><span>Unit checks, numpy against Qiskit <code>python -m quantum.research.evaluate checks</code></span></li>
            <li><span>Tune on validation only <code>python -m quantum.research.evaluate tune</code></span></li>
            <li><span>Freeze and commit the protocol <code>python -m quantum.research.evaluate freeze</code></span></li>
            <li><span>Test under the committed protocol <code>python -m quantum.research.evaluate test</code></span></li>
            <li><span>Worker latency for the re-rank rule <code>python -m quantum.research.evaluate bench</code></span></li>
          </ol></div>${checks(D, null)}</div></section>${live}</div>`;
      return;
    }
    const qp = D.get("QP-1"), k4 = D.get("K4"), k3 = D.get("K3");
    // A run where every model, the classical score and the shuffled-label control all land on the same recall cannot
    // separate anything: say so beside the verdict instead of letting "no advantage" read as a measured loss.
    const recs = D.agg.map((a) => a.recall).filter((v) => v != null);
    const nTest = ((S.rows || [])[0] || {}).n_test;
    const flat = recs.length > 2 && Math.max(...recs) - Math.min(...recs) < 1e-9;
    const power = flat ? `<p class="q-statement" style="margin-top:12px;padding:10px 12px;border-radius:var(--r-md);background:var(--amber-soft);color:#7a4d05">
        <b>Underpowered run.</b> All ${recs.length} models, including the classical score alone and the shuffled-label control, scored the same ${pct(recs[0])} recall at capacity on ${esc(nTest == null ? "?" : nTest)} test rows, so no comparison could move. This verdict says the data could not separate the models, not that the quantum model lost. Re-export more candidates (<span class="mono">python -m scoring export … --max-rows</span>) and rerun the gate for a real verdict.</p>` : "";
    const bestQ = D.agg.filter((a) => a.kind === "quantum").sort((a, b) => (b.pr_auc || 0) - (a.pr_auc || 0))[0];
    const bestC = D.agg.filter((a) => a.kind === "classical").sort((a, b) => (b.pr_auc || 0) - (a.pr_auc || 0))[0];
    const when = S.generated_ts ? new Date(S.generated_ts * 1000).toLocaleString() : "";
    el.innerHTML = `<div class="bento">
      <section class="card s-12" aria-label="Quantum gate verdict"><div class="q-hero"><div>
        <div class="q-tags"><span class="tag ${tone}">${esc(S.verdict)}</span>${flat ? `<span class="tag warn">underpowered: ${esc(nTest)} test rows</span>` : ""}<span class="tag ${S.rerank_allowed ? "ok" : "plain"}">${S.rerank_allowed ? "re-rank allowed" : "shadow only"}</span>${D.commit ? `<span class="tag plain">protocol ${esc(D.commit)}</span>` : ""}${when ? `<span class="tag plain">${esc(when)}</span>` : ""}</div>
        <div class="q-verdict">${tone === "ok" ? "The quantum feature map helped on this data" : tone === "bad" ? "This run is void" : "No advantage over matched classical controls"}</div>
        <p class="q-statement">${esc(S.statement || "")}</p>${power}
        <div class="kv" style="margin-top:16px">
          <div><span>QP-1 recall at capacity</span><b>${pct(qp && qp.recall)}</b></div>
          <div><span>K4 (its matched control)</span><b>${pct(k4 && k4.recall)}</b></div>
          <div><span>Classical score alone (K3)</span><b>${pct(k3 && k3.recall)}</b></div>
          <div><span>Best PR-AUC, quantum vs classical</span><b>${num(bestQ && bestQ.pr_auc)} <small class="muted">vs</small> ${num(bestC && bestC.pr_auc)}</b></div>
        </div></div>${checks(D, S)}</div></section>
      <section class="card s-5" aria-label="Matched comparisons"><div class="card-h"><h2>Matched comparisons <small>difference in recall at capacity, 95% paired bootstrap</small></h2></div>${forest(D)}</section>
      <section class="card s-7" aria-label="Models and controls"><div class="card-h"><h2>Models and controls <small>test split</small></h2></div>${leaderboard(D)}</section>
      ${live}</div>
      <p class="note">Full gate text: <span class="mono">${esc(S.gate_file || "quantum/GATE.md")}</span>. Even a PASS is not a quantum computational advantage: 8 qubits are simulated exactly on a classical machine.</p>`;
  }

  // Slides for the console's insight card.
  function slides(S, holds, C) {
    const L = liveStats(holds);
    const out = [];
    if (C && C.verdict === "PASS") {
      const qp = digest(C).get("QP-1");
      const avg = qp && qp.cmp.length ? mean(qp.cmp.map((c) => c.diff)) : null;
      out.push({ big: pts(avg, 1).replace(" pts", ""), title: "Points of recall QP-1 gains over its classical twin on quantum-structured data",
        text: "Positive control under the same pre-registered gate: PASS on every seed. Synthetic data, not a bank result; 8 qubits simulated exactly." });
    }
    if (S && S.verdict) {
      const D = digest(S), qp = D.get("QP-1");
      const avgDiff = qp && qp.cmp.length ? mean(qp.cmp.map((c) => c.diff)) : null;
      out.push({ big: pct(qp && qp.recall, 0), title: `QP-1 recall at analyst capacity, ${pts(avgDiff, 1)} vs K4`,
        text: `Gate verdict: ${S.verdict}. ${S.rerank_allowed ? "Re-ranking is allowed." : "Shadow only: it never changes a decision."}` });
      out.push({ big: D.gate.sanity_ok ? "OK" : "VOID", title: "Negative control lands on a random ordering",
        text: `N1 ${D.n1.map((x) => pct(x)).join(" · ") || "-"} against random ${pct(D.rand)}. The protocol was ${D.gate.protocol_committed ? "committed before testing" : "NOT committed"}.` });
    } else {
      out.push({ big: "8q", title: "Quantum gate not run yet", text: "8 qubits, 2 layers, simulated exactly. The verdict will come from the pre-registered gate, never by hand." });
    }
    out.push({ big: L.n ? `${L.scored}/${L.n}` : "0", title: "Held payments re-scored by the challenger right now",
      text: L.avg == null ? "No challenger scores in the hold queue yet." : `Average challenger score ${num(L.avg)}${L.agree == null ? "" : `; agrees with the classical side ${pct(L.agree, 0)} of the time`}.` });
    return out;
  }

  // The positive control (control_summary.json): the SAME pre-registered gate on synthetic data where a quantum edge
  // exists by construction (labels follow the feature map's 2-qubit correlations). It shows what a gain looks like
  // when there is one, so the bank-data verdict above means something. Never presented as a bank or fraud result.
  function renderControl(el, C) {
    if (!el) return;
    if (!C || C.empty || !C.verdict) { el.innerHTML = ""; return; }
    const D = digest(C);
    const tone = verdictTone(C.verdict);
    const qp = D.get("QP-1"), k4 = D.get("K4"), qk = D.get("QK-1"), k1 = D.get("K1");
    const gains = qp ? qp.cmp.map((c) => c.diff) : [];
    const avg = mean(gains);
    const head = tone === "ok" ? `QP-1 gains ${pts(avg, 1)} over its matched classical twin`
      : tone === "bad" ? "This run is void" : "No gain over the matched classical twin on this benchmark";
    el.innerHTML = `<div class="bento">
      <section class="card s-12" aria-label="Where the quantum challenger gains"><div class="q-hero"><div>
        <div class="q-tags"><span class="tag ${tone}">${esc(C.verdict)}</span><span class="tag violet">quantum-structured benchmark</span><span class="tag plain">synthetic · not bank data</span>${D.commit ? `<span class="tag plain">protocol ${esc(D.commit)}</span>` : ""}</div>
        <div class="q-verdict">${esc(head)}</div>
        <p class="q-statement">${esc(C.statement || "")}</p>
        <div class="kv" style="margin-top:16px">
          <div><span>QP-1 recall at capacity</span><b>${pct(qp && qp.recall)}</b></div>
          <div><span>K4, its matched control</span><b>${pct(k4 && k4.recall)}</b></div>
          <div><span>Gain per seed</span><b style="font-size:15px">${gains.length ? gains.map((g) => pts(g, 1)).join(" · ") : "-"}</b></div>
          <div><span>QK-1 vs K1 (reported too)</span><b style="font-size:15px">${pts(qk && mean(qk.cmp.map((c) => c.diff)), 1)} <small class="muted">${pct(qk && qk.recall)} vs ${pct(k1 && k1.recall)}</small></b></div>
        </div></div>${checks(D, C)}</div></section>
      <section class="card s-5" aria-label="Positive control comparisons"><div class="card-h"><h2>Matched comparisons <small>difference in recall at capacity, 95% paired bootstrap</small></h2></div>${forest(D)}</section>
      <section class="card s-7" aria-label="Positive control models"><div class="card-h"><h2>Models and controls <small>test split</small></h2></div>${leaderboard(D)}</section>
      </div>
      <p class="note">${esc(C.label || "")}</p>`;
  }

  // Quantum gain against pattern complexity (advantage_sweep.json): from a basic pattern (alpha 0, a plain linear rule a
  // classical model already captures) to the entangled pattern (alpha 1). Every point is measured with the gate's method.
  function renderSweep(el, W) {
    if (!el) return;
    if (!W || W.empty || !W.points || !W.points.length) { el.innerHTML = ""; return; }
    const P = W.points.slice().sort((a, b) => a.alpha - b.alpha);
    const minGain = W.min_gain != null ? W.min_gain : 0.02;
    const m = (p, k) => mean(p.recall_at_cap[k] || []);
    const series = [["QP-1", "var(--violet)", ""], ["K4", "var(--blue)", ""], ["QK-1", "var(--violet)", "5 4"], ["K1", "var(--blue)", "5 4"]]
      .filter(([k]) => P.every((p) => p.recall_at_cap[k]));
    const W_ = 620, H = 260, L = 46, R = 52, T = 22, B = 46;
    const X = (a) => L + a * (W_ - L - R), Y = (v) => T + (1 - v) * (H - T - B);
    let grid = "";
    [0, 0.25, 0.5, 0.75, 1].forEach((v) => { grid += `<line x1="${L}" x2="${W_ - R}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--line)"/><text x="${L - 8}" y="${Y(v) + 3.5}" text-anchor="end">${(100 * v).toFixed(0)}%</text>`; });
    P.forEach((p) => { grid += `<text x="${X(p.alpha)}" y="${H - B + 16}" text-anchor="middle">${p.alpha.toFixed(2)}</text>`; });
    grid += `<text x="${L}" y="${H - 6}">basic pattern</text><text x="${W_ - R}" y="${H - 6}" text-anchor="end">entangled pattern</text>`;
    // shade the alphas where QP-1 meets the gate's rule against K4 on every seed
    let shade = "";
    const win = P.filter((p) => (p.comparisons["QP-1"] || {}).beats);
    if (win.length) {
      const half = P.length > 1 ? (P[1].alpha - P[0].alpha) / 2 : 0.1;
      const x0 = X(Math.max(0, win[0].alpha - half)), x1 = X(Math.min(1, win[win.length - 1].alpha + half));
      shade = `<rect x="${x0.toFixed(1)}" y="${T}" width="${(x1 - x0).toFixed(1)}" height="${(H - T - B).toFixed(1)}" fill="var(--green-soft)" opacity=".65"/>
        <text x="${((x0 + x1) / 2).toFixed(1)}" y="${T - 7}" text-anchor="middle" style="fill:#137a48;font-weight:600">QP-1 beats K4 on every seed</text>`;
    }
    const rand = P.map((p) => [X(p.alpha), Y(p.random_order_recall)]);
    let lines = `<polyline fill="none" stroke="var(--faint)" stroke-width="1.5" stroke-dasharray="2 3" points="${rand.map((q) => q.join(",")).join(" ")}"/>`;
    // the kernel pair first and faint, the gate's pair on top and labelled at the line ends
    series.slice().sort((a, b) => (b[2] ? 1 : 0) - (a[2] ? 1 : 0)).forEach(([k, col, dash]) => {
      const pts_ = P.map((p) => [X(p.alpha), Y(m(p, k))]);
      lines += `<polyline fill="none" stroke="${col}" stroke-width="${dash ? 1.4 : 2.6}" ${dash ? `stroke-dasharray="${dash}" opacity=".45"` : ""} points="${pts_.map((q) => q.map((v) => v.toFixed(1)).join(",")).join(" ")}"/>`;
      lines += pts_.map((q, i) => `<circle cx="${q[0].toFixed(1)}" cy="${q[1].toFixed(1)}" r="${dash ? 2.5 : 4}" fill="#fff" stroke="${col}" stroke-width="2" ${dash ? 'opacity=".45"' : ""}><title>${esc(k)} at alpha ${P[i].alpha.toFixed(2)}: ${pct(m(P[i], k))} recall at capacity</title></circle>`).join("");
      if (!dash) { const e = pts_[pts_.length - 1]; lines += `<text class="lbl" x="${(e[0] + 8).toFixed(1)}" y="${(e[1] + 4).toFixed(1)}" style="fill:${col};font-weight:600">${esc(k)}</text>`; }
    });
    const chart = `<div class="forest"><svg viewBox="0 0 ${W_} ${H}" role="img" aria-label="Recall at capacity against pattern complexity for the quantum models and their matched classical controls">${shade}${grid}${lines}</svg></div>
      <div class="legend">${series.map(([k, col, dash]) => `<span><i style="background:${col};${dash ? "opacity:.55" : ""}"></i>${esc(k)}${dash ? " (dashed)" : ""}</span>`).join("")}<span><i style="background:var(--faint)"></i>random order</span></div>`;
    const rows = P.map((p) => {
      const c = (p.comparisons["QP-1"] || {}).by_seed || [];
      const g = mean(c.map((x) => x.diff));
      const lo = c.length ? Math.min(...c.map((x) => x.ci95[0])) : null, hi = c.length ? Math.max(...c.map((x) => x.ci95[1])) : null;
      const ok = (p.comparisons["QP-1"] || {}).beats;
      const ck = (p.comparisons["QK-1"] || {}).by_seed || [];
      const sg = (v) => (v == null ? "-" : (v >= 0 ? "+" : "−") + Math.abs(100 * v).toFixed(1));
      return `<tr><td class="num">${p.alpha.toFixed(2)}</td><td class="num">${pct(m(p, "QP-1"))}</td><td class="num">${pct(m(p, "K4"))}</td>
        <td class="num"><b style="color:${ok ? "var(--green)" : "var(--ink-2)"}">${sg(g)}</b></td><td class="num">${lo == null ? "-" : sg(lo) + " … " + sg(hi)}</td>
        <td><span class="tag ${ok ? "ok" : "plain"}">${ok ? "every seed" : "no"}</span></td>
        <td class="num">${ck.length ? sg(mean(ck.map((x) => x.diff))) : "-"}</td></tr>`;
    }).join("");
    el.innerHTML = `<div class="bento">
      <section class="card s-6" aria-label="Quantum gain against pattern complexity"><div class="card-h"><h2>Quantum gain against pattern complexity <small>recall at analyst capacity, mean of ${esc((W.seeds || []).length)} seeds</small></h2></div>${chart}</section>
      <section class="card s-6" aria-label="Gain table"><div class="card-h"><h2>QP-1 minus K4, in points <small>rule: ≥ +${(100 * minGain).toFixed(0)} on every seed, interval above 0</small></h2></div>
        <div class="scroll" style="max-height:none"><table style="white-space:nowrap"><thead><tr><th class="num">alpha</th><th class="num">QP-1</th><th class="num">K4</th><th class="num">gain</th><th class="num">95% range</th><th>rule met</th><th class="num">QK-1 − K1</th></tr></thead><tbody>${rows}</tbody></table></div>
        <p class="note">alpha 0 is a basic pattern, alpha 1 the entangled one. Gain is the mean of the seeds; the range spans every seed's 95% interval.</p></section>
      </div><p class="note">${esc(W.label || "")} Command: <span class="mono">${esc(W.command || "")}</span></p>`;
  }

  window.RFQuantum = { renderResults, renderControl, renderSweep, slides, digest };
})();
