"use strict";

// Latency model from docs/audits/2026-08-15-generation-latency.md. The audit is
// explicit that a two-point fit cannot validate itself, so the estimate is
// shown as one and labelled with what it came from.
const MS_PER_FRAME = 4461.1;
const FIXED_OVERHEAD_MS = 414196;

const POLL_INTERVAL_MS = 10000;
const PREFLIGHT_DEBOUNCE_MS = 350;

const state = {
  constraints: null,
  modelSet: null,
  form: "freeform",
  preflight: null,
  runs: new Map(),
};

const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  return response.json();
}

function text(node, value) {
  node.textContent = value == null ? "" : String(value);
}

function show(node, visible) {
  node.classList.toggle("hidden", !visible);
}

function duration(ms) {
  if (ms == null) return "—";
  const total = Math.round(ms / 1000);
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return minutes ? `${minutes}m ${String(seconds).padStart(2, "0")}s` : `${seconds}s`;
}

// ---------------------------------------------------------------- constraints

function currentRange() {
  return state.constraints.model_sets.find(
    (entry) => entry.model_set_id === state.modelSet
  );
}

function applyModelSet() {
  const range = currentRange();
  $("width").value = range.default_width;
  $("height").value = range.default_height;
  $("frames").value = range.default_frames;
  $("width").max = range.max_long_edge;
  $("height").max = range.max_long_edge;
  $("width").step = range.canvas_multiple;
  $("height").step = range.canvas_multiple;
  $("frames").max = range.max_frames;
  text(
    $("model-set-hint"),
    `Canvas up to ${range.max_long_edge}x${range.max_short_edge} in multiples of ` +
      `${range.canvas_multiple}. Frames on the ${range.grid_stride}k+${range.grid_offset} ` +
      `grid, trained ${range.min_trained_frames}-${range.max_frames} at ${range.fps} fps.`
  );
  text(
    $("frames-hint"),
    `${range.grid_stride}k+${range.grid_offset}: ` +
      nearbyFrames(range).join(", ") + ", …"
  );
}

function nearbyFrames(range) {
  const values = [];
  for (let k = 0; values.length < 5; k += 1) {
    const frames = range.grid_stride * k + range.grid_offset;
    if (frames > range.max_frames) break;
    values.push(frames);
  }
  return values;
}

function renderVocabularies() {
  const host = $("vocabularies-list");
  host.replaceChildren();
  for (const vocabulary of state.constraints.vocabularies) {
    const block = document.createElement("div");
    block.className = "vocabulary";
    const heading = document.createElement("h4");
    heading.textContent = vocabulary.omitted_default
      ? `${vocabulary.name} — omitted default: ${vocabulary.omitted_default}`
      : vocabulary.name;
    const terms = document.createElement("div");
    terms.className = "terms";
    for (const value of vocabulary.values) {
      const term = document.createElement("span");
      term.className = "term";
      term.textContent = value;
      terms.append(term);
    }
    block.append(heading, terms);
    host.append(block);
  }
}

// -------------------------------------------------------------------- compose

function requestDocument() {
  const document_ = {
    schema_version: "1",
    mode: "text-to-video",
    workflow_id: "minimax-h3-t2v",
    model_set_id: state.modelSet,
    seed: Number($("seed").value),
    width: Number($("width").value),
    height: Number($("height").value),
    frames: Number($("frames").value),
    fps: 24,
  };
  if (state.form === "freeform") {
    document_.prompt = $("prompt").value;
    return { document: document_ };
  }
  const raw = $("structured").value.trim();
  if (!raw) return { document: document_, localError: "Supply a structured prompt." };
  try {
    document_.structured_prompt = JSON.parse(raw);
  } catch (error) {
    return { document: document_, localError: `Structured prompt is not JSON: ${error.message}` };
  }
  return { document: document_ };
}

function renderRejection(payload, message) {
  show($("defects"), true);
  show($("wire"), false);
  show($("estimate"), false);
  text($("defects-message"), message || payload.message || "Refused.");
  const list = $("defects-list");
  list.replaceChildren();
  const rows = payload && payload.defects ? payload.defects : [];
  for (const defect of rows) {
    const item = document.createElement("li");
    const field = document.createElement("span");
    field.textContent = defect.field;
    const detail = document.createElement("span");
    detail.className = "rule";
    detail.textContent = ` ${defect.message || defect.rule}`;
    item.append(field, detail);
    list.append(item);
  }
  if (payload && payload.error_context && !rows.length) {
    const item = document.createElement("li");
    item.textContent = JSON.stringify(payload.error_context);
    list.append(item);
  }
}

function renderAcceptance(payload) {
  show($("defects"), false);
  show($("wire"), true);
  text($("wire-text"), payload.wire_text);

  const departure = payload.trained_envelope;
  show($("envelope"), !departure.inside);
  if (!departure.inside) {
    const reasons = [];
    if (departure.short_edge_below_trained) reasons.push("the canvas is smaller than any trained sample");
    if (departure.frames_below_trained) reasons.push("the clip is shorter than the trained floor");
    text(
      $("envelope"),
      `Below the trained envelope: ${reasons.join(" and ")}. This is permitted and ` +
        `cheaper, and is recorded as a departure on the committed result.`
    );
  }

  const frames = payload.request.frames;
  const estimate = FIXED_OVERHEAD_MS + frames * MS_PER_FRAME;
  show($("estimate"), true);
  $("estimate").className = "note neutral";
  text(
    $("estimate"),
    `About ${duration(estimate)} on a cold worker for ${frames} frames — inferred from ` +
      `the two runs in the latency audit, not measured per phase. Roughly ` +
      `${duration(FIXED_OVERHEAD_MS)} of that is hydration and fixed overhead.`
  );
}

let preflightTimer = null;

function schedulePreflight() {
  window.clearTimeout(preflightTimer);
  preflightTimer = window.setTimeout(runPreflight, PREFLIGHT_DEBOUNCE_MS);
}

async function runPreflight() {
  const { document: body, localError } = requestDocument();
  if (localError) {
    state.preflight = null;
    $("generate").disabled = true;
    text($("preflight-state"), "");
    renderRejection({ defects: [] }, localError);
    return;
  }
  text($("preflight-state"), "Checking…");
  const payload = await api("/api/preflight", {
    method: "POST",
    body: JSON.stringify(body),
  });
  state.preflight = payload.ok ? payload : null;
  $("generate").disabled = !payload.ok;
  text($("preflight-state"), payload.ok ? "Valid — same checks the worker runs." : "");
  if (payload.ok) renderAcceptance(payload);
  else renderRejection(payload);
}

// ----------------------------------------------------------------------- runs

function toneFor(status, resultState) {
  if (resultState === "completed") return "done";
  if (["FAILED", "CANCELLED", "TIMED_OUT"].includes(status) || resultState === "failed") return "failed";
  if (["IN_QUEUE", "IN_PROGRESS"].includes(status)) return "running";
  return "idle";
}

function createRunCard(record) {
  const node = $("run-template").content.firstElementChild.cloneNode(true);
  node.dataset.runId = record.run_id;
  text(node.querySelector("[data-run-id]"), record.run_id);
  text(node.querySelector("[data-job-id]"), record.job_id);
  text(node.querySelector("[data-model-set]"), record.model_set_id);
  text(node.querySelector("[data-status]"), "submitted");
  node.querySelector("[data-cancel]").addEventListener("click", async () => {
    await api(`/api/runs/${record.run_id}/cancel`, { method: "POST" });
    pollRun(record.run_id);
  });
  $("runs").prepend(node);
  show($("runs-empty"), false);
  return node;
}

function renderRun(node, payload) {
  const status = payload.status || "unknown";
  const resultState = payload.result_state;
  const badge = node.querySelector("[data-status]");
  text(badge, resultState === "completed" ? "completed" : status.toLowerCase().replace("_", " "));
  badge.dataset.tone = toneFor(status, resultState);
  text(node.querySelector("[data-delay]"), duration(payload.delay_time_ms));
  text(node.querySelector("[data-execution]"), duration(payload.execution_time_ms));
  text(node.querySelector("[data-result-state]"), resultState || "—");
  show(node.querySelector("[data-cancel]"), !payload.terminal);

  const note = node.querySelector("[data-note]");
  const messages = [];
  let bad = false;
  const handler = payload.handler;
  if (handler && handler.ok === false && handler.error_code) {
    bad = true;
    messages.push(`The worker refused this request: ${handler.error_code}.`);
    for (const defect of handler.defects || []) {
      messages.push(`${defect.field} — ${defect.rule}`);
    }
  }
  if (payload.provider_error) {
    bad = true;
    messages.push(payload.provider_error);
  }
  if (payload.identity_matches === false) {
    bad = true;
    messages.push(
      "The worker's job id differs from the one this console derived. The console's " +
        "principal is not the endpoint's, so it is looking for the result under the wrong key."
    );
  }
  note.className = bad ? "run-note bad" : "run-note";
  text(note, messages.join(" "));
  show(note, messages.length > 0);
}

async function attachDelivery(node, jobId) {
  const delivery = node.querySelector("[data-delivery]");
  if (!delivery.classList.contains("hidden")) return;
  const payload = await api(`/api/jobs/${jobId}/link`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  if (!payload.ok) return;
  const video = node.querySelector("[data-video]");
  video.src = payload.url;
  const download = node.querySelector("[data-download]");
  download.href = payload.url;
  download.setAttribute("download", `${jobId}.mp4`);
  text(
    node.querySelector("[data-expiry]"),
    new Date(payload.expires_at).toLocaleTimeString()
  );
  show(delivery, true);
}

async function pollRun(runId) {
  const entry = state.runs.get(runId);
  if (!entry) return;
  const payload = await api(`/api/runs/${runId}`);
  if (!payload.ok) return;
  renderRun(entry.node, payload);
  if (payload.result_state === "completed") {
    await attachDelivery(entry.node, entry.record.job_id);
  }
  const finished = payload.terminal && payload.result_state !== "in-progress";
  if (finished && entry.timer) {
    window.clearInterval(entry.timer);
    entry.timer = null;
  }
}

function trackRun(record) {
  const node = createRunCard(record);
  const entry = { record, node, timer: null };
  state.runs.set(record.run_id, entry);
  entry.timer = window.setInterval(() => pollRun(record.run_id), POLL_INTERVAL_MS);
  pollRun(record.run_id);
}

async function generate() {
  const { document: body, localError } = requestDocument();
  if (localError) return;
  $("generate").disabled = true;
  text($("preflight-state"), "Submitting…");
  const payload = await api("/api/generate", {
    method: "POST",
    body: JSON.stringify(body),
  });
  if (!payload.ok) {
    renderRejection(
      payload,
      payload.endpoint_paused
        ? "The endpoint has not resumed yet and is refusing work. Try again shortly."
        : payload.message
    );
    text($("preflight-state"), "");
    $("generate").disabled = false;
    return;
  }
  text($("preflight-state"), "Submitted.");
  $("generate").disabled = false;
  trackRun(payload.run);
}

// ----------------------------------------------------------------------- boot

function bindForm() {
  for (const id of ["prompt", "structured", "seed", "width", "height", "frames"]) {
    $(id).addEventListener("input", schedulePreflight);
  }
  $("prompt").addEventListener("input", () => {
    text($("prompt-count"), $("prompt").value.length);
  });
  $("model-set").addEventListener("change", () => {
    state.modelSet = $("model-set").value;
    applyModelSet();
    schedulePreflight();
  });
  $("reseed").addEventListener("click", () => {
    $("seed").value = String(Math.floor(Math.random() * 2 ** 32));
    schedulePreflight();
  });
  $("tab-freeform").addEventListener("click", () => selectForm("freeform"));
  $("tab-structured").addEventListener("click", () => selectForm("structured"));
  $("generate").addEventListener("click", generate);
}

function selectForm(form) {
  state.form = form;
  $("tab-freeform").setAttribute("aria-selected", String(form === "freeform"));
  $("tab-structured").setAttribute("aria-selected", String(form === "structured"));
  show($("pane-freeform"), form === "freeform");
  show($("pane-structured"), form === "structured");
  schedulePreflight();
}

function renderScope(scope) {
  const host = $("scope");
  host.replaceChildren();
  const rows = [
    ["endpoint", scope.endpoint_id],
    ["principal", scope.principal_id],
    ["bucket", scope.bucket_name],
    ["region", scope.region],
    ["link ttl", `${scope.signed_link_ttl_seconds}s`],
  ];
  for (const [name, value] of rows) {
    const group = document.createElement("div");
    const term = document.createElement("dt");
    term.textContent = name;
    const detail = document.createElement("dd");
    detail.textContent = value;
    group.append(term, detail);
    host.append(group);
  }
}

async function boot() {
  const [constraints, scope] = await Promise.all([
    api("/api/constraints"),
    api("/api/scope"),
  ]);
  state.constraints = constraints;
  renderScope(scope);
  renderVocabularies();
  text($("grammar-revision"), constraints.grammar_revision);
  for (const node of document.querySelectorAll(".prompt-limit")) {
    node.textContent = String(constraints.max_prompt_chars);
  }
  const select = $("model-set");
  for (const entry of constraints.model_sets) {
    const option = document.createElement("option");
    option.value = entry.model_set_id;
    option.textContent = entry.model_set_id;
    select.append(option);
  }
  $("structured").value = JSON.stringify(constraints.structured_example, null, 2);
  state.modelSet = constraints.model_sets[0].model_set_id;
  select.value = state.modelSet;
  applyModelSet();
  bindForm();
  schedulePreflight();
}

boot();
