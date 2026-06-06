const $ = (id) => document.getElementById(id);
async function api(path, opts) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
}

const els = {
  pod: $("statusPod"), statusText: $("statusText"), lapNum: $("lapNum"),
  elapsedTime: $("elapsedTime"),
  accelKey: $("accelKey"), steerKey: $("steerKey"), startDelay: $("startDelay"),
  pollInterval: $("pollInterval"), maxLaps: $("maxLaps"), throttleMod: $("throttleMod"), launchEase: $("launchEase"),
  saveBtn: $("saveBtn"), runBtn: $("runBtn"), pauseBtn: $("pauseBtn"), log: $("log"), clearLog: $("clearLog"),
  modeSeg: $("modeSeg"), presetSeg: $("presetSeg"), modeHint: $("modeHint"), accelLabel: $("accelLabel"),
  steerField: $("steerField"), conn: $("conn"),
  gamePod: $("gamePod"), gameText: $("gameText"),
  dispPod: $("dispPod"), dispText: $("dispText"),
  tabs: $("tabs"), modeTabs: $("modeTabs"),
  advToggle: $("advToggle"), advBody: $("advBody"),
  telEnabled: $("telEnabled"), telHost: $("telHost"), telPort: $("telPort"),
  telSaveBtn: $("telSaveBtn"), telState: $("telState"), telSource: $("telSource"),
  telRace: $("telRace"), telSpeed: $("telSpeed"), telHint: $("telHint"),
  rpEnabled: $("rpEnabled"), telDiscord: $("telDiscord"),
  rpSaveBtn: $("rpSaveBtn"), rpState: $("rpState"), rpCar: $("rpCar"),
  rpStateLine: $("rpStateLine"), rpHint: $("rpHint"),
  viewTelemetry: $("view-telemetry"), viewDiscord: $("view-discord"),
};

let mode = "keyboard";
let preset = "slowed";
let running = false;
let runBusy = false;
let manualPaused = false;
let gamepadAvailable = true;
let elapsedS = 0;
let elapsedTimer = null;

function updatePauseBtn() {
  els.pauseBtn.disabled = !running;
  els.pauseBtn.textContent = manualPaused ? "▶ RESUME" : "⏸ PAUSE";
  els.pauseBtn.classList.toggle("resume", manualPaused);
}

function fmtElapsed(s) {
  const h = String(Math.floor(s / 3600)).padStart(2, "0");
  const m = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const sec = String(s % 60).padStart(2, "0");
  return `${h}:${m}:${sec}`;  // always HH:MM:SS
}

// ---- logging ----------------------------------------------------------
function logLine(msg, cls = "l-sys") {
  const ts = new Date().toLocaleTimeString("en-GB");
  const div = document.createElement("div");
  div.className = "line";
  div.innerHTML = `<span class="ts">${ts}</span><span class="${cls}">${escapeHtml(msg)}</span>`;
  els.log.appendChild(div);
  els.log.scrollTop = els.log.scrollHeight;
  while (els.log.childElementCount > 400) els.log.removeChild(els.log.firstChild);
}
function escapeHtml(s) { return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }

// ---- status -----------------------------------------------------------
const LABELS = { stopped: "STOPPED", starting: "ARMING", racing: "RACING", results: "RESTART", restart_confirm: "CONFIRM", prerace_menu: "MENU", paused: "PAUSED" };
function setStatus(state, laps, elapsed_s) {
  els.pod.dataset.state = state;
  els.statusText.textContent = LABELS[state] || state.toUpperCase();
  if (typeof laps === "number") els.lapNum.textContent = String(laps).padStart(3, "0");
  running = !(state === "stopped");
  els.runBtn.textContent = running ? "■ STOP" : "▶ START";
  els.runBtn.classList.toggle("stop", running);
  setInputsDisabled(running);
  if (!running) manualPaused = false;  // stop clears manual-pause intent
  updatePauseBtn();
  // elapsed timer
  if (typeof elapsed_s === "number") elapsedS = elapsed_s;
  if (running) {
    els.elapsedTime.textContent = fmtElapsed(elapsedS);
    if (!elapsedTimer) {
      elapsedTimer = setInterval(() => {
        elapsedS++;
        els.elapsedTime.textContent = fmtElapsed(elapsedS);
      }, 1000);
    }
  } else {
    clearInterval(elapsedTimer); elapsedTimer = null;
    elapsedS = 0;
    els.elapsedTime.textContent = "00:00:00";
  }
}
function setInputsDisabled(d) {
  [els.accelKey, els.steerKey, els.startDelay, els.pollInterval, els.maxLaps, els.throttleMod, els.launchEase, els.saveBtn]
    .forEach((e) => (e.disabled = d));
  els.modeSeg.querySelectorAll(".seg-btn").forEach((b) => {
    // bouton GAMEPAD grise si ViGEmBus absent (en plus du verrou pendant le run)
    b.disabled = d || (b.dataset.mode === "gamepad" && !gamepadAvailable);
  });
  els.presetSeg.querySelectorAll(".seg-btn").forEach((b) => (b.disabled = d));
}

// ---- mode toggle ------------------------------------------------------
function applyMode(m) {
  if (m === "gamepad" && !gamepadAvailable) m = "keyboard";  // securite: pas de ViGEmBus
  mode = m;
  els.modeSeg.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.mode === m));
  if (m === "gamepad") {
    els.accelLabel.textContent = "ACCELERATE (RIGHT TRIGGER)";
    els.accelKey.disabled = true;
    els.steerField.style.opacity = ".4";
    els.modeHint.textContent = "Emulated Xbox pad — RT = throttle, A = select, X = restart.";
    els.modeHint.classList.remove("warn");
    api("/api/gamepad-connect", { method: "POST" }).catch(() => {});  // manette branchée
  } else {
    els.accelLabel.textContent = "ACCELERATE KEY";
    els.accelKey.disabled = running;
    els.steerField.style.opacity = "1";
    els.modeHint.textContent = gamepadAvailable ? "DirectInput keystrokes." : els.modeHint.textContent;
    api("/api/gamepad-disconnect", { method: "POST" }).catch(() => {});  // manette OFF
  }
}

function applyPreset(p) {
  preset = p === "fast" ? "fast" : "slowed";
  els.presetSeg.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.preset === preset));
  els.pollInterval.value = preset === "fast" ? "0.2" : "1.0";
}
// Verifie ViGEmBus (sans brancher) -> active/grise le bouton GAMEPAD.
async function checkGamepad() {
  try {
    const r = await api("/api/gamepad-check");
    gamepadAvailable = !!r.ok;
    const gp = els.modeSeg.querySelector('[data-mode="gamepad"]');
    gp.disabled = running || !gamepadAvailable;
    gp.classList.toggle("disabled", !gamepadAvailable);
    gp.title = gamepadAvailable ? "" : r.message;
    if (!gamepadAvailable) {
      if (mode === "gamepad") applyMode("keyboard");
      els.modeHint.textContent = r.message;
      els.modeHint.classList.add("warn");
    } else {
      els.modeHint.classList.remove("warn");
    }
  } catch { /* ignore */ }
  return gamepadAvailable;
}

// ---- config -----------------------------------------------------------
async function loadConfig() {
  const c = await api("/api/config");
  els.accelKey.value = c.accelerate_key ?? "w";
  els.steerKey.value = c.steer_key ?? "";
  els.startDelay.value = c.start_delay_s ?? 4;
  els.pollInterval.value = c.loop_poll_s ?? 1.0;
  applyPreset(c.automation_preset === "fast" ? "fast" : "slowed");
  els.throttleMod.checked = !!c.throttle_modulation;
  els.launchEase.checked = !!c.launch_ease;
  els.telEnabled.checked = c.telemetry_enabled !== false;
  els.telHost.value = c.telemetry_host === "localhost" ? "localhost" : "127.0.0.1";
  els.telPort.value = c.telemetry_port ?? 5300;
  els.rpEnabled.checked = c.rich_presence_enabled !== false;
  applyMode(c.input_backend === "gamepad" ? "gamepad" : "keyboard");
}

async function saveTelemetry() {
  const port = parseInt(els.telPort.value, 10);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("Port must be 1-65535.");
  await api("/api/config", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      telemetry_enabled: els.telEnabled.checked,
      telemetry_host: els.telHost.value,
      telemetry_port: port,
    }),
  });
  logLine(`Telemetry ${els.telEnabled.checked ? "armed" : "disabled"} on ${els.telHost.value}:${port}.`, "l-sys");
}

async function saveDiscord() {
  await api("/api/config", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rich_presence_enabled: els.rpEnabled.checked }),
  });
  logLine(`Discord Rich Presence ${els.rpEnabled.checked ? "shown" : "hidden"}.`, "l-sys");
}
function collectConfig() {
  const startDelay = parseFloat(els.startDelay.value);
  const pollInterval = parseFloat(els.pollInterval.value);
  const accelKey = els.accelKey.value.trim();
  if (!Number.isFinite(startDelay) || startDelay < 0 || startDelay > 60) throw new Error("Start delay must be 0-60 seconds.");
  if (!Number.isFinite(pollInterval) || pollInterval < 0.1 || pollInterval > 60) throw new Error("Poll interval must be 0.1-60 seconds.");
  if (mode === "keyboard" && !accelKey) throw new Error("Accelerate key is required.");
  return {
    input_backend: mode,
    accelerate_key: accelKey || "w",
    steer_key: els.steerKey.value.trim() || null,
    start_delay_s: startDelay,
    loop_poll_s: pollInterval,
    automation_preset: preset,
    throttle_modulation: els.throttleMod.checked,
    launch_ease: els.launchEase.checked,
  };
}
async function saveConfig() {
  await api("/api/config", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(collectConfig()),
  });
  logLine("Config saved.", "l-sys");
}

// ---- run --------------------------------------------------------------
async function toggleRun() {
  if (runBusy) return;
  runBusy = true;
  els.runBtn.disabled = true;
  try {
    if (running) { await api("/api/stop", { method: "POST" }); return; }
    await saveConfig();
    const maxLaps = parseInt(els.maxLaps.value, 10);
    if (!Number.isInteger(maxLaps) || maxLaps < 0 || maxLaps > 1000000) throw new Error("Max laps must be 0-1000000.");
    await api("/api/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max_laps: maxLaps }),
    });
  } catch (e) {
    logLine(e.message, "l-err");
  } finally {
    runBusy = false;
    els.runBtn.disabled = false;
  }
}

async function togglePause() {
  if (!running) return;
  try {
    if (manualPaused) {
      await api("/api/resume", { method: "POST" });
      manualPaused = false;
      logLine("Resumed (manual).", "l-state");
    } else {
      await api("/api/pause", { method: "POST" });
      manualPaused = true;
      logLine("Paused (manual).", "l-state");
    }
    updatePauseBtn();
  } catch (e) {
    logLine(e.message, "l-err");
  }
}

// ---- SSE --------------------------------------------------------------
function connect() {
  const es = new EventSource("/api/events");
  es.onopen = () => { els.conn.textContent = "● LINK"; els.conn.className = "conn on"; };
  es.onerror = () => { els.conn.textContent = "● LINK"; els.conn.className = "conn off"; };
  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "status") setStatus(ev.state, ev.laps, ev.elapsed_s);
    else if (ev.type === "done") { /* status event already covers it */ }
    else if (ev.type === "log") {
      let cls = "l-sys";
      if (ev.msg.startsWith("ERROR")) cls = "l-err";
      else if (ev.msg.includes("->")) cls = "l-action";
      else if (/AFK running|Starting|reached|Stop requested|Failsafe/.test(ev.msg)) cls = "l-state";
      logLine(ev.msg, cls);
    }
  };
}

// ---- bind -------------------------------------------------------------
els.modeSeg.addEventListener("click", (e) => {
  const b = e.target.closest(".seg-btn");
  if (b && !b.disabled) applyMode(b.dataset.mode);
});
els.presetSeg.addEventListener("click", (e) => {
  const b = e.target.closest(".seg-btn");
  if (b && !b.disabled) applyPreset(b.dataset.preset);
});
els.saveBtn.addEventListener("click", () => saveConfig().catch((e) => logLine(e.message, "l-err")));
els.runBtn.addEventListener("click", toggleRun);
els.pauseBtn.addEventListener("click", togglePause);
els.advToggle.addEventListener("click", () => {
  const open = els.advBody.hidden;
  els.advBody.hidden = !open;
  els.advToggle.setAttribute("aria-expanded", open ? "true" : "false");
});
els.clearLog.addEventListener("click", () => (els.log.innerHTML = ""));
els.telSaveBtn.addEventListener("click", () => saveTelemetry().catch((e) => logLine(e.message, "l-err")));
els.rpSaveBtn.addEventListener("click", () => saveDiscord().catch((e) => logLine(e.message, "l-err")));
// section tabs (SKILL POINTS / TELEMETRY) — TELEMETRY is a full screen that
// replaces the controller/guide/eventlab views + their tab row.
function currentView() {
  const t = els.tabs.querySelector(".tab.active");
  return t ? t.dataset.view : "controller";
}
function applySection(m) {
  document.querySelectorAll(".mode-tab").forEach((t) => t.classList.toggle("active", t.dataset.mode === m));
  const farm = m === "skill-points";
  els.tabs.style.display = farm ? "" : "none";
  ["controller", "guide", "eventlab"].forEach((v) => {
    const el = $(`view-${v}`);
    if (el) el.classList.toggle("active", farm && v === currentView());
  });
  els.viewTelemetry.classList.toggle("active", m === "telemetry");
  els.viewDiscord.classList.toggle("active", m === "discord");
}
els.modeTabs.addEventListener("click", (e) => {
  const b = e.target.closest(".mode-tab");
  if (b) applySection(b.dataset.mode);
});
// sub-tab switching
els.tabs.addEventListener("click", (e) => {
  const b = e.target.closest(".tab");
  if (!b) return;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === b));
  const view = b.dataset.view;
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
});
// copy EventLab codes
document.querySelectorAll(".copy-btn").forEach((btn) => {
  btn.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(btn.dataset.code); } catch { /* ignore */ }
    const old = btn.textContent;
    btn.textContent = "COPIED";
    btn.classList.add("copied");
    setTimeout(() => { btn.textContent = old; btn.classList.remove("copied"); }, 1200);
  });
});

// ---- FH6 process detection -------------------------------------------
async function pollGame() {
  try {
    const r = await api("/api/game-status");
    els.gamePod.dataset.on = r.running ? "true" : "false";
    els.gameText.textContent = r.running ? "FH6 ✓" : "FH6 ✗";
    const d = r.display || { found: false };
    if (!d.found) {
      els.dispPod.dataset.disp = "nowin";
      els.dispText.textContent = "NO FH6 WINDOW";
    } else if (d.fullscreen) {
      els.dispPod.dataset.disp = "full";
      els.dispText.textContent = "FULLSCREEN";
    } else {
      els.dispPod.dataset.disp = "win";
      els.dispText.textContent = `WINDOWED ${d.rect.w}×${d.rect.h}`;
    }
  } catch {
    els.gamePod.dataset.on = "false";
    els.gameText.textContent = "FH6 ?";
    els.dispPod.dataset.disp = "nowin";
    els.dispText.textContent = "DISPLAY ?";
  }
}

// ---- live telemetry readout ------------------------------------------
async function pollTelemetry() {
  try {
    const r = await api("/api/telemetry");
    els.telState.dataset.on = !r.enabled ? "off" : (r.available ? "on" : "wait");
    els.telState.textContent = !r.enabled ? "DISABLED" : (r.available ? "ONLINE" : "WAITING");
    els.telSource.textContent = `${r.host}:${r.port}`;
    els.telRace.textContent = r.available ? (r.race_on ? "ON" : "OFF") : "—";
    els.telSpeed.textContent = r.available ? `${Math.round(r.speed_kmh)} km/h` : "— km/h";
    els.telHint.textContent = !r.enabled
      ? "Telemetry disabled — rewind falls back to visual stuck detection."
      : (r.available ? "Live. Rewind fires only when speed ≈ 0 (jumps/off-road ignored)." : `Waiting for FH6 Data Out on ${r.host}:${r.port}…`);
  } catch { /* ignore */ }
  try {
    const rp = await api("/api/rich-presence");
    const st = !rp.enabled ? "DISABLED" : (rp.connected ? "CONNECTED" : "WAITING");
    els.telDiscord.textContent = st;
    els.rpState.dataset.on = !rp.enabled ? "off" : (rp.connected ? "on" : "wait");
    els.rpState.textContent = !rp.enabled ? "HIDDEN" : (rp.connected ? "ONLINE" : "WAITING");
    els.rpCar.textContent = rp.car || "—";
    els.rpStateLine.textContent = rp.state || "—";
    els.rpHint.textContent = !rp.enabled
      ? "Hidden — your Discord shows nothing from Cruise."
      : (rp.connected ? "Live on your Discord profile (timer = FH6 session)." : "Waiting for Discord — make sure the Discord app is running.");
  } catch { /* ignore */ }
}

(async () => {
  await checkGamepad();  // dispo ViGEmBus -> active/grise le bouton GAMEPAD
  await loadConfig();    // applyMode respecte la disponibilite
})();
connect();
pollGame();
setInterval(pollGame, 3000);
pollTelemetry();
setInterval(pollTelemetry, 700);
logLine("Telemetry deck online. Configure and arm.", "l-sys");
