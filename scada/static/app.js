/* 三容水箱 SCADA 前端
 * 数据来源: WebSocket /ws (10 Hz) + REST /api/history (首屏回填)
 * 职责: 只做呈现与指令下发, 不含任何控制逻辑。
 */
"use strict";

const HMAX = 350;                 // 水箱物理高度 mm
const BUFFER_MS = 5 * 60 * 1000;  // 趋势保留 5 分钟

// 各水箱在 SVG 中的几何位置 (与 index.html 一致)
const TANKS = {
  h1: { water: "water1", val: "val1", top: 30, height: 180, sp: "spmark1" },
  h2: { water: "water2", val: "val2", top: 30, height: 180, sp: "spmark2" },
  h3: { water: "water3", val: "val3", top: 270, height: 110, sp: null },
};

const $ = (id) => document.getElementById(id);

const state = {
  ws: null,
  latest: null,
  buffer: [],
  chart: null,
  lastChartDraw: 0,
  userTouched: 0,
  initialized: false,
};

/* ------------------------------------------------------------------ 图表 */
function initChart() {
  if (typeof echarts === "undefined") {
    $("chart").innerHTML =
      '<p style="color:#7c8ca5;font-size:13px;padding:20px">ECharts 未能从 CDN 加载，趋势图不可用。' +
      '请检查网络，或将 echarts.min.js 放入 scada/static/ 并修改 index.html 的引用。</p>';
    return;
  }

  state.chart = echarts.init($("chart"), null, { renderer: "canvas" });

  const line = (name, color, dashed) => ({
    name,
    type: "line",
    showSymbol: false,
    smooth: 0.2,
    lineStyle: { width: dashed ? 1.4 : 2, color, type: dashed ? "dashed" : "solid", opacity: dashed ? 0.75 : 1 },
    itemStyle: { color },
    emphasis: { focus: "series" },
    data: [],
  });

  state.chart.setOption({
    animation: false,
    backgroundColor: "transparent",
    grid: { left: 46, right: 18, top: 34, bottom: 26 },
    legend: {
      top: 0,
      right: 4,
      itemWidth: 14,
      itemHeight: 8,
      textStyle: { color: "#7c8ca5", fontSize: 11 },
    },
    tooltip: {
      trigger: "axis",
      backgroundColor: "rgba(13,20,32,0.94)",
      borderColor: "#263248",
      textStyle: { color: "#e2e8f0", fontSize: 11.5 },
    },
    xAxis: {
      type: "time",
      axisLine: { lineStyle: { color: "#263248" } },
      axisLabel: { color: "#5b708f", fontSize: 10.5, hideOverlap: true },
      splitLine: { show: false },
    },
    yAxis: {
      type: "value",
      min: 0,
      max: HMAX,
      name: "mm",
      nameTextStyle: { color: "#4d6285", fontSize: 10 },
      axisLine: { show: false },
      axisLabel: { color: "#5b708f", fontSize: 10.5 },
      splitLine: { lineStyle: { color: "#1a2434" } },
    },
    series: [
      line("T1", "#38bdf8", false),
      line("T2", "#22c55e", false),
      line("T3", "#a78bfa", false),
      line("SP1", "#f59e0b", true),
      line("SP2", "#f472b6", true),
    ],
  });

  window.addEventListener("resize", () => state.chart && state.chart.resize());
}

function drawChart(sample) {
  if (!state.chart) return;
  const series = state.buffer.map((s) => [
    [s.ts * 1000, s.h1],
    [s.ts * 1000, s.h2],
    [s.ts * 1000, s.h3],
    [s.ts * 1000, s.sp1],
    [s.ts * 1000, s.sp2],
  ]);
  // 转置: series[i] 对应第 i 条线
  const data = [0, 1, 2, 3, 4].map((i) => series.map((row) => row[i]));
  state.chart.setOption({ series: data.map((d) => ({ data: d })) });
}

/* ------------------------------------------------------------------ 渲染 */
function clampLevel(v) {
  return Math.max(0, Math.min(HMAX, v));
}

function renderDiagram(d) {
  const levels = { h1: d.levels.h1, h2: d.levels.h2, h3: d.levels.h3 };
  for (const key of ["h1", "h2", "h3"]) {
    const t = TANKS[key];
    const v = clampLevel(levels[key]);
    const ratio = v / HMAX;
    const h = t.height * ratio;
    const y = t.top + t.height - h;

    const rect = $(t.water);
    rect.setAttribute("y", y.toFixed(1));
    rect.setAttribute("height", h.toFixed(1));

    const label = $(t.val);
    label.textContent = v;
    label.setAttribute("fill", v >= 320 ? "#ef4444" : "#f1f5f9");
  }

  // 设定值标记线
  const sp1 = clampLevel(d.sp.sp1);
  const y1 = 30 + 180 * (1 - sp1 / HMAX);
  const m1 = $(TANKS.h1.sp);
  m1.setAttribute("y1", y1.toFixed(1));
  m1.setAttribute("y2", y1.toFixed(1));

  const sp2 = clampLevel(d.sp.sp2);
  const y2 = 30 + 180 * (1 - sp2 / HMAX);
  const m2 = $(TANKS.h2.sp);
  m2.setAttribute("y1", y2.toFixed(1));
  m2.setAttribute("y2", y2.toFixed(1));

  // 泵状态
  const p1 = $("pump1");
  const p2 = $("pump2");
  p1.classList.toggle("running", d.flows.q1 > 0.05);
  p2.classList.toggle("running", d.flows.q2 > 0.05);
  $("flow-p1").textContent = d.flows.q1.toFixed(1) + " L/min";
  $("flow-p2").textContent = d.flows.q2.toFixed(1) + " L/min";
  $("flow-q3").textContent = "排入地沟 " + d.flows.q3.toFixed(1) + " L/min";
}

function renderReadouts(d) {
  $("ro-out1").textContent = d.pid.out1;
  $("ro-out2").textContent = d.pid.out2;
  $("ro-err1").textContent = d.pid.err1;
  $("ro-err2").textContent = d.pid.err2;
  $("ro-dec").textContent = d.pid.decouple;
  $("ro-scan").textContent = d.plc.scan_ms || 0;

  // 偏差着色
  for (const [id, val] of [["ro-err1", d.pid.err1], ["ro-err2", d.pid.err2]]) {
    const el = $(id);
    el.style.color = Math.abs(val) > 30 ? "#ef4444" : Math.abs(val) > 10 ? "#f59e0b" : "#22c55e";
  }
}

function renderStatus(d) {
  const conn = $("chip-conn");
  const online = d.plc && d.plc.online;
  conn.className = "chip " + (online ? "ok" : "bad");
  conn.querySelector("span").textContent = online ? "PLC 在线" : "PLC 离线";

  const mode = $("chip-mode");
  mode.className = "chip " + (d.mode === 1 ? "auto" : "");
  mode.querySelector("span").textContent = d.mode === 1 ? "自动 AUTO" : "手动 MAN";

  const alarmChip = $("chip-alarm");
  const n = d.alarms ? d.alarms.length : 0;
  alarmChip.className = "chip " + (n ? "bad" : "ok");
  alarmChip.querySelector("span").textContent = n ? n + " 条报警" : "无报警";
}

function syncControls(d) {
  // 模式下发: 自动时手动滑块失能, 显示 PLC 实际输出
  const auto = d.mode === 1;
  $("pump1").disabled = auto;
  $("pump2").disabled = auto;

  document.querySelectorAll("#mode-switch button").forEach((b) => {
    b.classList.toggle("active", Number(b.dataset.mode) === d.mode);
  });

  // 用户正在拖动时不要回写, 避免"跟手"打架
  if (Date.now() - state.userTouched > 1500) {
    const sp1 = $("sp1");
    const sp2 = $("sp2");
    sp1.value = d.sp.sp1;
    sp2.value = d.sp.sp2;
    $("sp1-out").textContent = d.sp.sp1 + " mm";
    $("sp2-out").textContent = d.sp.sp2 + " mm";
    if (!auto) {
      $("pump1").value = d.pumps.u1;
      $("pump2").value = d.pumps.u2;
      $("p1-out").textContent = (d.pumps.u1 / 10).toFixed(1) + " %";
      $("p2-out").textContent = (d.pumps.u2 / 10).toFixed(1) + " %";
    }
  }
}

function render(d) {
  state.latest = d;
  if (!d.levels) return;

  renderDiagram(d);
  renderReadouts(d);
  renderStatus(d);
  syncControls(d);

  const last = state.buffer[state.buffer.length - 1];
  if (!last || d.ts - last.ts >= 0.2) {
    state.buffer.push({
      ts: d.ts,
      h1: d.levels.h1,
      h2: d.levels.h2,
      h3: d.levels.h3,
      sp1: d.sp.sp1,
      sp2: d.sp.sp2,
    });
    const cutoff = Date.now() - BUFFER_MS;
    while (state.buffer.length && state.buffer[0].ts * 1000 < cutoff) state.buffer.shift();
  }

  const now = performance.now();
  if (now - state.lastChartDraw > 500) {
    state.lastChartDraw = now;
    drawChart();
  }
}

/* ------------------------------------------------------------------ 通信 */
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(proto + "://" + location.host + "/ws");
  state.ws = ws;

  ws.onmessage = (ev) => {
    try {
      render(JSON.parse(ev.data));
    } catch (e) {
      console.warn("数据解析失败", e);
    }
  };

  ws.onclose = () => {
    $("chip-conn").className = "chip bad";
    $("chip-conn").querySelector("span").textContent = "后台断开";
    setTimeout(connect, 2000);
  };

  ws.onerror = () => ws.close();
}

async function sendCommand(payload) {
  try {
    const r = await fetch("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!r.ok) {
      const txt = await r.text();
      console.warn("指令失败", txt);
    }
  } catch (e) {
    console.warn("指令发送异常", e);
  }
}

async function loadHistory() {
  try {
    const r = await fetch("/api/history?seconds=300");
    const rows = await r.json();
    state.buffer = rows.map((x) => ({
      ts: x.ts,
      h1: x.h1,
      h2: x.h2,
      h3: x.h3,
      sp1: x.sp1,
      sp2: x.sp2,
    }));
    drawChart();
  } catch (e) {
    console.warn("历史加载失败", e);
  }
}

async function loadAlarms() {
  try {
    const r = await fetch("/api/alarms?limit=40");
    const rows = await r.json();
    const ul = $("alarm-list");
    if (!rows.length) {
      ul.innerHTML = '<li class="empty">暂无报警</li>';
      return;
    }
    ul.innerHTML = rows
      .map((a) => {
        const t = new Date(a.ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
        const lvl = a.level === 1 ? "紧急" : "警告";
        return `<li class="lv${a.level}"><time>${t}</time><span class="msg">${a.message}</span><span class="lvl">${lvl}</span></li>`;
      })
      .join("");
  } catch (e) {
    console.warn("报警加载失败", e);
  }
}

/* ------------------------------------------------------------------ 交互 */
function bindControls() {
  document.querySelectorAll("#mode-switch button").forEach((btn) => {
    btn.onclick = () => {
      const mode = Number(btn.dataset.mode);
      document.querySelectorAll("#mode-switch button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      sendCommand({ mode });
    };
  });

  const bindSlider = (id, outId, transform, payloadKey) => {
    const el = $(id);
    const out = $(outId);
    el.addEventListener("input", () => {
      state.userTouched = Date.now();
      out.textContent = transform(Number(el.value));
    });
    el.addEventListener("change", () => {
      sendCommand({ [payloadKey]: Number(el.value) });
    });
  };

  bindSlider("sp1", "sp1-out", (v) => v + " mm", "sp1");
  bindSlider("sp2", "sp2-out", (v) => v + " mm", "sp2");
  bindSlider("pump1", "p1-out", (v) => (v / 10).toFixed(1) + " %", "pump1");
  bindSlider("pump2", "p2-out", (v) => (v / 10).toFixed(1) + " %", "pump2");

  // 初始一次回写, 之后交由服务器数据驱动
  setTimeout(() => {
    state.initialized = true;
  }, 1200);
}

/* ------------------------------------------------------------------ 启动 */
initChart();
bindControls();
loadHistory();
loadAlarms();
setInterval(loadAlarms, 5000);
connect();
