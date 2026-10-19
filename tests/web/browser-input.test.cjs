// Actual browser handlers with deterministic clocks/network; no hardware.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const {webcrypto} = require("node:crypto");

function harness({autoAck = true, cryptoAPI = {randomUUID: () => "test-id"}} = {}) {
  const elements = new Map(), timers = new Map(), intervals = [], sockets = [], images = [];
  let now = 100, timerId = 0, closed = false;
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      style: {}, classList: {toggle() {}, add() {}, remove() {}, contains() {return false;}},
      events: {}, addEventListener(name, callback) {this.events[name] = callback;},
      getContext() {return {drawImage() {}};}, focus() {}, getBoundingClientRect() {return {left:0, top:0, width:1280, height:720};},
    });
    return elements.get(id);
  }
  const document = {
    pointerLockElement: null, events: {}, getElementById: element,
    addEventListener(name, callback) {this.events[name] = callback;},
    exitPointerLock() {this.pointerLockElement = null; this.events.pointerlockchange?.();},
    body: {classList: element("body").classList},
  };
  const window = {events: {}, addEventListener(name, callback) {this.events[name] = callback;}};
  class WebSocket {
    static OPEN = 1;
    constructor() {this.readyState = 1; sockets.push(this);}
    send() {}
    close() {}
  }
  const sandbox = {
    document, window, performance: {now: () => now},
    setInterval(fn, ms) {intervals.push({fn, ms});},
    setTimeout(fn) {timers.set(++timerId, fn); return timerId;},
    clearTimeout(id) {timers.delete(id);},
    fetch: () => new Promise(() => {}), location: {hash:"", pathname:"/", host:"127.0.0.1"},
    localStorage: {getItem() {return null;}, setItem() {}},
    crypto: cryptoAPI,
    WebSocket, console, navigator: {},
    Blob: class {}, URL: {createObjectURL() {return "blob:test";}, revokeObjectURL() {}},
    Image: class {constructor() {images.push(this);}},
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../../web/app.js"), "utf8"), sandbox);
  const state = vm.runInContext("state", sandbox);
  const sent = [];
  state.instanceId = "instance";
  state.pageSessionId = "page";
  state.mode = "human";
  state.lease = {generation:1};
  state.wsInput = {readyState:1, send(data) {
    const envelope = JSON.parse(data);
    sent.push(envelope);
    if (autoAck) sandbox.acknowledgeInput({sequence:envelope.sequence, status:"written"});
  }, close() {closed = true;}};
  const canvas = element("screen");
  canvas.requestPointerLock = () => {document.pointerLockElement = canvas; return Promise.resolve();};
  return {state, document, window, element, canvas, sent, sandbox, images, intervals, sockets,
    applyState: sandbox.applyState, isClosed: () => closed,
    tick(ms) {now += ms;},
    flush() {const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(fn => fn());},
  };
}

async function drain() {for (let i=0; i<10; i++) await Promise.resolve();}
async function main() {
  // Reproduce LAN HTTP API availability (randomUUID is secure-context-only).
  // Exercise actual handlers, including renewal, to avoid a delayed lease
  // loss after takeover appears to succeed.
  const lan = harness({cryptoAPI: {getRandomValues: bytes => webcrypto.getRandomValues(bytes)}});
  lan.sandbox.isSecureContext = false;
  lan.sandbox.location.host = "superuse.test:8765";
  const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const generated = Array.from({length:1000}, () => lan.sandbox.newRequestId());
  assert(generated.every(id => uuidPattern.test(id)), "HTTP fallback generates valid v4 UUIDs");
  assert.equal(new Set(generated).size, generated.length);
  const requests = [];
  lan.sandbox.fetch = async (url, options) => {
    const body = JSON.parse(options.body);
    assert(uuidPattern.test(body.request_id), "every control/command request uses the HTTP-compatible UUID helper");
    requests.push({url, body});
    const result = url.endsWith("takeover") ? {
      lease: {generation:1}, expires_in_ms:5000,
      state: {instance_id:"instance", state_revision:1, generation:1, mode:"human", owner:{session_id:"page"}},
    } : url.endsWith("renew") ? {expires_in_ms:5000}
      : {instance_id:"instance", state_revision:2, mode:"idle", owner:null};
    return {ok:true, json:async () => result};
  };
  await lan.element("btnTakeover").events.click();
  assert(lan.state.lease && !lan.isClosed());
  lan.tick(2000);
  lan.intervals.find(x => x.ms === 500).fn();
  await drain();
  assert(lan.state.lease && lan.state.nextRenew > 2100, "HTTP lease renewal succeeds without randomUUID");
  lan.element("btnCad").events.click();
  lan.element("btnAltTab").events.click();
  await drain();
  await lan.element("btnHandback").events.click();
  assert.equal(lan.state.mode, "idle");
  assert.equal(lan.state.lease, null);
  assert.deepEqual(requests.map(r => r.url), ["/v1/control/takeover", "/v1/control/renew", "/v1/commands", "/v1/commands", "/v1/control/handback"]);
  assert.equal(new Set(requests.map(r => r.body.request_id)).size, requests.length);
  assert.equal(harness().sandbox.newRequestId(), "test-id", "native randomUUID remains supported");

  const h = harness();
  assert.equal(h.state.mouseMode, "absolute", "absolute mouse default (vendor default)");
  h.state.mouseMode = "relative";  // exercise the relative flow explicitly
  h.canvas.events.mousedown({type:"mousedown", preventDefault() {}, buttons:1});
  assert.equal(h.sent.length, 0, "capture click must not press a target button");
  h.canvas.events.mousemove({movementX:10, movementY:0, buttons:1});
  h.flush();
  assert.equal(h.sent.length, 0, "capture gesture movement must be ignored");
  h.canvas.events.mouseup({type:"mouseup", preventDefault() {}, buttons:0});
  for (let i=0; i<100; i++) h.canvas.events.mousemove({movementX:3, movementY:-1.28, buttons:0});
  assert.equal(h.sent.length, 0, "batch high frequency movement for 16 ms");
  h.flush();
  assert.equal(h.sent.length, 3);
  assert.equal(h.sent.reduce((n,x) => n+x.action.dx,0), 300);
  assert.equal(h.sent.reduce((n,x) => n+x.action.dy,0), -128);
  assert(h.sent.every(x => Math.abs(x.action.dx)<=127 && Math.abs(x.action.dy)<=127));
  h.canvas.events.mousemove({movementX:5, movementY:0, buttons:0});
  h.canvas.events.mousedown({type:"mousedown", preventDefault() {}, buttons:1});
  assert.equal(h.sent.at(-2).action.dx, 5, "flush motion before button transition");
  assert.equal(h.sent.at(-1).action.buttons, 1);
  const lease = h.state.lease;
  h.document.exitPointerLock();
  assert(!h.isClosed() && h.state.lease === lease, "Esc keeps ownership and the ordered stream");
  assert.equal(h.sent.at(-1).action.buttons, 0);
  assert.equal(h.sent.at(-2).action.kind, "keyboard_state");
  assert.equal(h.sent.at(-2).action.keys.length, 0);

  const aligned = harness();
  aligned.applyState({device_mouse_mode: "relative", state_revision: 2});
  assert.equal(aligned.state.mouseMode, "relative", "page input mapping follows the device mode");
  assert(aligned.element("btnDeviceMode").textContent.includes("相对"), "device mode shown on the button");

  const viewer = harness();
  viewer.state.lease = null;
  viewer.canvas.events.mousedown({type:"mousedown", preventDefault() {}, buttons:1});
  assert.equal(viewer.document.pointerLockElement, null);

  const switching = harness(), oldLease = switching.state.lease;
  switching.element("mouseMode").events.change({target:{value:"absolute"}});
  assert(!switching.isClosed());
  assert.equal(switching.state.lease, oldLease, "mode changes retain ownership");
  switching.window.events.blur();
  assert(switching.isClosed() && switching.state.lease === null, "real blur still fences inputs");
  switching.applyState({mode:"human_hold", human_latched:true});
  assert(switching.element("status").textContent.includes("页面失去焦点"));
  assert(!switching.element("btnTakeover").disabled);
  assert(!switching.element("btnHandback").disabled, "paused human can explicitly hand back");

  const ordered = harness();
  ordered.applyState({instance_id:"instance", state_revision:12, generation:1, mode:"human", owner:{session_id:"page"}});
  ordered.applyState({instance_id:"instance", state_revision:11, mode:"transition"});
  assert(ordered.state.lease && !ordered.isClosed(), "late events cannot revoke a newer takeover grant");
  ordered.state.wsInput = null;
  ordered.sandbox.openInput();
  const oldSocket = ordered.state.wsInput;
  ordered.state.wsInput = {readyState:1};
  oldSocket.onclose();
  assert(ordered.state.wsInput && ordered.state.lease, "old socket callbacks cannot clear a new stream");

  const renew = harness();
  let renewals = 0;
  renew.sandbox.fetch = async (url) => {
    assert.equal(url, "/v1/control/renew");
    renewals++;
    return {ok:true, json:async () => ({expires_in_ms:5000})};
  };
  const renewTimer = renew.intervals.find(x => x.ms === 500).fn;
  for (let i=0; i<120; i++) {renew.tick(500); renewTimer(); await drain();}
  assert(renewals >= 25 && renew.state.lease, "renew beyond many lease lifetimes");

  const slow = harness({autoAck:false});
  for (let i=0; i<1000; i++) slow.sandbox.sendRelative(3, -2, 0);
  assert.equal(slow.sent.length, 4, "only four reports may await server completion");
  slow.sandbox.acknowledgeInput({sequence:1, status:"accepted"});
  assert.equal(slow.sent.length, 4, "acceptance is not serial completion");
  slow.sandbox.sendInput({kind:"keyboard_state", keys:["KeyA"], modifiers:[]});
  slow.sandbox.sendRelative(0, 0, 1);
  slow.sandbox.sendRelative(12, 0, 1);
  slow.sandbox.sendRelative(0, 0, 1, 1);
  slow.sandbox.sendRelative(0, 0, 0);
  slow.sandbox.sendInput({kind:"keyboard_state", keys:[], modifiers:[]});
  let completed = 0;
  while (completed < slow.sent.length) {
    slow.sandbox.acknowledgeInput({sequence:slow.sent[completed++].sequence, status:"written"});
    assert(slow.sent.length - completed <= 4, "never overfill the server during catch-up");
    assert(completed < 100, "pending motion must accumulate instead of growing a packet backlog");
  }
  const keyDown = slow.sent.findIndex(x => x.action.keys?.includes("KeyA"));
  const beforeKey = slow.sent.slice(0, keyDown);
  assert(beforeKey.length <= 6, "old motion cannot delay keydown by more than two pending reports");
  assert(beforeKey.reduce((sum,x) => sum+x.action.dx,0) < 3000, "squash excess movement under overload");
  const afterKey = slow.sent.slice(keyDown+1).map(x => x.action);
  assert.equal(afterKey[0].buttons, 1);
  assert.equal(afterKey.reduce((sum,x) => sum+(x.dx||0),0), 12);
  assert.equal(afterKey.find(x => x.wheel).wheel, 1);
  assert.equal(afterKey.at(-2).buttons, 0, "button release stays ordered");
  assert.equal(afterKey.at(-1).keys.length, 0, "keyup stays ordered");
  assert(slow.sent.every((x,i) => x.sequence === i+1));
  assert(slow.sent.filter(x => x.action.kind === "mouse_relative_state")
    .every(x => Math.abs(x.action.dx)<=127 && Math.abs(x.action.dy)<=127));
  assert(!slow.isClosed() && slow.state.lease, "a slow writer must not force a new takeover");

  const highDpi = harness({autoAck:false});
  highDpi.sandbox.sendRelative(5000, -5000, 0);
  assert(!highDpi.isClosed() && highDpi.sent.length === 4, "large valid gestures wait for ACKs");
  let highDpiCompleted = 0;
  while (highDpiCompleted < highDpi.sent.length) {
    highDpi.sandbox.acknowledgeInput({sequence:highDpi.sent[highDpiCompleted++].sequence, status:"written"});
  }
  assert(highDpi.sent.length <= 6, "high-DPI bursts have a fixed report budget");
  assert.equal(highDpi.sent.reduce((sum,x) => sum+x.action.dx,0), 762);
  assert.equal(highDpi.sent.reduce((sum,x) => sum+x.action.dy,0), -762);
  highDpi.sandbox.sendRelative(1000000, -500000, 0);
  assert(highDpi.state.lease && !highDpi.isClosed(), "no displacement threshold disconnects a real mouse gesture");
  assert(highDpi.sent.every(x => Object.keys(x.action).every(k => ["kind", "dx", "dy", "buttons", "wheel"].includes(k))), "local motion metadata never enters the wire action");

  // Sustained production faster than consumption was missing from the first
  // fix's finite-burst tests. Run two simulated minutes at 1000 Hz for each
  // writer rate, through the real mouse handler and batch timer callback.
  for (const writeMs of [2, 20, 100]) {
    const busy = harness({autoAck:false});
    busy.document.pointerLockElement = busy.canvas;
    let written = 0;
    for (let ms=1; ms<=120000; ms++) {
      busy.tick(1);
      busy.canvas.events.mousemove({movementX:80, movementY:-40, buttons:0});
      if (ms % 16 === 0) busy.flush();
      if (ms % writeMs === 0 && written < busy.sent.length) {
        busy.sandbox.acknowledgeInput({sequence:busy.sent[written++].sequence, status:"written"});
      }
      if (ms % 1000 === 0) {
        assert(busy.state.lease && !busy.isClosed(), "sustained overload retains human control");
        assert(busy.sent.length - written <= 4);
        assert(vm.runInContext("pendingInput.length <= 1 && pendingInput.every(e => Math.max(Math.abs(e.action.dx), Math.abs(e.action.dy)) <= 254)", busy.sandbox));
      }
    }
    busy.flush();
    busy.canvas.events.mouseup({type:"mouseup", preventDefault() {}, buttons:0});
    busy.sandbox.sendInput({kind:"keyboard_state", keys:[], modifiers:[]});
    let releaseWrites = 0;
    while (written < busy.sent.length) {
      busy.sandbox.acknowledgeInput({sequence:busy.sent[written++].sequence, status:"written"});
      assert(++releaseWrites <= 8, "release never waits behind an old motion history");
    }
    assert.equal(busy.sent.at(-2).action.buttons, 0);
    assert.equal(busy.sent.at(-1).action.keys.length, 0);
  }

  const reverse = harness({autoAck:false});
  for (let i=0; i<10000; i++) reverse.sandbox.sendRelative(80, 0, 0);
  reverse.sandbox.sendRelative(-10, 0, 0);
  reverse.sandbox.acknowledgeInput({sequence:1, status:"written"});
  assert.equal(reverse.sent.at(-1).action.dx, -10, "reverse saturated motion immediately");

  const stale = harness({autoAck:false});
  for (let i=0; i<100; i++) stale.sandbox.sendRelative(10, 0, 1);
  stale.sandbox.sendRelative(0, 0, 0);
  stale.tick(1000);
  let staleWritten = 0;
  while (staleWritten < stale.sent.length) {
    stale.sandbox.acknowledgeInput({sequence:stale.sent[staleWritten++].sequence, status:"written"});
  }
  assert.equal(stale.sent.at(-1).action.buttons, 0, "expire stale movement without dropping button release");
  assert(stale.sent.length <= 5, "do not replay stale pending motion after a stall");

  const missedButton = harness({autoAck:false});
  for (let i=0; i<4; i++) missedButton.sandbox.sendRelative(1, 0, 0);
  missedButton.sandbox.sendRelative(10, 0, 1);
  missedButton.tick(100);
  missedButton.sandbox.acknowledgeInput({sequence:1, status:"written"});
  assert.equal(missedButton.sent.at(-1).action.buttons, 1, "button change survives expiring movement");
  assert.equal(missedButton.sent.at(-1).action.dx, 0);

  const precise = harness({autoAck:false});
  for (let i=0; i<20; i++) {
    precise.sandbox.sendRelative(3, -2, 0);
    precise.sandbox.acknowledgeInput({sequence:precise.sent.at(-1).sequence, status:"written"});
  }
  assert.equal(precise.sent.reduce((sum,x) => sum+x.action.dx,0), 60, "normal motion remains exact");
  assert.equal(precise.sent.reduce((sum,x) => sum+x.action.dy,0), -40);

  const absolute = harness({autoAck:false});
  for (let i=0; i<10000; i++) absolute.sandbox.sendInput({kind:"mouse_state", basis_id:"frame", x:i, y:0, buttons:0}, true);
  absolute.sandbox.sendInput({kind:"mouse_state", basis_id:"frame", x:9999, y:0, buttons:1});
  absolute.sandbox.sendInput({kind:"mouse_state", basis_id:"frame", x:9999, y:0, buttons:0});
  absolute.tick(1000);
  let absoluteWritten = 0;
  while (absoluteWritten < absolute.sent.length) {
    absolute.sandbox.acknowledgeInput({sequence:absolute.sent[absoluteWritten++].sequence, status:"written"});
  }
  assert.equal(absolute.sent.at(-2).action.buttons, 1, "absolute click survives stale movement expiry");
  assert.equal(absolute.sent.at(-1).action.buttons, 0);
  assert(absolute.state.lease && !absolute.isClosed());

  const discarded = harness({autoAck:false});
  for (let i=0; i<100; i++) discarded.sandbox.sendRelative(1, 0, 0);
  discarded.sandbox.closeInput();
  const sentBeforeClose = discarded.sent.length;
  discarded.sandbox.acknowledgeInput({sequence:1, status:"written"});
  assert.equal(discarded.sent.length, sentBeforeClose, "revocation discards unsent input permanently");

  const video = harness();
  for (let i=1; i<=100; i++) video.sandbox.drawFrame({frame_id:i, image_width:1920, image_height:1080}, [1]);
  assert.equal(video.images.length, 1, "one decode in flight");
  video.images[0].onload();
  assert.equal(video.images.length, 2, "decode only the newest pending frame");
  video.images[1].onload();
  assert.equal(vm.runInContext("lastMeta.frame_id", video.sandbox), 100);
  console.log("browser regression: LAN HTTP UUID fallback (takeover/renew/commands/handback), capture, bounded motion, sustained 1000 Hz overload (3 writer rates x 120s), reversal, expiry, ordered release, retained lease, pause reasons, stale events, renewal, latest-frame decode passed");
}
main().catch(err => {console.error(err); process.exitCode = 1;});
