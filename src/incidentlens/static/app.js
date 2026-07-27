/* IncidentLens replay UI.
 *
 * Fetches a scenario's architecture and analysis, lays the service graph
 * out as columns by dependency depth, and replays the incident timeline
 * frame by frame. Every frame maps to a timeline entry produced by the
 * analysis engine, so nothing shown here is invented client-side.
 */

"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const NODE_H = 44;
const COL_GAP = 220;
const ROW_GAP = 74;
const PAD = 28;
const BASE_FRAME_MS = 1800;

const state = {
  analysis: null,
  architecture: null,
  frames: [],
  frame: -1,
  playing: false,
  timer: null,
  nodeEls: new Map(), // service name -> <g>
  edgeEls: new Map(), // "a|b" (sorted) -> <path>
};

const $ = (id) => document.getElementById(id);

function esc(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

function shortTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "--:--";
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

async function fetchJson(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body && body.detail) detail = body.detail;
    } catch {
      /* keep statusText */
    }
    throw new Error(detail);
  }
  return res.json();
}

/* ---------- scenario list ---------- */

async function loadScenarios() {
  const select = $("scenario-select");
  try {
    const scenarios = await fetchJson("/api/v1/scenarios");
    select.innerHTML = "";
    for (const s of scenarios) {
      const opt = document.createElement("option");
      opt.value = s.name;
      opt.textContent = s.title;
      select.appendChild(opt);
    }
  } catch (err) {
    select.innerHTML = "";
    showError(`Could not load scenarios: ${err.message}`);
  }
}

function showError(message) {
  const empty = $("empty");
  empty.classList.remove("hidden");
  empty.innerHTML = `<p class="eyebrow">Error</p><p>${esc(message)}</p>`;
  $("stage-panel").classList.add("hidden");
  $("detail-panel").classList.add("hidden");
}

/* ---------- graph layout ---------- */

// Width must clear BOTH lines: the 12px name and the 10px owner beneath it.
// Measuring only the name let long owner strings ("it-devops · external") spill
// past the box edge.
function nodeWidth(service) {
  const name = typeof service === "string" ? service : service.name;
  const owner =
    typeof service === "string"
      ? ""
      : service.user_facing
        ? `${service.owner || ""} · user-facing`
        : service.owner || "";
  const nameW = name.length * 7.3;
  const ownerW = owner.length * 6.15;
  return Math.max(132, Math.ceil(Math.max(nameW, ownerW)) + 26);
}

function layoutGraph(architecture) {
  const services = architecture.services;
  const byName = new Map(services.map((s) => [s.name, s]));
  const depth = new Map();

  // BFS from user-facing services along depends_on.
  const queue = [];
  for (const s of services) {
    if (s.user_facing) {
      depth.set(s.name, 0);
      queue.push(s.name);
    }
  }
  while (queue.length) {
    const name = queue.shift();
    const d = depth.get(name);
    for (const dep of byName.get(name).depends_on) {
      if (!byName.has(dep)) continue;
      const next = d + 1;
      if (!depth.has(dep) || next > depth.get(dep)) {
        depth.set(dep, next);
        queue.push(dep);
      }
    }
  }

  // Services no user-facing path reaches (queue consumers, batch workers):
  // place one column past the deepest of their own dependencies.
  let changed = true;
  while (changed) {
    changed = false;
    for (const s of services) {
      if (depth.has(s.name)) continue;
      const deps = s.depends_on.filter((d) => depth.has(d));
      if (deps.length) {
        depth.set(s.name, Math.max(...deps.map((d) => depth.get(d))) + 1);
        changed = true;
      }
    }
  }
  const maxKnown = depth.size ? Math.max(...depth.values()) : 0;
  for (const s of services) {
    if (!depth.has(s.name)) depth.set(s.name, maxKnown + 1);
  }

  // Column buckets, stable order.
  const cols = new Map();
  for (const s of services) {
    const d = depth.get(s.name);
    if (!cols.has(d)) cols.set(d, []);
    cols.get(d).push(s);
  }
  const depths = [...cols.keys()].sort((a, b) => a - b);
  const maxRows = Math.max(...depths.map((d) => cols.get(d).length));
  const height = PAD * 2 + maxRows * NODE_H + (maxRows - 1) * (ROW_GAP - NODE_H);

  const pos = new Map();
  depths.forEach((d, colIdx) => {
    const bucket = cols.get(d);
    const colHeight = bucket.length * NODE_H + (bucket.length - 1) * (ROW_GAP - NODE_H);
    const startY = (height - colHeight) / 2;
    bucket.forEach((s, rowIdx) => {
      pos.set(s.name, {
        x: PAD + colIdx * COL_GAP,
        y: startY + rowIdx * ROW_GAP,
        w: nodeWidth(s),
      });
    });
  });

  const width = PAD * 2 + (depths.length - 1) * COL_GAP + 160;
  return { pos, width, height };
}

function edgeKey(a, b) {
  return [a, b].sort().join("|");
}

function renderGraph(architecture) {
  const svg = $("graph");
  svg.innerHTML = "";
  state.nodeEls.clear();
  state.edgeEls.clear();

  const { pos, width, height } = layoutGraph(architecture);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

  const defs = document.createElementNS(SVG_NS, "defs");
  defs.innerHTML =
    '<marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" ' +
    'markerHeight="7" orient="auto-start-reverse">' +
    '<path d="M0,0 L8,4 L0,8 z" fill="currentColor"/></marker>';
  svg.appendChild(defs);

  const edgeLayer = document.createElementNS(SVG_NS, "g");
  const nodeLayer = document.createElementNS(SVG_NS, "g");
  svg.appendChild(edgeLayer);
  svg.appendChild(nodeLayer);

  for (const service of architecture.services) {
    const from = pos.get(service.name);
    for (const dep of service.depends_on) {
      const to = pos.get(dep);
      if (!to) continue;
      const x1 = from.x + from.w;
      const y1 = from.y + NODE_H / 2;
      const x2 = to.x;
      const y2 = to.y + NODE_H / 2;
      const bend = Math.max(40, (x2 - x1) / 2);
      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute(
        "d",
        `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`,
      );
      path.setAttribute("class", "edge");
      path.setAttribute("marker-end", "url(#arrow)");
      edgeLayer.appendChild(path);
      state.edgeEls.set(edgeKey(service.name, dep), path);
    }
  }

  for (const service of architecture.services) {
    const p = pos.get(service.name);
    const g = document.createElementNS(SVG_NS, "g");
    g.setAttribute("class", "node healthy");
    g.setAttribute("transform", `translate(${p.x}, ${p.y})`);

    const rect = document.createElementNS(SVG_NS, "rect");
    rect.setAttribute("width", p.w);
    rect.setAttribute("height", NODE_H);
    rect.setAttribute("rx", 8);
    g.appendChild(rect);

    const label = document.createElementNS(SVG_NS, "text");
    label.setAttribute("x", 12);
    label.setAttribute("y", 19);
    label.textContent = service.name;
    g.appendChild(label);

    const owner = document.createElementNS(SVG_NS, "text");
    owner.setAttribute("class", "owner");
    owner.setAttribute("x", 12);
    owner.setAttribute("y", 34);
    owner.textContent = service.user_facing ? `${service.owner} · user-facing` : service.owner;
    g.appendChild(owner);

    nodeLayer.appendChild(g);
    state.nodeEls.set(service.name, g);
  }
}

/* ---------- replay ---------- */

function buildFrames(analysis) {
  return analysis.timeline.map((entry) => ({
    time: entry.timestamp,
    title: entry.title,
    description: entry.description,
    severity: entry.severity,
    services: entry.services,
    evidence: entry.evidence_ids,
  }));
}

function applyFrame(index) {
  state.frame = index;

  // Fold node states over frames 0..index. Later evidence wins, so a
  // recovery event returns its services to the recovery state.
  const rank = { healthy: 0, info: 0, warning: 1, critical: 2 };
  const nodeState = new Map();
  const failedAt = new Map(); // service -> first frame index it degraded
  for (let i = 0; i <= index; i += 1) {
    const frame = state.frames[i];
    for (const svc of frame.services) {
      if (frame.severity === "recovery") {
        nodeState.set(svc, "recovery");
      } else if (frame.severity === "warning" || frame.severity === "critical") {
        const current = nodeState.get(svc);
        if (current === "recovery") continue;
        if (!current || rank[frame.severity] >= rank[current]) {
          nodeState.set(svc, frame.severity);
        }
        if (!failedAt.has(svc)) failedAt.set(svc, i);
      }
    }
  }

  for (const [name, el] of state.nodeEls) {
    el.setAttribute("class", `node ${nodeState.get(name) || "healthy"}`);
  }

  // A propagation edge lights up once both ends have degraded.
  for (const path of state.edgeEls.values()) path.classList.remove("active");
  for (const step of state.analysis.propagation) {
    if (failedAt.has(step.from_service) && failedAt.has(step.to_service)) {
      const path = state.edgeEls.get(edgeKey(step.from_service, step.to_service));
      if (path) path.classList.add("active");
    }
  }

  const frame = index >= 0 ? state.frames[index] : null;
  $("replay-clock").textContent = frame ? `${shortTime(frame.time)} UTC` : "--:-- UTC";
  $("caption-title").textContent = frame ? frame.title : "Press play to replay the incident.";
  $("caption-body").textContent = frame ? frame.description : "";
  $("caption-evidence").textContent =
    frame && frame.evidence.length ? `evidence: ${frame.evidence.join(", ")}` : "";
  $("frame-count").textContent = `${index + 1} / ${state.frames.length}`;
  $("scrub").value = String(index);
}

function stopPlayback() {
  state.playing = false;
  if (state.timer) {
    clearInterval(state.timer);
    state.timer = null;
  }
  $("play").textContent = "▶";
  $("play").setAttribute("aria-label", "Play");
}

function startPlayback() {
  if (!state.frames.length) return;
  if (state.frame >= state.frames.length - 1) applyFrame(0);
  state.playing = true;
  $("play").textContent = "⏸";
  $("play").setAttribute("aria-label", "Pause");
  const speed = Number($("speed").value) || 1;
  state.timer = setInterval(() => {
    if (state.frame >= state.frames.length - 1) {
      stopPlayback();
      return;
    }
    applyFrame(state.frame + 1);
  }, BASE_FRAME_MS / speed);
}

function togglePlayback() {
  if (state.playing) stopPlayback();
  else startPlayback();
}

function step(delta) {
  if (!state.frames.length) return;
  stopPlayback();
  const next = Math.min(state.frames.length - 1, Math.max(0, state.frame + delta));
  applyFrame(next);
}

/* ---------- detail tabs ---------- */

function confBar(confidence) {
  const pct = Math.round(confidence * 100);
  return `<div class="confbar" role="img" aria-label="confidence ${pct} percent">` +
    `<span style="width:${pct}%"></span></div><p class="meta">confidence ${pct}%</p>`;
}

function renderTimeline(analysis) {
  $("timeline").innerHTML = analysis.timeline
    .map(
      (t) => `<article class="severity ${esc(t.severity)}">
        <p class="meta">${esc(shortTime(t.timestamp))} UTC · ${esc(t.services.join(", "))}</p>
        <h3>${esc(t.title)}</h3>
        <p>${esc(t.description)}</p>
        <small>evidence: ${esc(t.evidence_ids.join(", ") || "none")}</small>
      </article>`,
    )
    .join("");
}

function renderRootCause(analysis) {
  $("rootcause").innerHTML = analysis.hypotheses
    .map(
      (h) => `<article>
        <span class="badge ${esc(h.status)}">${esc(h.status)}</span>
        <h3>${esc(h.title)}</h3>
        <p>${esc(h.explanation)}</p>
        ${confBar(h.confidence)}
        <small>evidence: ${esc(h.evidence_ids.join(", ") || "none")}</small>
      </article>`,
    )
    .join("");
}

function renderPropagation(analysis) {
  if (!analysis.propagation.length) {
    $("propagation").innerHTML = "<p>No propagation beyond the origin service was observed.</p>";
    return;
  }
  $("propagation").innerHTML = analysis.propagation
    .map(
      (p) => `<article>
        <h3>${esc(p.from_service)} <span class="arrow">→</span> ${esc(p.to_service)}</h3>
        <p>${esc(p.mechanism)}</p>
        <small>evidence: ${esc(p.evidence_ids.join(", "))}</small>
      </article>`,
    )
    .join("");
}

function renderActions(analysis) {
  const actions = [...analysis.recommended_actions].sort((a, b) => a.priority - b.priority);
  $("actions").innerHTML = actions
    .map(
      (a) => `<article>
        <p class="meta">priority ${esc(a.priority)}</p>
        <h3>${esc(a.action)}</h3>
        <p>${esc(a.reason)}</p>
        <small>risk: ${esc(a.risk)}</small>
      </article>`,
    )
    .join("");
}

function renderBriefings(analysis) {
  const missing = analysis.missing_evidence.length
    ? `<article><h3>Missing evidence</h3><ul>${analysis.missing_evidence
        .map((m) => `<li>${esc(m)}</li>`)
        .join("")}</ul></article>`
    : "";
  $("briefings").innerHTML = `
    <article><h3>Engineer briefing</h3><p>${esc(analysis.engineer_briefing)}</p></article>
    <article><h3>Executive summary</h3><p>${esc(analysis.executive_summary)}</p></article>
    ${missing}`;
}

function renderEvidence(analysis) {
  $("evidence").innerHTML = `<div class="grid">${analysis.evidence
    .map((e) => {
      const attrs = Object.entries(e.attributes || {})
        .map(([k, v]) => `${esc(k)}=${esc(v)}`)
        .join(" · ");
      return `<article>
        <p class="meta">${esc(e.id)} · ${esc(e.source_type)} · ${esc(e.source)}</p>
        <p>${esc(e.detail)}</p>
        <small>${esc(shortTime(e.timestamp))} UTC${attrs ? " · " + attrs : ""}</small>
      </article>`;
    })
    .join("")}</div>`;
}

/* ---------- orchestration ---------- */

// Paint an analysis into the stage. Shared by the bundled scenarios and by the
// sandbox, so telemetry a visitor supplies renders through exactly the same path.
function showAnalysis(architecture, analysis, note) {
  state.analysis = analysis;
  state.architecture = architecture;
  state.frames = buildFrames(analysis);

  $("incident-id").textContent = note
    ? `${analysis.incident_id} · ${note}`
    : analysis.incident_id;
  $("incident-title").textContent = analysis.title;
  $("incident-impact").textContent = analysis.customer_impact;
  $("scrub").max = String(Math.max(0, state.frames.length - 1));

  renderGraph(architecture);
  renderTimeline(analysis);
  renderRootCause(analysis);
  renderPropagation(analysis);
  renderActions(analysis);
  renderBriefings(analysis);
  renderEvidence(analysis);

  $("empty").classList.add("hidden");
  $("stage-panel").classList.remove("hidden");
  $("detail-panel").classList.remove("hidden");

  applyFrame(0);
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!reduceMotion) startPlayback();
}

async function reconstruct() {
  const scenario = $("scenario-select").value;
  const button = $("run");
  button.disabled = true;
  button.textContent = "Reconstructing…";
  stopPlayback();

  try {
    const [detail, analysis] = await Promise.all([
      fetchJson(`/api/v1/scenarios/${encodeURIComponent(scenario)}`),
      fetchJson("/api/v1/incidents/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scenario }),
      }),
    ]);

    showAnalysis(detail.architecture, analysis);
  } catch (err) {
    showError(err.message);
  } finally {
    button.disabled = false;
    button.textContent = "Reconstruct incident";
  }
}

function wireEvents() {
  $("run").addEventListener("click", reconstruct);
  $("play").addEventListener("click", togglePlayback);
  $("step-back").addEventListener("click", () => step(-1));
  $("step-fwd").addEventListener("click", () => step(1));
  $("scrub").addEventListener("input", (e) => {
    stopPlayback();
    applyFrame(Number(e.target.value));
  });
  $("speed").addEventListener("change", () => {
    if (state.playing) {
      stopPlayback();
      startPlayback();
    }
  });
  $("graph-wrap").addEventListener("keydown", (e) => {
    if (e.code === "Space") {
      e.preventDefault();
      togglePlayback();
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      step(1);
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      step(-1);
    }
  });

  for (const tab of document.querySelectorAll(".tabs button")) {
    tab.addEventListener("click", () => {
      for (const b of document.querySelectorAll(".tabs button")) b.classList.remove("active");
      for (const panel of document.querySelectorAll(".tab")) panel.classList.remove("active");
      tab.classList.add("active");
      $(tab.dataset.tab).classList.add("active");
    });
  }
}

document.addEventListener("DOMContentLoaded", () => {
  wireEvents();
  wireSandbox();
  wireIntro();
  // Reconstruct the first scenario immediately. A visitor should land on the
  // product working, not on an empty state asking them to press a button.
  loadScenarios().then(() => {
    if ($("scenario-select").options.length) reconstruct();
  });
});

/* ---------------------------------------------------------------- sandbox ---
 * Two inputs a visitor controls: paste real log lines, or describe an incident
 * and let a model write the telemetry. Both end in showAnalysis(), so what they
 * see is the same reconstruction the bundled scenarios produce.
 */

const SAMPLE_LOGS = [
  "2026-07-27 09:12:01.114 INFO  [main] payments.api.routes : POST /v1/charge 200 in 84 ms",
  "2026-07-27 09:12:31.207 INFO  [main] payments.config.loader : config change applied: rotated db credential reference to db/payments/primary (deploy 2026.7.27-b19)",
  "2026-07-27 09:12:44.882 WARN  [pool-2] payments.db.pool : connection acquire took 2871 ms (budget 250 ms), 47/50 slots busy",
  "2026-07-27 09:13:02.410 ERROR [pool-2] payments.db.credentials : asyncpg.InvalidPasswordError: password authentication failed for user \"svc_payments\"",
  "2026-07-27 09:13:02.418 ERROR [pool-2] payments.db.credentials : credential resolution failed after 3 attempts (retryable=false); opening circuit",
  "2026-07-27 09:13:05.001 ERROR [main] payments.api.routes : POST /v1/charge 502 in 3011 ms — 1841 sessions affected in the last 60 s",
].join("\n");

function sbStatus(message, kind) {
  const el = $("sb-status");
  if (!message) {
    el.classList.add("hidden");
    return;
  }
  el.textContent = message;
  el.className = "sandbox-status" + (kind ? " " + kind : "");
}

async function sbRun(body, button, busyLabel) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = busyLabel;
  sbStatus("");
  stopPlayback();
  try {
    const data = await fetchJson("/api/v1/sandbox/reconstruct", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const note = data.synthesised ? "synthesised telemetry" : "your log lines";
    showAnalysis(data.architecture, data.analysis, note);
    sbStatus(data.disclosure, "ok");
    $("stage-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    sbStatus(err.message, "bad");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function wireSandbox() {
  const paste = $("pane-paste");
  const prompt = $("pane-prompt");
  const tabPaste = $("tab-paste");
  const tabPrompt = $("tab-prompt");
  if (!paste || !prompt) return;

  const select = (which) => {
    const onPaste = which === "paste";
    paste.classList.toggle("hidden", !onPaste);
    prompt.classList.toggle("hidden", onPaste);
    tabPaste.classList.toggle("active", onPaste);
    tabPrompt.classList.toggle("active", !onPaste);
    sbStatus("");
  };
  tabPaste.addEventListener("click", () => select("paste"));
  tabPrompt.addEventListener("click", () => select("prompt"));

  $("sb-sample").addEventListener("click", () => {
    $("sb-logs").value = SAMPLE_LOGS;
    $("sb-service").value = "payment-api";
  });

  $("sb-run-logs").addEventListener("click", () => {
    const logs = $("sb-logs").value.trim();
    if (!logs) {
      sbStatus("Paste a few log lines first, or load the sample.", "bad");
      return;
    }
    sbRun(
      { logs, service: $("sb-service").value.trim() || "my-service" },
      $("sb-run-logs"),
      "Reconstructing…"
    );
  });

  $("sb-run-prompt").addEventListener("click", () => {
    const text = $("sb-prompt").value.trim();
    if (!text) {
      sbStatus("Describe the failure you want to see reconstructed.", "bad");
      return;
    }
    sbRun({ prompt: text }, $("sb-run-prompt"), "Generating telemetry…");
  });

  // Reflect what this deployment actually supports.
  fetchJson("/api/v1/sandbox")
    .then((s) => {
      if (!s.generation_enabled) {
        $("sb-run-prompt").disabled = true;
        $("sb-quota").textContent =
          "Scenario generation is not configured on this deployment — pasting logs still works.";
        return;
      }
      const q = s.quota || {};
      $("sb-quota").textContent =
        `${q.daily_remaining} of ${q.daily_limit} generated scenarios left today.`;
    })
    .catch(() => {});
}

/* --------------------------------------------------------------- intro ---
 * The hero's entry points: jump to the sandbox with the right tab already
 * selected, and show real poster frames from the B2 catalog so the object
 * storage is visible on the landing page rather than only linked.
 */

function wireIntro() {
  document.querySelectorAll(".cta[data-seg]").forEach((link) => {
    link.addEventListener("click", () => {
      const want = link.dataset.seg === "prompt" ? "tab-prompt" : "tab-paste";
      const button = $(want);
      if (button && !button.classList.contains("active")) button.click();
    });
  });

  const rail = $("b2strip-posters");
  if (!rail) return;
  fetchJson("/api/v1/incidents")
    .then((data) => {
      const picks = (data.incidents || [])
        .filter((i) => i.prefix.startsWith("Hary_"))
        .slice(0, 3);
      if (!picks.length) {
        $("b2strip").classList.add("hidden");
        return;
      }
      rail.textContent = "";
      picks.forEach((inc) => {
        const a = document.createElement("a");
        a.href = "/gallery";
        const img = document.createElement("img");
        img.loading = "lazy";
        img.src = inc.poster_url;
        img.alt = inc.title || inc.prefix;
        const label = document.createElement("span");
        label.className = "poster-label";
        label.textContent = inc.failing_symbol || inc.failing_module || inc.prefix;
        a.append(img, label);
        rail.appendChild(a);
      });
    })
    .catch(() => {
      // Object storage unreachable: hide the strip rather than show broken frames.
      const strip = $("b2strip");
      if (strip) strip.classList.add("hidden");
    });
}
