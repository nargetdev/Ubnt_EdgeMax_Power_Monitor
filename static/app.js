"use strict";

const API = {
  meta: "/api/meta",
  labels: "/api/labels",
  status: "/api/status",
  refresh: "/api/status/refresh",
  poe: (port) => `/api/poe/${port}`,
};

const state = {
  meta: null,
  labels: {}, // saved on server (source of truth)
  draftLabels: {}, // edits in flight
  status: { poe_status: [], timestamp: null, server: null, source: null },
};

const els = {
  grid: document.getElementById("port-grid"),
  template: document.getElementById("port-card-template"),
  switchMeta: document.getElementById("switch-meta"),
  metaSwitch: document.getElementById("meta-switch"),
  metaLastStatus: document.getElementById("meta-last-status"),
  metaStatusSource: document.getElementById("meta-status-source"),
  metaPollInterval: document.getElementById("meta-poll-interval"),
  metaLastCommand: document.getElementById("meta-last-command"),
  mqttPill: document.getElementById("mqtt-pill"),
  refreshBtn: document.getElementById("refresh-btn"),
  saveBtn: document.getElementById("save-labels-btn"),
  toast: document.getElementById("toast"),
};

async function fetchJSON(url, options = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail;
    try {
      detail = (await res.json()).error;
    } catch {
      detail = res.statusText;
    }
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return res.json();
}

function toast(message, kind = "ok", ms = 2200) {
  els.toast.textContent = message;
  els.toast.className = `toast visible ${kind}`;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => {
    els.toast.className = "toast";
  }, ms);
}

function formatTime(ts) {
  if (!ts) return "never";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function classifyState(row) {
  if (!row) return { label: "no data", cls: "" };
  const det = (row.detection || "").toLowerCase();
  if (!det) return { label: "no data", cls: "" };
  if (det.includes("disabled") || det.includes("shutdown")) {
    return { label: "off", cls: "state-off" };
  }
  if (det === "good" || det.startsWith("delivering")) {
    return { label: "on", cls: "state-good" };
  }
  if (det.includes("open")) {
    return { label: "open", cls: "state-off" };
  }
  if (det.includes("fault") || det.includes("denied") || det.includes("overload")) {
    return { label: "fault", cls: "state-bad" };
  }
  return { label: det, cls: "" };
}

function statusByPort() {
  const map = new Map();
  for (const row of state.status.poe_status || []) {
    const m = /^0\/(\d+)$/.exec((row.intf || "").trim());
    if (!m) continue;
    map.set(parseInt(m[1], 10), row);
  }
  return map;
}

function effectiveLabel(port) {
  const k = String(port);
  return Object.prototype.hasOwnProperty.call(state.draftLabels, k)
    ? state.draftLabels[k]
    : state.labels[k] || "";
}

function hasDirtyLabels() {
  return Object.keys(state.draftLabels).length > 0;
}

function refreshSaveButton() {
  els.saveBtn.disabled = !hasDirtyLabels();
}

function renderPorts() {
  els.grid.innerHTML = "";
  const portCount = state.meta?.port_count ?? 24;
  const byPort = statusByPort();
  const frag = document.createDocumentFragment();
  for (let p = 1; p <= portCount; p++) {
    const node = els.template.content.firstElementChild.cloneNode(true);
    node.dataset.port = String(p);
    node.querySelector(".port-number").textContent = `Port ${p}`;
    const input = node.querySelector('[data-role="label-input"]');
    input.value = effectiveLabel(p);
    input.addEventListener("input", () => {
      const original = state.labels[String(p)] || "";
      const v = input.value;
      if (v === original) {
        delete state.draftLabels[String(p)];
      } else {
        state.draftLabels[String(p)] = v;
      }
      node.classList.toggle(
        "dirty",
        Object.prototype.hasOwnProperty.call(state.draftLabels, String(p))
      );
      refreshSaveButton();
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        saveLabels();
      }
    });

    node.querySelector('[data-role="on-btn"]').addEventListener("click", () =>
      togglePoe(p, "on", node)
    );
    node.querySelector('[data-role="off-btn"]').addEventListener("click", () =>
      togglePoe(p, "off", node)
    );

    applyPortReadings(node, byPort.get(p));
    frag.appendChild(node);
  }
  els.grid.appendChild(frag);
}

function applyPortReadings(card, row) {
  const badge = card.querySelector('[data-role="state-badge"]');
  const { label, cls } = classifyState(row);
  badge.textContent = label;
  badge.className = `port-state-badge ${cls}`.trim();

  const setText = (sel, val) => {
    card.querySelector(`[data-role="${sel}"]`).textContent = val ?? "—";
  };
  if (!row) {
    setText("detection", "—");
    setText("class", "—");
    setText("consumed", "—");
    setText("vi", "—");
    return;
  }
  setText("detection", row.detection || "—");
  setText("class", row.class || "—");
  const w = row.consumed ? `${row.consumed} W` : "—";
  setText("consumed", w);
  const v = row.voltage ? `${row.voltage} V` : "—";
  const i = row.current ? `${row.current} mA` : "—";
  setText("vi", `${v} / ${i}`);

  const onBtn = card.querySelector('[data-role="on-btn"]');
  const offBtn = card.querySelector('[data-role="off-btn"]');
  onBtn.classList.toggle("active", cls === "state-good");
  offBtn.classList.toggle("active", cls === "state-off" && label !== "open");
}

function updatePortReadingsInPlace() {
  const byPort = statusByPort();
  for (const card of els.grid.querySelectorAll(".port-card")) {
    const p = parseInt(card.dataset.port, 10);
    applyPortReadings(card, byPort.get(p));
  }
}

async function loadMeta() {
  state.meta = await fetchJSON(API.meta);
  els.switchMeta.textContent =
    `Switch ${state.meta.switch_host ?? "(unknown)"} ` +
    `• MQTT ${state.meta.mqtt_broker}:${state.meta.mqtt_port}` +
    ` • ${state.meta.port_count} ports`;
  els.metaSwitch.textContent = state.meta.switch_host ?? "—";
  const interval = state.meta.poll_interval_seconds;
  els.metaPollInterval.textContent =
    interval > 0 ? `${interval}s` : "disabled";
  refreshMqttPill();
}

function refreshMqttPill() {
  const ok = !!state.meta?.mqtt_connected;
  els.mqttPill.classList.toggle("connected", ok);
  els.mqttPill.classList.toggle("disconnected", !ok);
  els.mqttPill.querySelector(".label").textContent = ok
    ? "MQTT connected"
    : "MQTT down";
}

async function loadLabels() {
  state.labels = await fetchJSON(API.labels);
  state.draftLabels = {};
  refreshSaveButton();
}

async function loadStatus({ silent = false } = {}) {
  try {
    const fresh = await fetchJSON(API.status);
    state.status = fresh;
    updatePortReadingsInPlace();
    els.metaLastStatus.textContent = formatTime(fresh.timestamp);
    els.metaStatusSource.textContent = fresh.source ?? "—";
    return fresh;
  } catch (err) {
    if (!silent) toast(`Could not load status: ${err.message}`, "error");
    return null;
  }
}

async function loadMetaThenMqtt() {
  try {
    state.meta = await fetchJSON(API.meta);
    refreshMqttPill();
  } catch (err) {
    /* swallow; we'll retry */
  }
}

async function saveLabels() {
  if (!hasDirtyLabels()) return;
  els.saveBtn.disabled = true;
  const merged = { ...state.labels };
  for (const [k, v] of Object.entries(state.draftLabels)) {
    if (!v || !v.trim()) {
      delete merged[k];
    } else {
      merged[k] = v.trim();
    }
  }
  try {
    state.labels = await fetchJSON(API.labels, {
      method: "PUT",
      body: JSON.stringify(merged),
    });
    state.draftLabels = {};
    for (const card of els.grid.querySelectorAll(".port-card.dirty")) {
      card.classList.remove("dirty");
    }
    refreshSaveButton();
    toast("Labels saved");
  } catch (err) {
    toast(`Save failed: ${err.message}`, "error");
    refreshSaveButton();
  }
}

async function togglePoe(port, action, cardEl) {
  cardEl.classList.add("busy");
  try {
    const info = await fetchJSON(API.poe(port), {
      method: "POST",
      body: JSON.stringify({ action }),
    });
    els.metaLastCommand.textContent =
      `port ${info.port} → ${info.action} ` +
      `@ ${formatTime(info.timestamp)}`;
    toast(`Port ${port} → ${action.toUpperCase()}`);
  } catch (err) {
    toast(`Toggle failed: ${err.message}`, "error");
  } finally {
    cardEl.classList.remove("busy");
  }
}

async function manualRefresh() {
  els.refreshBtn.disabled = true;
  const originalLabel = els.refreshBtn.textContent;
  els.refreshBtn.textContent = "Refreshing…";
  try {
    await fetchJSON(API.refresh, { method: "POST" });
    await loadStatus({ silent: true });
    toast("Status refreshed");
  } catch (err) {
    toast(`Refresh failed: ${err.message}`, "error");
  } finally {
    els.refreshBtn.textContent = originalLabel;
    els.refreshBtn.disabled = false;
  }
}

function startPollers() {
  setInterval(() => loadStatus({ silent: true }), 4000);
  setInterval(() => loadMetaThenMqtt(), 8000);
}

async function init() {
  els.refreshBtn.addEventListener("click", manualRefresh);
  els.saveBtn.addEventListener("click", saveLabels);
  try {
    await loadMeta();
    await loadLabels();
    await loadStatus({ silent: true });
    renderPorts();
    startPollers();
  } catch (err) {
    toast(`Initial load failed: ${err.message}`, "error", 6000);
    console.error(err);
  }
}

init();
