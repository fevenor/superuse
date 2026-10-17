// Actual browser handlers with deterministic clocks/network; no hardware.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

function harness() {
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
    crypto: {randomUUID: () => "test-id"},
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
  state.wsInput = {readyState:1, send(data) {sent.push(JSON.parse(data));}, close() {closed = true;}};
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
  const h = harness();
  assert.equal(h.state.mouseMode, "relative", "relative mouse default");
  h.canvas.events.mousedown({type:"mousedown", preventDefault() {}, buttons:1});
  assert.equal(h.sent.length, 0, "capture click must not press a target button");
  h.canvas.events.mousemove({movementX:10, movementY:0, buttons:1});
  h.flush();
  assert.equal(h.sent.length, 0, "capture gesture movement must be ignored");
  h.canvas.events.mouseup({type:"mouseup", preventDefault() {}, buttons:0});
  for (let i=0; i<100; i++) h.canvas.events.mousemove({movementX:3, movementY:-1.28, buttons:0});
  assert.equal(h.sent.length, 0, "batch high frequency movement for 8 ms");
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

  const video = harness();
  for (let i=1; i<=100; i++) video.sandbox.drawFrame({frame_id:i, image_width:1920, image_height:1080}, [1]);
  assert.equal(video.images.length, 1, "one decode in flight");
  video.images[0].onload();
  assert.equal(video.images.length, 2, "decode only the newest pending frame");
  video.images[1].onload();
  assert.equal(vm.runInContext("lastMeta.frame_id", video.sandbox), 100);
  console.log("browser regression: capture, ordered/batched movement, release, retained lease, handback, stale events, renewal, latest-frame decode passed");
}
main().catch(err => {console.error(err); process.exitCode = 1;});
