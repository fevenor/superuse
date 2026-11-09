"use strict";

(() => {
  const byId = id => document.getElementById(id);
  const defaults = {scaleMode:"fit", displayWidth:0, displayHeight:0, zoom:1, cursorStyle:"default", wheelDirection:1, wheelSpeed:1, showCursor:true};
  let overview = null, logEntries = [], busy = false, polling = false;
  const message = (text, error = false, display = false) => {
    const target = byId(display ? "displayMessage" : "settingsMessage");
    target.textContent = text;
    target.classList.toggle("error", error);
  };
  const saveDisplay = () => {
    const value = Object.fromEntries(Object.keys(defaults).map(key => [key, state[key]]));
    try { localStorage.setItem("superuse.display", JSON.stringify(value)); } catch (_) {}
  };
  try {
    const value = JSON.parse(localStorage.getItem("superuse.display") || "null");
    if (value && ["fit","fill","native"].includes(value.scaleMode)
        && Number.isFinite(value.zoom) && value.zoom >= 0.1 && value.zoom <= 4
        && [0.5,1,2,3].includes(value.wheelSpeed) && [1,-1].includes(value.wheelDirection)
        && ["default","crosshair","grab","cell"].includes(value.cursorStyle)
        && Number.isInteger(value.displayWidth) && value.displayWidth >= 0 && value.displayWidth <= 4096
        && Number.isInteger(value.displayHeight) && value.displayHeight >= 0 && value.displayHeight <= 2160) {
      for (const key of Object.keys(defaults)) if (key in value) state[key] = value[key];
      updateCursor();
    }
  } catch (_) {}
  window.superuseDisplayChanged = saveDisplay;
  function showDialog(id) {
    neutralizeInput();
    if (document.pointerLockElement) document.exitPointerLock();
    byId(id).showModal();
  }
  for (const button of document.querySelectorAll("[data-close]")) {
    button.addEventListener("click", () => byId(button.dataset.close).close());
  }
  function displayValues() {
    byId("displayFit").value = state.scaleMode;
    byId("displayWidth").value = state.displayWidth || lastMeta?.native_width || 1920;
    byId("displayHeight").value = state.displayHeight || lastMeta?.native_height || 1080;
    const resolution = `${state.displayWidth}x${state.displayHeight}`;
    byId("displayResolution").value = [...byId("displayResolution").options].some(o => o.value === resolution) ? resolution : "custom";
    byId("displayZoom").value = Math.round(state.zoom * 100);
    byId("zoomValue").textContent = `${Math.round(state.zoom * 100)}%`;
    byId("cursorStyle").value = state.showCursor ? state.cursorStyle : "none";
    byId("wheelDirection").value = state.wheelDirection;
    byId("wheelSpeed").value = state.wheelSpeed;
  }
  byId("btnDisplay").addEventListener("click", () => {displayValues(); showDialog("displayDialog");});
  byId("displayZoom").addEventListener("input", () => {byId("zoomValue").textContent = `${byId("displayZoom").value}%`;});
  byId("displayResolution").addEventListener("change", () => {
    const value = byId("displayResolution").value;
    if (value !== "custom" && value !== "0x0") {
      const [width,height] = value.split("x");
      byId("displayWidth").value = width; byId("displayHeight").value = height;
    }
    byId("displayFit").value = "native";
  });
  for (const id of ["displayWidth","displayHeight"]) byId(id).addEventListener("input", () => {byId("displayResolution").value = "custom";});
  byId("displayForm").addEventListener("submit", event => {
    event.preventDefault();
    state.scaleMode = byId("displayFit").value;
    const native = byId("displayResolution").value === "0x0";
    state.displayWidth = native ? 0 : Number(byId("displayWidth").value);
    state.displayHeight = native ? 0 : Number(byId("displayHeight").value);
    state.zoom = Number(byId("displayZoom").value) / 100;
    state.showCursor = byId("cursorStyle").value !== "none";
    state.cursorStyle = state.showCursor ? byId("cursorStyle").value : "default";
    state.wheelDirection = Number(byId("wheelDirection").value);
    state.wheelSpeed = Number(byId("wheelSpeed").value);
    updateCanvasSize(); updateCursor(); saveDisplay();
    message("显示设置已保存。", false, true);
  });
  byId("displayReset").addEventListener("click", () => {Object.assign(state,defaults); displayValues(); updateCanvasSize(); updateCursor(); saveDisplay();});
  function download(data, name) {
    const url = URL.createObjectURL(data), link = document.createElement("a");
    link.href = url; link.download = name; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }
  byId("btnScreenshot").addEventListener("click", async () => {
    const button = byId("btnScreenshot"); button.disabled = true;
    try {
      const response = await fetch("/v1/frames/screenshot.png", {credentials:"same-origin", headers:{"X-Superuse-Page":state.pageToken}});
      if (!response.ok) throw new Error((await response.json()).error?.message || "截图失败");
      download(await response.blob(), `Superuse-${new Date().toISOString().replace(/[:.]/g,"-")}.png`);
      statusEl.textContent = "原始分辨率截图已保存";
    } catch (error) {flashError(error);} finally {button.disabled = false;}
  });
  async function action(operation, value) {
    return api("/v1/device/action", {method:"POST", body:JSON.stringify({request_id:newRequestId(), operation, value})});
  }
  async function perform(fn, success = "设置已应用。") {
    if (busy) return;
    busy = true; message("正在处理…");
    byId("settingsDialog").setAttribute("aria-busy", "true");
    const buttons = [...byId("settingsDialog").querySelectorAll("button,input,select")];
    const old = buttons.map(e => e.disabled); buttons.forEach(e => {e.disabled = true;});
    try {
      await fn();
      await loadOverview(false);
      message(success);
    } catch (error) {message(`${error.code || ""} ${error.message}`, true);}
    finally {
      buttons.forEach((e,i) => {e.disabled = old[i];});
      if (overview) {
        byId("mouseAutoMove").disabled = ![0,1].includes(overview.info.mouse_auto_move_raw);
        byId("refreshInfo").disabled = !overview.capabilities.device_info;
      }
      busy = false; byId("settingsDialog").removeAttribute("aria-busy");
    }
  }
  const infoLabels = {firmware:"KVM 固件", hardware:"硬件版本", bootloader:"引导程序", temperature_c:"温度（°C）", serial_number:"设备序列", build_time:"编译时间", run_state_raw:"运行状态", mouse_coordinate_mode:"鼠标模式", mouse_scroll_direction_raw:"滚轮方向参数", mouse_sensitivity_raw:"鼠标灵敏度参数", udisk_direction_raw:"U 盘方向参数", usb_baud:"Target 上报波特率", usb_vid:"Target VID", usb_pid:"Target PID", usb_host_baud:"Host 上报波特率", usb_host_vid:"Host VID", usb_host_pid:"Host PID", video_low_power_enable_raw:"视频低功耗", mouse_auto_move_raw:"鼠标微动", edid_summary:"EDID 信息摘要"};
  function renderInfo(data, fillForm) {
    overview = data;
    state.deviceType = data.capabilities.device_type; renderDeviceMode();
    byId("keepAwake").checked = data.power.keep_awake;
    byId("powerStatus").textContent = data.power.error || (data.power.active ? "Windows 唤醒请求已生效" : "按 Windows 电源策略运行");
    const list = byId("deviceInfo"); list.replaceChildren();
    for (const [key,label] of Object.entries(infoLabels)) {
      if (!(key in data.info)) continue;
      const dt = document.createElement("dt"), dd = document.createElement("dd");
      dt.textContent = label;
      const value = data.info[key];
      dd.textContent = ["mouse_auto_move_raw","video_low_power_enable_raw"].includes(key) && ![0,1].includes(value) ? "固件未提供有效状态" : value ?? "未提供有效值";
      list.append(dt,dd);
    }
    if (!list.children.length) {const item = document.createElement("dd"); item.textContent = "设备未提供扩展信息"; list.append(item);}
    byId("captureActual").textContent = data.capture ? `实际采集 ${data.capture.width} × ${data.capture.height}` : "视频尚未就绪";
    byId("serialStats").textContent = `串口 ${data.serial.status} · 发送 ${data.serial.tx_bytes} 字节 · 接收 ${data.serial.rx_bytes} 字节`;
    byId("extendedSettings").hidden = !data.capabilities.video_power;
    byId("diagnosticsPanel").hidden = !data.capabilities.device_logs;
    byId("refreshInfo").disabled = !data.capabilities.device_info;
    byId("videoPower").checked = data.info.video_low_power_enable_raw === 1;
    byId("mouseAutoMove").checked = data.info.mouse_auto_move_raw === 1;
    byId("mouseAutoMove").disabled = ![0,1].includes(data.info.mouse_auto_move_raw);
    byId("mouseAutoMove").title = byId("mouseAutoMove").disabled ? "固件未提供有效状态，此选项不可用" : "";
    byId("enableLogs").checked = data.logs_enabled;
    if (fillForm) {
      byId("deviceType").value = data.connection.device_type;
      byId("serialBaud").replaceChildren(...data.baud_rates.map(rate => new Option(String(rate), String(rate))));
      byId("serialBaud").value = data.connection.baud;
      byId("captureWidth").value = data.connection.width; byId("captureHeight").value = data.connection.height; byId("captureFps").value = data.connection.fps;
      const rect = data.active_rect || [0,0,data.capture?.width || 1920,data.capture?.height || 1080];
      ["cropX","cropY","cropWidth","cropHeight"].forEach((id,i) => {byId(id).value = rect[i];});

    }
  }
  async function loadOverview(fillForm = true) {renderInfo(await api("/v1/device/overview"), fillForm);}
  byId("btnSettings").addEventListener("click", async () => {
    showDialog("settingsDialog"); message("");
    try {await loadOverview();} catch (error) {message(error.message,true);}
  });
  byId("keepAwake").addEventListener("change", () => {const enabled = byId("keepAwake").checked; perform(() => action("keep_awake",enabled));});
  byId("deviceType").addEventListener("change", () => {byId("serialBaud").value = byId("deviceType").value === "ch9329" ? "9600" : "2000000";});
  byId("scanDevices").addEventListener("click", () => perform(async () => {
    const devices = await api("/v1/device/discover");
    for (const kind of ["serial","video"]) byId(`${kind}Choice`).replaceChildren(new Option("保持当前设备", ""), ...devices[kind].map(d => new Option(d.label,d.id)));
  }, "扫描完成，请选择对应设备。"));
  byId("connectionForm").addEventListener("submit", event => {
    event.preventDefault();
    const value = {device_type:byId("deviceType").value, baud:Number(byId("serialBaud").value), width:Number(byId("captureWidth").value), height:Number(byId("captureHeight").value), fps:Number(byId("captureFps").value), serial_choice:byId("serialChoice").value, video_choice:byId("videoChoice").value};
    perform(() => action("connection",value), "连接参数已保存，正在按选定身份重新连接。重新接管后才能输入。");
  });
  byId("refreshInfo").addEventListener("click", () => perform(async () => {await action("refresh",null); await loadOverview();}, "设备信息已刷新。"));
  for (const [id,operation] of [["videoPower","video_low_power"],["mouseAutoMove","mouse_auto_move"]]) {
    byId(id).addEventListener("change", () => {const enabled = byId(id).checked; perform(() => action(operation,enabled));});
  }
  byId("calibrationForm").addEventListener("submit", event => {event.preventDefault(); const rect = ["cropX","cropY","cropWidth","cropHeight"].map(id => Number(byId(id).value)); perform(() => action("calibration",rect));});
  byId("detectCrop").addEventListener("click", () => perform(async () => {
    const result = await action("calibration_detect",null);
    ["cropX","cropY","cropWidth","cropHeight"].forEach((id,i) => {byId(id).value = result.rect[i];});
  }, "候选边界已填入，请确认后点击“应用校正”。"));
  byId("resetCrop").addEventListener("click", () => perform(() => action("calibration",null)));
  function renderLogs() {
    const filter = byId("logFilter").value.toLowerCase();
    const rows = logEntries.map(row => `${new Date(row.received_at*1000).toLocaleTimeString()} [${row.product}/${row.application}/${row.module}] ${row.message}`);
    byId("deviceLogs").textContent = rows.filter(row => row.toLowerCase().includes(filter)).join("\n") || (byId("enableLogs").checked ? "等待设备日志…" : "日志接收已关闭。");
  }
  byId("logFilter").addEventListener("input",renderLogs);
  byId("enableLogs").addEventListener("change", () => {const enabled = byId("enableLogs").checked; perform(async () => {const result = await action("logs",{enabled}); logEntries = result.entries; renderLogs();});});
  byId("clearLogs").addEventListener("click", () => perform(async () => {await action("logs",{clear:true}); logEntries=[]; renderLogs();}));
  byId("exportLogs").addEventListener("click", () => download(new Blob([byId("deviceLogs").textContent],{type:"text/plain;charset=utf-8"}),"Superuse-device-log.txt"));
  setInterval(async () => {
    byId("btnSettings").hidden = !state.connected || state.lan;
    if (state.connected && !state.lan && !state.deviceType) {
      try {await loadOverview(false);} catch (_) {}
    }
    if (!byId("settingsDialog").open || !byId("enableLogs").checked || busy || polling) return;
    polling = true;
    try {const result = await api("/v1/device/logs"); logEntries = result.entries; renderLogs();} catch (_) {}
    finally {polling = false;}
  }, 2000);
})();
