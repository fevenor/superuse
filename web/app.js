/* Superuse human interface logic.
 *
 * Adapted from the upstream serial-hid-kvm viewer (MIT; see
 * web/vendor/upstream-viewer.html). Changes per third_party/CHANGES.md:
 * bootstrap -> browser context -> page session authentication, lease-aware
 * takeover/handback over HTTP, ordered HumanInputEnvelope stream over a
 * dedicated input WebSocket, state events drive the status line, video uses
 * the binary length-prefixed header framing. No third-party resources. */
"use strict";

const canvas = document.getElementById("screen");
const ctx = canvas.getContext("2d");
const statusEl = document.getElementById("status");
const fpsEl = document.getElementById("fps");
const overlay = document.getElementById("overlay");
const overlayTitle = document.getElementById("overlayTitle");
const overlayDetail = document.getElementById("overlayDetail");

const state = {
  instanceId: null,
  csrf: null,
  pageToken: null,
  pageSessionId: null,
  lan: false,           // true when authenticated through the LAN key entry
  heartbeatFailures: 0,
  revision: -1,
  nextRenew: 0,
  renewingLease: null,
  lease: null,          // {instance_id, lease_id, generation} while holding control
  mode: null,
  humanLatched: false,
  source: null,
  frameAgeMs: null,
  serialStatus: null,
  videoStatus: null,
  pauseReason: null,
  connected: false,
  viewOnly: false,
  showCursor: true,
  mouseMode: "absolute",   // page input mapping; aligned with the device mode
  deviceMouseMode: null,   // device-side persisted coordinate mode ("absolute"/"relative"/null)
  deviceInfoText: null,    // one-shot /v1/status device summary for the tooltip
  scaleMode: "fit",
  sequence: 0,
  heldKeys: new Set(),  // W3C codes currently held (non-modifier)
  heldModifiers: new Set(),
  wsVideo: null,
  wsInput: null,
  wsEvents: null,
  frameCount: 0,
  lastFpsTime: performance.now(),
};

/* ------------------------------------------------------------------ */
/* Authentication: bootstrap fragment -> browser context -> page       */
/* ------------------------------------------------------------------ */

function newRequestId() {
  // LAN HTTP is not a secure context: randomUUID may be absent, while
  // getRandomValues remains available. Preserve the UUID v4 wire contract.
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

async function api(path, options = {}) {
  const headers = Object.assign({"Content-Type": "application/json"}, options.headers || {});
  if (state.csrf) headers["X-CSRF-Token"] = state.csrf;
  if (state.pageToken) headers["X-Superuse-Page"] = state.pageToken;
  const response = await fetch(path, {...options, headers, credentials: "same-origin"});
  let body = null;
  try { body = await response.json(); } catch (e) { /* non-JSON error */ }
  if (!response.ok) {
    const code = body && body.error ? body.error.code : response.status;
    const message = body && body.error ? body.error.message : response.statusText;
    const err = new Error(message); err.code = code; err.response = body;
    throw err;
  }
  return body;
}

async function authenticate() {
  const hash = location.hash;
  const match = hash.match(/[#&]bootstrap=([A-Za-z0-9_-]+)/);
  if (match) {
    const bootstrap = match[1];
    history.replaceState(null, "", location.pathname);  // clear the fragment immediately
    const context = await api("/v1/sessions/browser", {
      method: "POST",
      body: JSON.stringify({bootstrap}),
    });
    state.csrf = context.csrf_token;
    state.instanceId = context.instance_id;
  } else if (await tryLanEntry()) {
    // LAN entry: key login already produced a LAN cookie + CSRF.
  } else {
    // Existing local cookie identity: read the context (a fresh page session
    // is created per load; old leases never carry over).
    const context = await api("/v1/browser/context");
    state.csrf = context.csrf_token;
    state.instanceId = context.instance_id;
  }
  const grant = await api("/v1/sessions/page", {
    method: "POST",
    body: JSON.stringify({label: "page"}),
  });
  state.pageToken = grant.page_token;
  state.pageSessionId = grant.session_id;
  state.instanceId = grant.instance_id;
}

/* LAN entry (task 2026-10-17 T5): /lan/context exists only on the LAN
 * listener. 404 -> this is the local entry; 401 -> key login required. */
async function tryLanEntry() {
  let context;
  try {
    context = await api("/lan/context");
  } catch (err) {
    if (err.code === 404 || err.code === "NOT_FOUND") return false;
    if (err.code !== "AUTH_FAILED") throw err;
    context = await showLanLogin();
  }
  state.csrf = context.csrf_token;
  state.instanceId = context.instance_id;
  state.lan = true;
  document.getElementById("btnLogout").hidden = false;
  return true;
}

function showLanLogin() {
  return new Promise((resolve, reject) => {
    const box = document.getElementById("login");
    const errorEl = document.getElementById("loginError");
    const input = document.getElementById("loginKey");
    box.hidden = false;
    input.focus();
    document.getElementById("loginForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      errorEl.textContent = "";
      try {
        await api("/lan/login", {method: "POST", body: JSON.stringify({key: input.value})});
        const context = await api("/lan/context");
        box.hidden = true;
        resolve(context);
      } catch (err) {
        if (err.code === "AUTH_FAILED") errorEl.textContent = "密钥错误";
        else if (err.code === "RATE_LIMITED") errorEl.textContent = "尝试过于频繁，请稍候";
        else if (err.code === "FORBIDDEN") errorEl.textContent = "局域网访问未开启";
        else errorEl.textContent = `${err.code || ""} ${err.message}`;
      }
    }, {once: false});
  });
}

document.getElementById("btnLogout").addEventListener("click", async () => {
  try { await api("/lan/logout", {method: "POST", body: "{}"}); } catch (e) { /* already gone */ }
  location.reload();
});

/* ------------------------------------------------------------------ */
/* WebSockets                                                          */
/* ------------------------------------------------------------------ */

function wsUrl(path) { return `ws://${location.host}${path}`; }

function openVideo() {
  const ws = new WebSocket(wsUrl("/v1/video"));
  ws.binaryType = "arraybuffer";
  state.wsVideo = ws;
  ws.onopen = () => ws.send(JSON.stringify({
    type: "authenticate", page_token: state.pageToken, csrf: state.csrf,
  }));
  ws.onmessage = (ev) => {
    if (!(ev.data instanceof ArrayBuffer)) return;
    const view = new DataView(ev.data);
    const headerLen = view.getUint32(0);
    if (ev.data.byteLength < 4 + headerLen) return;
    const headerText = new TextDecoder().decode(new Uint8Array(ev.data, 4, headerLen));
    let meta;
    try { meta = JSON.parse(headerText); } catch (e) { return; }
    const jpeg = new Uint8Array(ev.data, 4 + headerLen);
    drawFrame(meta, jpeg);
  };
  ws.onclose = () => { state.wsVideo = null; setTimeout(() => { if (state.connected) openVideo(); }, 2000); };
}

function openEvents() {
  const ws = new WebSocket(wsUrl("/v1/events"));
  state.wsEvents = ws;
  ws.onopen = () => ws.send(JSON.stringify({
    type: "authenticate", page_token: state.pageToken, csrf: state.csrf,
  }));
  ws.onmessage = (ev) => {
    if (typeof ev.data !== "string") return;
    let event;
    try { event = JSON.parse(ev.data); } catch (e) { return; }
    applyState(event.state || {}, event.reason);
  };
  ws.onclose = () => { state.wsEvents = null; setTimeout(() => { if (state.connected) openEvents(); }, 2000); };
}

function openInput() {
  if (state.wsInput) return;
  resetInputFlow();
  const ws = new WebSocket(wsUrl("/v1/input"));
  state.wsInput = ws;
  state.sequence = 0;
  ws.onopen = () => ws.send(JSON.stringify({
    type: "authenticate", page_token: state.pageToken, csrf: state.csrf,
  }));
  ws.onclose = () => {
    if (state.wsInput !== ws) return;
    state.wsInput = null;
    state.lease = null;
    state.pauseReason = "输入连接已断开";
    resetInputFlow();
    discardRelative();
    releaseLocalKeys();
    updateControls();
  };
  ws.onmessage = (ev) => {
    if (state.wsInput !== ws) return;
    if (typeof ev.data !== "string") return;
    let ack;
    try { ack = JSON.parse(ev.data); } catch (e) { return; }
    if (ack.type === "input_ack" && ack.status === "rejected") {
      const code = ack.error && ack.error.code;
      flashError({code, message: ack.error ? ack.error.message : "输入被拒绝"});
      if (code === "INPUT_BACKPRESSURE") closeInput("输入队列积压，已释放按键");
      if (code === "CONTROL_REVOKED" || code === "HUMAN_CONTROL" || code === "LEASE_EXPIRED") {
        closeInput("控制租约已失效");
      }
    }
    acknowledgeInput(ack);
  };
}

function closeInput(reason = null) {
  if (reason) state.pauseReason = reason;
  resetInputFlow();
  discardRelative();
  state.lease = null;
  if (document.pointerLockElement === canvas) document.exitPointerLock();
  if (state.wsInput) { const ws = state.wsInput; state.wsInput = null; ws.close(); }
  releaseLocalKeys();
}

// Motion is sampled before sequence assignment. Keep recent intent instead
// of replaying an unbounded history when the device is slower than the mouse.
// State transitions (including button/key releases) remain ordered/lossless.
const MAX_INPUT_IN_FLIGHT = 4, MAX_PENDING_INPUTS = 128;
const MAX_PENDING_RELATIVE_COUNTS = 254, MAX_MOTION_AGE_MS = 50;
const pendingInput = [], inputInFlight = new Set();
let queuedMouseButtons = 0;

function resetInputFlow() {
  pendingInput.length = 0;
  inputInFlight.clear();
  queuedMouseButtons = 0;
}

function boundMotion(action, limit = MAX_PENDING_RELATIVE_COUNTS) {
  const scale = Math.max(1, Math.abs(action.dx) / limit, Math.abs(action.dy) / limit);
  action.dx = Math.round(action.dx / scale);
  action.dy = Math.round(action.dy / scale);
}

function sendInput(action, motion = false) {
  if (!state.wsInput || state.wsInput.readyState !== WebSocket.OPEN) return;
  if (!state.lease) return;
  // Movement may report a button change if a DOM button event was missed.
  // Preserve that change independently so expiring motion cannot drop it.
  if (motion && action.buttons !== queuedMouseButtons) {
    sendInput(action.kind === "mouse_relative_state" ? {...action, dx: 0, dy: 0} : action);
    if (!state.lease) return;
  }
  if (action.kind === "mouse_relative_state" || action.kind === "mouse_state") {
    queuedMouseButtons = action.buttons;
  }
  const now = performance.now();
  const tail = pendingInput.at(-1);
  if (motion && tail?.motion && action.kind === "mouse_relative_state"
      && tail.action.kind === action.kind && tail.action.buttons === action.buttons) {
    for (const axis of ["dx", "dy"]) {
      // Under saturation a reversal expresses new intent; don't spend time
      // cancelling a large old vector before moving in the new direction.
      if (now - tail.updated > MAX_MOTION_AGE_MS
          || (tail.saturated && action[axis] && Math.sign(action[axis]) !== Math.sign(tail.action[axis]))) {
        tail.action[axis] = 0;
      }
      tail.action[axis] += action[axis];
    }
    tail.saturated = Math.max(Math.abs(tail.action.dx), Math.abs(tail.action.dy)) > MAX_PENDING_RELATIVE_COUNTS;
    boundMotion(tail.action);
    tail.updated = now;
  } else if (motion && tail?.motion && action.kind === "mouse_state" && tail.action.kind === action.kind
      && tail.action.buttons === action.buttons && tail.action.basis_id === action.basis_id) {
    tail.action = {...action};
    tail.updated = now;
  } else {
    if (pendingInput.length >= MAX_PENDING_INPUTS) {
      closeInput("输入队列积压，已释放按键");
      return;
    }
    const entry = {action: {...action}, motion, updated: now, saturated: false};
    if (motion && action.kind === "mouse_relative_state") {
      const freeSlots = pendingInput.length ? 0 : MAX_INPUT_IN_FLIGHT - inputInFlight.size;
      const budget = MAX_PENDING_RELATIVE_COUNTS + freeSlots * 127;
      entry.saturated = Math.max(Math.abs(action.dx), Math.abs(action.dy)) > budget;
      boundMotion(entry.action, budget);
    }
    pendingInput.push(entry);
  }
  pumpInput();
}

function acknowledgeInput(ack) {
  if (!["written", "coalesced", "rejected"].includes(ack.status)) return;
  if (inputInFlight.delete(ack.sequence)) pumpInput();
}

function pumpInput() {
  while (pendingInput.length && inputInFlight.size < MAX_INPUT_IN_FLIGHT
      && state.lease && state.wsInput?.readyState === WebSocket.OPEN) {
    const entry = pendingInput[0];
    if (entry.motion && performance.now() - entry.updated > MAX_MOTION_AGE_MS) {
      pendingInput.shift();
      continue;
    }
    let action = entry.action;
    if (action.kind === "mouse_relative_state") {
      const segments = Math.max(1, Math.ceil(Math.abs(action.dx) / 127), Math.ceil(Math.abs(action.dy) / 127));
      if (segments > 1) {
        const dx = Math.round(action.dx / segments), dy = Math.round(action.dy / segments);
        action.dx -= dx; action.dy -= dy;
        action = {...action, dx, dy, wheel: 0};
      } else pendingInput.shift();
    } else pendingInput.shift();
    const sequence = ++state.sequence;
    inputInFlight.add(sequence);
    state.wsInput.send(JSON.stringify({api_version: 1, sequence, lease: state.lease, action}));
  }
}

/* ------------------------------------------------------------------ */
/* Video rendering                                                     */
/* ------------------------------------------------------------------ */

let lastMeta = null;

function drawFrame(meta, jpegBytes) {
  // Bound browser decoding too: one active decode and one newest pending frame.
  if (decodingFrame) { pendingFrame = {meta, jpegBytes}; return; }
  decodingFrame = true;
  const blob = new Blob([jpegBytes], {type: "image/jpeg"});
  const url = URL.createObjectURL(blob);
  const img = new Image();
  img.onload = () => {
    if (canvas.width !== meta.image_width || canvas.height !== meta.image_height) {
      canvas.width = meta.image_width;
      canvas.height = meta.image_height;
      updateCanvasSize();
    }
    ctx.drawImage(img, 0, 0);
    URL.revokeObjectURL(url);
    state.frameCount++;
    lastMeta = meta;
    lastFrameArrival = performance.now();
    overlay.hidden = true;
    finishFrame();
  };
  img.onerror = () => { URL.revokeObjectURL(url); finishFrame(); };
  img.src = url;
}

let decodingFrame = false, pendingFrame = null;
function finishFrame() {
  decodingFrame = false;
  if (!pendingFrame) return;
  const next = pendingFrame;
  pendingFrame = null;
  drawFrame(next.meta, next.jpegBytes);
}

setInterval(() => {
  const now = performance.now();
  const elapsed = (now - state.lastFpsTime) / 1000;
  fpsEl.textContent = (state.frameCount / elapsed).toFixed(1) + " fps";
  state.frameCount = 0;
  state.lastFpsTime = now;
  refreshFreshness();
}, 2000);

let lastFrameArrival = performance.now();

function refreshFreshness() {
  if (performance.now() - lastFrameArrival > 2000) {
    showOverlay("画面过期", "视频超过 2 秒未更新；等待恢复…");
  }
}

function showOverlay(title, detail) {
  overlayTitle.textContent = title;
  overlayDetail.textContent = detail || "";
  overlay.hidden = false;
}

function updateCanvasSize() {
  if (canvas.width === 0 || canvas.height === 0) return;
  const container = document.getElementById("container");
  if (state.scaleMode === "fit") {
    const cw = container.clientWidth, ch = container.clientHeight;
    const ar = canvas.width / canvas.height;
    let dw, dh;
    if (cw / ch > ar) { dh = ch; dw = ch * ar; } else { dw = cw; dh = cw / ar; }
    canvas.style.width = dw + "px";
    canvas.style.height = dh + "px";
  } else {
    canvas.style.width = canvas.width + "px";
    canvas.style.height = canvas.height + "px";
  }
}
window.addEventListener("resize", updateCanvasSize);

/* ------------------------------------------------------------------ */
/* Input mapping                                                       */
/* ------------------------------------------------------------------ */

const MODIFIER_CODES = {
  ControlLeft: "left_ctrl", ControlRight: "right_ctrl",
  ShiftLeft: "left_shift", ShiftRight: "right_shift",
  AltLeft: "left_alt", AltRight: "right_alt",
  MetaLeft: "left_gui", MetaRight: "right_gui",
};

function imageCoords(e) {
  // Map a mouse event to *image pixel* coordinates inside the canvas,
  // excluding letterbox padding outside the painted area.
  const rect = canvas.getBoundingClientRect();
  const sx = canvas.width / rect.width;
  const sy = canvas.height / rect.height;
  const x = (e.clientX - rect.left) * sx;
  const y = (e.clientY - rect.top) * sy;
  if (x < 0 || y < 0 || x >= canvas.width || y >= canvas.height) return null;
  return {x, y};
}

function sendKeyboardState() {
  flushRelative();
  sendInput({
    kind: "keyboard_state",
    keys: Array.from(state.heldKeys).slice(0, 6),
    modifiers: Array.from(state.heldModifiers),
  });
}

function releaseLocalKeys() {
  state.heldKeys.clear();
  state.heldModifiers.clear();
}

let lastMoveSend = 0;
canvas.addEventListener("mousemove", (e) => {
  if (state.mouseMode === "relative") {
    if (!awaitingPointerLock && document.pointerLockElement === canvas) {
      queueRelative(e.movementX, e.movementY, e.buttons);
    }
    return;
  }
  const now = performance.now();
  if (now - lastMoveSend < 16) return;
  lastMoveSend = now;
  const point = imageCoords(e);
  if (!point) return;
  sendInput({kind: "mouse_state", basis_id: lastMeta ? lastMeta.basis_id : null,
             x: Math.floor(point.x), y: Math.floor(point.y), buttons: e.buttons}, true);
});
let awaitingPointerLock = false;
canvas.addEventListener("mousedown", (e) => {
  e.preventDefault(); canvas.focus();
  if (state.mouseMode === "relative" && document.pointerLockElement !== canvas) {
    if (!state.lease) return;
    awaitingPointerLock = true;
    Promise.resolve(canvas.requestPointerLock()).catch((err) => {
      awaitingPointerLock = false; flashError(err);
    });
    return;
  }
  sendMouse(e);
});
canvas.addEventListener("mouseup", (e) => { e.preventDefault(); sendMouse(e); });
canvas.addEventListener("contextmenu", (e) => e.preventDefault());

function sendMouse(e) {
  if (state.mouseMode === "relative") {
    flushRelative();
    if (awaitingPointerLock && e.type === "mouseup") { awaitingPointerLock = false; return; }
    if (!awaitingPointerLock && document.pointerLockElement === canvas) sendRelative(0, 0, e.buttons);
    return;
  }
  const point = imageCoords(e);
  if (!point) return;
  sendInput({kind: "mouse_state", basis_id: lastMeta ? lastMeta.basis_id : null,
             x: Math.floor(point.x), y: Math.floor(point.y), buttons: e.buttons});
}

function sendRelative(dx, dy, buttons, wheel = 0) {
  dx = Math.round(dx); dy = Math.round(dy);
  if (!Number.isSafeInteger(dx) || !Number.isSafeInteger(dy)) return;
  sendInput({kind: "mouse_relative_state", dx, dy, buttons, wheel}, !!(dx || dy) && !wheel);
}

let relativeTimer = null, pendingX = 0, pendingY = 0, pendingButtons = 0;
function queueRelative(dx, dy, buttons) {
  if (!state.lease || !state.wsInput) return;
  if (buttons !== pendingButtons) flushRelative();
  pendingButtons = buttons;
  pendingX += dx; pendingY += dy;
  if (relativeTimer === null) relativeTimer = setTimeout(flushRelative, 16);
}
function flushRelative() {
  if (relativeTimer !== null) clearTimeout(relativeTimer);
  relativeTimer = null;
  const dx = pendingX, dy = pendingY;
  pendingX = 0; pendingY = 0;
  if (dx || dy) sendRelative(dx, dy, pendingButtons);
}
function discardRelative() {
  if (relativeTimer !== null) clearTimeout(relativeTimer);
  relativeTimer = null;
  pendingX = 0; pendingY = 0; pendingButtons = 0;
}
function neutralizeInput() {
  flushRelative();
  releaseLocalKeys();
  // Both zero states share the ordered input stream with prior moves/buttons.
  // Changing capture/mode never releases the control lease.
  sendKeyboardState();
  sendRelative(0, 0, 0);
  pendingButtons = 0;
}

document.getElementById("mouseMode").addEventListener("change", (e) => {
  neutralizeInput();
  if (document.pointerLockElement === canvas) document.exitPointerLock();
  setInputMode(e.target.value);
  statusEl.textContent = state.mouseMode === "relative"
    ? "相对模式：接管后点击画面捕获鼠标；Esc 释放捕获，控制权仍保留"
    : "绝对模式：控制权保持不变";
  updateControls();
});
document.addEventListener("pointerlockchange", () => {
  if (document.pointerLockElement === canvas) {
    if (state.mouseMode !== "relative" || !state.lease) document.exitPointerLock();
    return;
  }
  if (document.pointerLockElement !== canvas) {
    awaitingPointerLock = false;
    if (state.mouseMode === "relative" && state.lease) neutralizeInput();
  }
});
document.addEventListener("pointerlockerror", () => {
  awaitingPointerLock = false;
  flashError(new Error("浏览器未允许捕获鼠标，请重新点击画面"));
});

canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  // Protocol wheel: positive scrolls up. DOM deltaY is positive downwards.
  let pixels = e.deltaY;
  if (e.deltaMode === 1) pixels *= 16;        // lines
  else if (e.deltaMode === 2) pixels *= 100;  // pages
  const steps = Math.max(-120, Math.min(120, Math.round(-pixels / 40))) || -Math.sign(pixels);
  if (steps === 0) return;
  if (state.mouseMode === "relative") {
    flushRelative();
    if (document.pointerLockElement === canvas) sendRelative(0, 0, e.buttons, steps);
    return;
  }
  const point = imageCoords(e);
  if (!point) return;
  sendInput({kind: "mouse_scroll", basis_id: lastMeta ? lastMeta.basis_id : null,
             x: Math.floor(point.x), y: Math.floor(point.y), steps});
}, {passive: false});

canvas.addEventListener("keydown", (e) => {
  if (state.viewOnly) return;
  if (e.code === "Escape" && document.pointerLockElement === canvas) {
    document.exitPointerLock();
    return;
  }
  e.preventDefault();
  e.stopPropagation();
  if (e.repeat) return;
  const modifier = MODIFIER_CODES[e.code];
  if (modifier) {
    state.heldModifiers.add(modifier);
  } else if (state.heldKeys.size < 6) {
    state.heldKeys.add(e.code);
  }
  sendKeyboardState();
});
canvas.addEventListener("keyup", (e) => {
  if (state.viewOnly) return;
  e.preventDefault();
  e.stopPropagation();
  const modifier = MODIFIER_CODES[e.code];
  if (modifier) state.heldModifiers.delete(modifier);
  else state.heldKeys.delete(e.code);
  sendKeyboardState();
});

/* Losing focus while holding control parks control in human_hold
 * (the input connection closes server-side); the page re-takes over with
 * the button after refocusing. */
window.addEventListener("blur", () => { if (state.lease) closeInput("页面失去焦点"); });
document.addEventListener("visibilitychange", () => {
  if (document.hidden && state.lease) closeInput("页面已切到后台");
});

/* ------------------------------------------------------------------ */
/* Toolbar actions                                                     */
/* ------------------------------------------------------------------ */

function comboKeySequence(modifierCodes, keyCode) {
  // Ordered discrete toolbar combos go over HTTP as bounded jobs.
  const modifiers = modifierCodes.map((c) => MODIFIER_CODES[c]);
  api("/v1/commands", {
    method: "POST",
    body: JSON.stringify({
      api_version: 1,
      request_id: newRequestId(),
      lease: state.lease,
      action: {kind: "press_key", code: keyCode, modifiers},
    }),
  }).catch((err) => flashError(err));
}

document.getElementById("btnCad").addEventListener("click", () => {
  comboKeySequence(["ControlLeft", "AltLeft"], "Delete");
  canvas.focus();
});
document.getElementById("btnAltTab").addEventListener("click", () => {
  comboKeySequence(["AltLeft"], "Tab");
  canvas.focus();
});
document.getElementById("btnCursor").addEventListener("click", () => {
  state.showCursor = !state.showCursor;
  updateCursor();
  canvas.focus();
});
document.getElementById("btnScale").addEventListener("click", () => {
  const btn = document.getElementById("btnScale");
  state.scaleMode = state.scaleMode === "native" ? "fit" : "native";
  btn.textContent = state.scaleMode === "native" ? "1:1" : "适应";
  updateCanvasSize();
  canvas.focus();
});
document.getElementById("btnFs").addEventListener("click", () => {
  if (!document.fullscreenElement) document.documentElement.requestFullscreen();
  else document.exitFullscreen();
  canvas.focus();
});

document.getElementById("btnTakeover").addEventListener("click", async () => {
  try {
    const grant = await api("/v1/control/takeover", {
      method: "POST",
      body: JSON.stringify({request_id: newRequestId()}),
    });
    if (grant.state.state_revision < state.revision) throw new Error("控制状态已改变，请重新接管");
    state.lease = grant.lease;
    state.pauseReason = null;
    state.nextRenew = performance.now() + Math.min(4000, grant.expires_in_ms / 3);
    applyState(grant.state);
    openInput();
    updateControls();
  } catch (err) { flashError(err); }
});

document.getElementById("btnHandback").addEventListener("click", async () => {
  try {
    const next = await api("/v1/control/handback", {
      method: "POST",
      body: JSON.stringify({request_id: newRequestId()}),
    });
    state.lease = null;
    closeInput();
    applyState(next);
    updateControls();
  } catch (err) { flashError(err); }
});

function flashError(err) {
  statusEl.textContent = `错误：${err.code || ""} ${err.message}`;
}

/* Device-side mouse coordinate mode (persisted in the SuperKVM device; the
 * local entry can switch it, the LAN entry cannot reach the endpoint). */
function setInputMode(mode) {
  state.mouseMode = mode;
  document.getElementById("mouseMode").value = mode;
  try { localStorage.setItem("superuse.mouseMode", mode); } catch (_) { /* optional preference */ }
}

let deviceBusy = false;  // a mode switch / restart transaction is in flight

function renderDeviceMode() {
  const btn = document.getElementById("btnDeviceMode");
  const restartBtn = document.getElementById("btnDeviceRestart");
  btn.disabled = deviceBusy;
  restartBtn.disabled = deviceBusy;
  restartBtn.hidden = btn.hidden;  // same visibility rules as the mode button
  const label = {absolute: "绝对", relative: "相对"}[state.deviceMouseMode] || "未知";
  btn.textContent = `设备鼠标:${label}`;
  btn.title = state.deviceMouseMode === "relative"
    ? "设备处于相对坐标模式（用于移动设备/多屏目标）；点击切回绝对（默认）"
    : state.deviceMouseMode === "absolute"
      ? "设备处于绝对坐标模式（默认）；点击切为相对（仅移动设备/多屏目标需要）"
      : "设备鼠标坐标模式未知；点击尝试设为绝对（默认）";
  if (state.deviceInfoText) btn.title += `\n${state.deviceInfoText}`;
  btn.hidden = !state.connected || state.lan || state.source === "simulated";
}

async function refreshDeviceInfo() {
  // One-shot read-only diagnostics for the tooltip (firmware/temperature).
  try {
    const status = await api("/v1/status");
    const d = status.device || {};
    state.deviceInfoText = [
      d.firmware ? `固件 ${d.firmware}` : null,
      typeof d.temperature_c === "number" ? `${d.temperature_c.toFixed(1)}°C` : null,
      d.video_low_power_enabled === true ? "视频低功耗:开" : null,
    ].filter(Boolean).join(" · ") || null;
  } catch (err) { state.deviceInfoText = null; }
  renderDeviceMode();
}

document.getElementById("btnDeviceMode").addEventListener("click", () => switchDeviceMode());
document.getElementById("btnDeviceRestart").addEventListener("click", () => restartDevice());

async function switchDeviceMode() {
  const target = state.deviceMouseMode === "absolute" ? "relative" : "absolute";
  await runDeviceTransaction(`切换为${target === "absolute" ? "绝对" : "相对"}…`,
    () => api("/v1/device/mouse-mode", {method: "POST", body: JSON.stringify({mode: target})}));
}

async function restartDevice() {
  if (!window.confirm("确定重启 SuperKVM 设备？期间控制中断约 15–30 秒，租约将回收。")) return;
  await runDeviceTransaction("设备重启中…", () => api("/v1/device/restart", {method: "POST", body: "{}"}));
}

async function runDeviceTransaction(hint, action) {
  if (deviceBusy) return;
  deviceBusy = true;
  renderDeviceMode();
  statusEl.textContent = hint;
  try {
    const resp = await action();
    if (resp && resp.state) applyState(resp.state, "device_mouse_mode");
    statusEl.textContent = "设备操作完成";
  } catch (err) {
    flashError(err);
  } finally {
    deviceBusy = false;
    renderDeviceMode();
  }
}

/* ------------------------------------------------------------------ */
/* State rendering, heartbeat, renewal                                 */
/* ------------------------------------------------------------------ */

function applyState(next, reason) {
  if (next.instance_id && next.instance_id !== state.instanceId) return;
  if (next.state_revision !== undefined) {
    if (next.state_revision < state.revision) return;
    state.revision = next.state_revision;
  }
  state.mode = next.mode !== undefined ? next.mode : state.mode;
  state.humanLatched = next.human_latched !== undefined ? next.human_latched : state.humanLatched;
  state.source = next.source !== undefined ? next.source : state.source;
  state.frameAgeMs = next.frame_age_ms !== undefined ? next.frame_age_ms : state.frameAgeMs;
  state.serialStatus = next.serial_status !== undefined ? next.serial_status : state.serialStatus;
  state.videoStatus = next.video_status !== undefined ? next.video_status : state.videoStatus;
  const deviceMode = next.device_mouse_mode !== undefined ? next.device_mouse_mode : state.deviceMouseMode;
  if (deviceMode !== state.deviceMouseMode) {
    state.deviceMouseMode = deviceMode;
    // The device gate rejects absolute reports in relative mode, so the page
    // input mapping follows the device setting (manual override stays free).
    if (["absolute", "relative"].includes(deviceMode) && state.mouseMode !== deviceMode) {
      neutralizeInput();
      if (document.pointerLockElement === canvas) document.exitPointerLock();
      setInputMode(deviceMode);
    }
    renderDeviceMode();
  } else {
    renderDeviceMode();
  }
  document.body.classList.toggle("simulated", state.source === "simulated");

  const ownerText = next.owner ? `${next.owner.kind}（${next.owner.label}）` : "无";
  const modeText = {
    idle: "空闲", agent: "agent 控制中", human: "人工控制",
    human_hold: "人工控制已暂停（agent 仍被阻止）", transition: "接管屏障中…",
    recovery: "恢复中", stopping: "停止中",
  }[state.mode] || state.mode;
  statusEl.textContent = `${modeText} · 所有者 ${ownerText} · 串口 ${next.serial_status || "?"} · 视频 ${next.video_status || "?"}`;
  if (state.deviceMouseMode) {
    statusEl.textContent += ` · 设备鼠标 ${state.deviceMouseMode === "absolute" ? "绝对" : "相对"}`;
  }
  document.title = `Superuse — ${modeText}`;

  const ownLease = state.mode === "human" && next.owner?.session_id === state.pageSessionId
    && state.lease && next.generation === state.lease.generation;
  if (!ownLease) closeInput();
  updateControls();
  if (ownLease && !state.wsInput) openInput();
  const reasons = {human_lease_expired: "控制租约已过期", human_session_closed: "页面会话已结束",
    input_backpressure_fence: "输入队列积压，已释放按键"};
  if (state.mode === "human_hold") {
    if (reasons[reason]) state.pauseReason = reasons[reason];
    if (!state.pauseReason && reason === "human_input_closed") state.pauseReason = "输入连接已关闭";
    statusEl.textContent += ` · ${state.pauseReason || "等待人工恢复"} · 点击“接管”继续，或“交还”允许 agent`;
  }
  const videoDown = state.videoStatus === "disconnected" || state.videoStatus === "fault";
  const serialDown = state.serialStatus === "disconnected" || state.serialStatus === "fault";
  if (videoDown || serialDown) {
    const detail = next.video_detail || next.serial_detail || "等待 SuperKVM 设备接入…";
    showOverlay("请连接 SuperKVM 设备", detail);
  } else if (state.mode === "recovery") showOverlay("恢复中", "设备状态不确定，正在中和与恢复…");
  else if (state.mode !== "idle") overlay.hidden = true;
  else overlay.hidden = !overlay.hidden ? true : overlay.hidden;
}

function deviceReady() {
  // Only an explicitly bad device state disables control; a state event that
  // omits the fields (older consumers, tests) changes nothing.
  const serialDown = state.serialStatus === "disconnected" || state.serialStatus === "fault";
  const videoDown = state.videoStatus === "disconnected" || state.videoStatus === "fault";
  return !(serialDown || videoDown);
}

function updateControls() {
  const takeoverBtn = document.getElementById("btnTakeover");
  const handbackBtn = document.getElementById("btnHandback");
  takeoverBtn.disabled = !!state.lease || state.mode === "transition" || state.mode === "stopping"
    || !deviceReady();
  handbackBtn.disabled = !(state.mode === "human_hold" || (state.mode === "human" && state.lease));
  takeoverBtn.classList.toggle("takeover-active", state.mode === "human");
}

function updateCursor() {
  canvas.style.cursor = state.showCursor ? "default" : "none";
  document.getElementById("btnCursor").classList.toggle("active", !state.showCursor);
}

setInterval(() => {
  if (!state.pageToken) return;
  api("/v1/session/heartbeat", {method: "POST", body: "{}"}).then(() => {
    state.heartbeatFailures = 0;
  }).catch((err) => {
    // A LAN generation change kills the cookie chain: re-login via reload.
    if (state.lan && err.code === "AUTH_FAILED" && ++state.heartbeatFailures >= 3) location.reload();
  });
}, 5000);

setInterval(() => {
  if (state.lease && state.wsInput && performance.now() >= state.nextRenew && !state.renewingLease) {
    const lease = state.lease;
    state.renewingLease = lease;
    api("/v1/control/renew", {
      method: "POST",
      body: JSON.stringify({request_id: newRequestId(), lease}),
    })
      .then((grant) => {
        if (state.lease === lease) state.nextRenew = performance.now() + Math.min(4000, grant.expires_in_ms / 3);
      })
      .catch((err) => {
        if (state.lease !== lease) return;
        flashError(err);
        if (["CONTROL_REVOKED", "LEASE_EXPIRED", "INSTANCE_CHANGED", "AUTH_FAILED"].includes(err.code)) {
          closeInput(); updateControls();
        }
      }).finally(() => { if (state.renewingLease === lease) state.renewingLease = null; });
  }
}, 500);

/* Fullscreen toolbar auto-hide (from upstream). */
let _tbTimer = null;
document.addEventListener("fullscreenchange", () => {
  const isFs = !!document.fullscreenElement;
  document.body.classList.toggle("fs-autohide", isFs);
  if (!isFs) document.getElementById("toolbar").classList.remove("visible");
  if (isFs && navigator.keyboard && navigator.keyboard.lock) {
    navigator.keyboard.lock(["AltLeft", "AltRight", "Tab", "MetaLeft", "MetaRight", "Escape"]).catch(() => {});
  }
  updateCanvasSize();
});
document.addEventListener("mousemove", (e) => {
  if (!document.body.classList.contains("fs-autohide")) return;
  const toolbar = document.getElementById("toolbar");
  if (e.clientY < 50) {
    toolbar.classList.add("visible");
    clearTimeout(_tbTimer);
  } else if (toolbar.classList.contains("visible")) {
    clearTimeout(_tbTimer);
    _tbTimer = setTimeout(() => toolbar.classList.remove("visible"), 1500);
  }
});

/* ------------------------------------------------------------------ */
/* Start                                                               */
/* ------------------------------------------------------------------ */

// Only non-sensitive preference is persisted; identities and leases remain in memory.
try {
  const saved = localStorage.getItem("superuse.mouseMode");
  if (["absolute", "relative"].includes(saved)) state.mouseMode = saved;
} catch (_) { /* storage may be disabled */ }
document.getElementById("mouseMode").value = state.mouseMode;

(async function start() {
  try {
    await authenticate();
    state.connected = true;
    openEvents();
    openVideo();
    renderDeviceMode();
    refreshDeviceInfo();
    statusEl.textContent = "已连接（观看模式）";
    showOverlay("观看模式", "点击工具栏“接管”取得键鼠控制。");
    canvas.focus();
  } catch (err) {
    state.connected = false;
    showOverlay("无法连接", `${err.code || ""} ${err.message}。请从托盘“打开界面”重新进入。`);
  }
})();
