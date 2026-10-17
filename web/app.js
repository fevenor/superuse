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
  revision: -1,
  nextRenew: 0,
  renewingLease: null,
  lease: null,          // {instance_id, lease_id, generation} while holding control
  mode: null,
  humanLatched: false,
  source: null,
  frameAgeMs: null,
  connected: false,
  viewOnly: false,
  showCursor: true,
  mouseMode: "relative",
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
  } else {
    // Existing cookie identity: read the context (a fresh page session is
    // created per load; old leases never carry over).
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
      if (code === "INPUT_BACKPRESSURE") closeInput();
      if (code === "CONTROL_REVOKED" || code === "HUMAN_CONTROL" || code === "LEASE_EXPIRED") {
        state.lease = null;  // control lost; state events will update the UI
      }
    }
  };
}

function closeInput() {
  discardRelative();
  state.lease = null;
  if (document.pointerLockElement === canvas) document.exitPointerLock();
  if (state.wsInput) { const ws = state.wsInput; state.wsInput = null; ws.close(); }
  releaseLocalKeys();
}

function sendInput(action) {
  if (!state.wsInput || state.wsInput.readyState !== WebSocket.OPEN) return;
  if (!state.lease) return;
  state.sequence += 1;
  state.wsInput.send(JSON.stringify({
    api_version: 1,
    sequence: state.sequence,
    lease: state.lease,
    action,
  }));
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
             x: Math.floor(point.x), y: Math.floor(point.y), buttons: e.buttons});
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
  // Preserve deltas: unlike absolute mousemove these events cannot be replaced
  // with the last point. Server queue limits and input acks remain authoritative.
  dx = Math.round(dx); dy = Math.round(dy);
  const segments = Math.max(1, Math.ceil(Math.abs(dx) / 127), Math.ceil(Math.abs(dy) / 127));
  if (segments > 32) { closeInput(); flashError(new Error("相对位移过大，请重新接管")); return; }
  let previousX = 0, previousY = 0;
  for (let i = 1; i <= segments; i++) {
    const x = Math.round(dx * i / segments), y = Math.round(dy * i / segments);
    sendInput({kind: "mouse_relative_state", dx: x - previousX, dy: y - previousY,
               buttons, wheel: i === 1 ? wheel : 0});
    previousX = x; previousY = y;
  }
}

let relativeTimer = null, pendingX = 0, pendingY = 0, pendingButtons = 0;
function queueRelative(dx, dy, buttons) {
  if (!state.lease || !state.wsInput) return;
  if (buttons !== pendingButtons) flushRelative();
  pendingButtons = buttons;
  pendingX += dx; pendingY += dy;
  if (relativeTimer === null) relativeTimer = setTimeout(flushRelative, 8);
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
  state.mouseMode = e.target.value;
  try { localStorage.setItem("superuse.mouseMode", state.mouseMode); } catch (_) { /* optional preference */ }
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
window.addEventListener("blur", () => { if (state.lease) closeInput(); });
document.addEventListener("visibilitychange", () => {
  if (document.hidden && state.lease) closeInput();
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
      request_id: crypto.randomUUID(),
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
      body: JSON.stringify({request_id: crypto.randomUUID()}),
    });
    if (grant.state.state_revision < state.revision) throw new Error("控制状态已改变，请重新接管");
    state.lease = grant.lease;
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
      body: JSON.stringify({request_id: crypto.randomUUID()}),
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
  document.body.classList.toggle("simulated", state.source === "simulated");

  const ownerText = next.owner ? `${next.owner.kind}（${next.owner.label}）` : "无";
  const modeText = {
    idle: "空闲", agent: "agent 控制中", human: "人工控制",
    human_hold: "人工暂停（仍阻止 agent，可接管或交还）", transition: "接管屏障中…",
    recovery: "恢复中", stopping: "停止中",
  }[state.mode] || state.mode;
  statusEl.textContent = `${modeText} · 所有者 ${ownerText} · 串口 ${next.serial_status || "?"} · 视频 ${next.video_status || "?"}`;
  document.title = `Superuse — ${modeText}`;

  const ownLease = state.mode === "human" && next.owner?.session_id === state.pageSessionId
    && state.lease && next.generation === state.lease.generation;
  if (!ownLease) closeInput();
  updateControls();
  if (ownLease && !state.wsInput) openInput();
  const reasons = {human_input_closed: "输入连接已关闭", human_lease_expired: "租约已过期",
    human_session_closed: "页面会话已结束"};
  if (state.mode === "human_hold" && reasons[reason]) statusEl.textContent += ` · ${reasons[reason]}`;
  if (state.mode === "recovery") showOverlay("恢复中", "设备状态不确定，正在中和与恢复…");
  else if (state.mode !== "idle") overlay.hidden = true;
  else overlay.hidden = !overlay.hidden ? true : overlay.hidden;
}

function updateControls() {
  const takeoverBtn = document.getElementById("btnTakeover");
  const handbackBtn = document.getElementById("btnHandback");
  takeoverBtn.disabled = !!state.lease || state.mode === "transition" || state.mode === "stopping";
  handbackBtn.disabled = !(state.mode === "human_hold" || (state.mode === "human" && state.lease));
  takeoverBtn.classList.toggle("takeover-active", state.mode === "human");
}

function updateCursor() {
  canvas.style.cursor = state.showCursor ? "default" : "none";
  document.getElementById("btnCursor").classList.toggle("active", !state.showCursor);
}

setInterval(() => {
  if (!state.pageToken) return;
  api("/v1/session/heartbeat", {method: "POST", body: "{}"}).catch(() => {});
}, 5000);

setInterval(() => {
  if (state.lease && state.wsInput && performance.now() >= state.nextRenew && !state.renewingLease) {
    const lease = state.lease;
    state.renewingLease = lease;
    api("/v1/control/renew", {
      method: "POST",
      body: JSON.stringify({request_id: crypto.randomUUID(), lease}),
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
    statusEl.textContent = "已连接（观看模式）";
    showOverlay("观看模式", "点击工具栏“接管”取得键鼠控制。");
    canvas.focus();
  } catch (err) {
    state.connected = false;
    showOverlay("无法连接", `${err.code || ""} ${err.message}。请从托盘“打开界面”重新进入。`);
  }
})();
