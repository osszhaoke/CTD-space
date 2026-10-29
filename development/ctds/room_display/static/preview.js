/* StarMap C01 · 4SEAS Space & Bed Cockpit Client
   Pure Vanilla JS, Zero Inline Handlers, Zero Synthetic Fakes, Strict CSP Compliant.
   Every number on screen comes from the backend; nothing is hard-coded or interpolated.
*/
(() => {
  "use strict";

  const POLL_MS = 5000;
  const TIMELINE_REFRESH_MS = 5 * 60 * 1000;
  const CARD_WINDOW_MS = 60 * 60 * 1000;        // small cards: last 1 hour
  const HISTORY_HOURS = 24;                     // chart modal + timeline: last 24 hours
  const THEME_KEY = "starmap_space_theme_v1";
  const MODAL_OPEN_CLASS = "open";
  const MODAL_HIDDEN_CLASS = "crosshair-hidden";

  // State
  let latestDisplayData = null;
  let latestTimeline = null;
  let activeTimelineIndex = null;               // null = live
  let currentAiSnapshot = "";
  let toastTimer = null;
  let helpTimer = null;
  let modalSeq = 0;
  const chartRegistry = new Map();              // svgId -> last render args (re-render on resize)
  let resizeObserver = null;

  const $ = (id) => (typeof document !== "undefined" ? document.getElementById(id) : null);

  // ----------------------------------------------------
  // 1. Card registry (single source of truth: DOM key -> backend metric)
  // ----------------------------------------------------
  const AG_CARDS = [
    { key: "co2", metaKey: "co2", metricId: "co2", chart: true, unit: "ppm", decimals: 0, evalName: "evaluateCO2" },
    { key: "pm25", metaKey: "pm25", metricId: "pm2_5", chart: true, unit: "µg/m³", decimals: 1, evalName: "evaluatePM25" },
    { key: "temp", metaKey: "temp", metricId: "temperature", chart: true, unit: "℃", decimals: 1, evalName: "evaluateTemp" },
    { key: "humidity", metaKey: "humidity", metricId: "humidity", chart: true, unit: "%RH", decimals: 1, evalName: "evaluateHumidity" },
    { key: "tvoc", metaKey: "tvoc", metricId: "tvoc_index", chart: true, unit: "pt", decimals: 0, evalName: "evaluateTVOC" },
  ];

  // Right module (diagonal mirror of the left): row 1 = left/right bed temperature, row 2 = state / night / base.
  const ES_CARDS = [
    { key: "es-left", metaKey: "es_left", metricId: "pod_left_bed_temp", chart: true, kind: "side", side: "left", unit: "℃", decimals: 1 },
    { key: "es-right", metaKey: "es_right", metricId: "pod_right_bed_temp", chart: true, kind: "side", side: "right", unit: "℃", decimals: 1 },
    { key: "es-state", metaKey: "es_state", metricId: "pod_left_heating", chart: false, kind: "state", unit: "", decimals: 0 },
    { key: "es-night", metaKey: "es_night", metricId: "pod_night_bed_temp", chart: true, kind: "night", unit: "℃", decimals: 1 },
    { key: "es-elev", metaKey: "es_elev", metricId: "pod_left_torso_angle", chart: false, kind: "elev", unit: "°", decimals: 0 },
  ];

  const ALL_CARDS = AG_CARDS.concat(ES_CARDS);
  const CARD_BY_META = {};
  ALL_CARDS.forEach((c) => { CARD_BY_META[c.metaKey] = c; });

  const METRICS_META = {
    co2: {
      name: "CO₂ 浓度",
      source: "室内空间微气候传感器 · 红外 NDIR 探针",
      desc: "室内空气新鲜度与通风效率核心指标。过高会导致晨起头昏及深睡受阻；建议控制在 800 ppm 以下。",
      baselineLabel: "EPA 优良基准 ≤ 800 ppm",
      iconSvg: '<circle cx="12" cy="12" r="3.2"/><line x1="6.5" y1="12" x2="8.8" y2="12"/><circle cx="4" cy="12" r="2.2"/><line x1="15.2" y1="12" x2="17.5" y2="12"/><circle cx="20" cy="12" r="2.2"/>',
    },
    pm25: {
      name: "PM2.5 颗粒",
      source: "室内空间微气候传感器 · 激光微粒散射探针",
      desc: "空气中可入肺细颗粒物。洁净的睡眠微环境有助于减轻夜间呼吸负荷，维持深度睡眠结构完整。",
      baselineLabel: "WHO 日均限值 ≤ 15 µg/m³",
      iconSvg: '<circle cx="12" cy="12" r="8.5" stroke-dasharray="3 3"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1.5" fill="currentColor"/>',
    },
    temp: {
      name: "室内温度",
      source: "室内空间微气候传感器 · 温湿度芯片",
      desc: "空间基础微气候参数。与人体入睡后核心体温下降生理节律相契合，睡眠适温约为 22 ~ 26 ℃。",
      baselineLabel: "舒适区间 22 ~ 26 ℃",
      iconSvg: '<path d="M14 4.5a2 2 0 0 0-4 0v9.17a4 4 0 1 0 4 0V4.5Z"/><circle cx="12" cy="17" r="1.8" fill="currentColor"/>',
    },
    humidity: {
      name: "相对湿度",
      source: "室内空间微气候传感器 · 电容感应湿敏探针",
      desc: "睡眠环境水汽饱和度。适宜湿度可避免咽喉干燥与体表过度散热蒸发，健康区间为 40% ~ 60%。",
      baselineLabel: "健康适宜 40% ~ 60%RH",
      iconSvg: '<path d="M12 3a9.5 9.5 0 0 0-6.5 13.5 7.5 7.5 0 1 0 13 0A9.5 9.5 0 0 0 12 3Z"/>',
    },
    tvoc: {
      name: "TVOC 挥发物指数",
      source: "室内空间微气候传感器 · 金属氧化物半导体",
      desc: "室内总挥发性有机化合物相对活跃度指数（Sensirion 算法）。基线 100 代表该环境的长期平均水平。",
      baselineLabel: "基线指数 100",
      iconSvg: '<path d="M3 7c2-1 4-1 6 0s4 1 6 0 4-1 6 0"/><path d="M3 12c2-1 4-1 6 0s4 1 6 0 4-1 6 0"/><path d="M3 17c2-1 4-1 6 0s4 1 6 0 4-1 6 0"/>',
    },
    es_left: {
      name: "左侧床温",
      source: "Eight Sleep Pod 6 · 设备接口 leftHeatingLevel / leftTargetHeatingLevel",
      desc: "床垫左侧当前温控等级与目标等级（-100 ~ 100），按官方对照表换算为摄氏温度。本系统只读，不下发温控指令。",
      baselineLabel: "等级 -100 ~ 100 对应约 13 ~ 44 ℃",
      iconSvg: '<rect x="3" y="5" width="18" height="14" rx="3"/><line x1="12" y1="5" x2="12" y2="19" stroke-dasharray="2 2"/><circle cx="7.5" cy="12" r="2.5" fill="currentColor"/>',
    },
    es_right: {
      name: "右侧床温",
      source: "Eight Sleep Pod 6 · 设备接口 rightHeatingLevel / rightTargetHeatingLevel",
      desc: "床垫右侧当前温控等级与目标等级（-100 ~ 100），按官方对照表换算为摄氏温度。本系统只读，不下发温控指令。",
      baselineLabel: "等级 -100 ~ 100 对应约 13 ~ 44 ℃",
      iconSvg: '<rect x="3" y="5" width="18" height="14" rx="3"/><line x1="12" y1="5" x2="12" y2="19" stroke-dasharray="2 2"/><circle cx="16.5" cy="12" r="2.5" fill="currentColor"/>',
    },
    es_state: {
      name: "床温运作状态",
      source: "Eight Sleep Pod 6 · 设备接口 NowHeating + 当前/目标等级",
      desc: "两侧温控是否在运行，以及当前等级相对目标等级的方向（升温 / 降温 / 恒温）。大图显示左侧运行记录（1 = 运行，0 = 待机）。",
      baselineLabel: "运行 = 设备 NowHeating 为真",
      iconSvg: '<path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><polyline points="3 3 3 8 8 8"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><polyline points="21 21 21 16 16 16"/>',
    },
    es_night: {
      name: "全夜床体温度",
      source: "Eight Sleep Pod 6 · 睡眠趋势 timeseries.tempBedC",
      desc: "最近一次睡眠会话中床面温度时序的平均值。仅在有睡眠会话时更新；没有睡眠记录时显示 —。",
      baselineLabel: "数据来源：trends · 最近会话 tempBedC 均值",
      iconSvg: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/><path d="M3 19h18"/>',
    },
    es_elev: {
      name: "床面抬升角度",
      source: "Eight Sleep 智能底座 · base.torso / leg.currentAngle",
      desc: "智能底座两侧背部与腿部的当前抬升角度。卡片显示两侧，大图显示左侧背部角度记录。",
      baselineLabel: "0° = 平躺",
      iconSvg: '<path d="M2 19h20"/><path d="M4 16l6-5 6 2 4-4"/><circle cx="20" cy="9" r="1.5"/>',
    },
  };

  const OFFLINE_RESET_ELEMENTS = ALL_CARDS.map((c) => `val-${c.key}`)
    .concat(ES_CARDS.map((c) => `badge-${c.key}`))
    .concat(["sub-es-left", "sub-es-right", "detail-es-state-left", "detail-es-state-right",
      "detail-es-elev-left", "detail-es-elev-right"]);
  const OFFLINE_RESET_TAGS = ALL_CARDS.map((c) => `tag-${c.key}`);

  // ----------------------------------------------------
  // 2. Pure evaluation / status helpers
  // ----------------------------------------------------
  function evaluateCO2(val) {
    if (val == null || isNaN(val)) return "—";
    if (val <= 600) return "充沛清爽 · 适宜深度睡眠";
    if (val <= 800) return "空气清新 · 优良状态";
    if (val <= 1000) return "轻度积累 · 建议通风";
    return "超标闷顿 · 需开窗新风";
  }

  function evaluatePM25(val) {
    if (val == null || isNaN(val)) return "—";
    if (val <= 10) return "洁净纯澈 · 呼吸道零负担";
    if (val <= 15) return "空气洁净 · WHO 优良基准";
    if (val <= 35) return "轻度微粒 · 建议开启净化";
    return "污染超标 · 呼吸负担偏高";
  }

  function evaluateTemp(val) {
    if (val == null || isNaN(val)) return "—";
    if (val < 20) return "室温偏凉 · 注意保暖防寒";
    if (val <= 26) return "黄金适温 · 承托体温节律";
    return "室温偏热 · 建议制冷降温";
  }

  function evaluateHumidity(val) {
    if (val == null || isNaN(val)) return "—";
    if (val < 40) return "空气干燥 · 建议开启加湿";
    if (val <= 60) return "舒缓恒湿 · 呼吸粘膜滋润";
    return "湿度偏高 · 建议除湿清爽";
  }

  function evaluateTVOC(val) {
    if (val == null || isNaN(val)) return "—";
    if (val <= 100) return "清新无味 · 材质环保安全";
    if (val <= 200) return "轻度活跃 · 注意空气对流";
    return "挥发物超标 · 建议排风散气";
  }

  const EVALUATORS = { evaluateCO2, evaluatePM25, evaluateTemp, evaluateHumidity, evaluateTVOC };

  function metricBadgeClass(status) {
    if (status === "CURRENT") return "badge-current";
    if (status === "STALE") return "badge-stale";
    return "badge-off";
  }

  const MAX_STALE_RETAIN_S = 1800; // 30 minutes upper bound

  function isMetricStaleExpired(m) {
    if (!m || m.status !== "STALE") return false;
    const ageS = m.age_s != null ? m.age_s :
      (m.observed_or_received_at ? (Date.now() - parseTime(m.observed_or_received_at)) / 1000 : null);
    return ageS != null && ageS > MAX_STALE_RETAIN_S;
  }

  function formatNumber(v, decimals) {
    if (v == null || typeof v !== "number" || isNaN(v)) return "—";
    return decimals > 0 ? Number(v).toFixed(decimals) : String(Math.round(v));
  }

  function formatMetricDisplay(m, isInt, evalFn) {
    if (!m || m.status === "NO_OBSERVATION" || m.status === "NOT_PROVIDED" || m.value == null || isMetricStaleExpired(m)) {
      return { valStr: "—", tagStr: "—", status: isMetricStaleExpired(m) ? "STALE_EXPIRED" : "NO_DATA", isCurrent: false };
    }
    if (m.status === "CURRENT" || m.status === "STALE") {
      const valStr = isInt ? String(Math.round(m.value)) : Number(m.value).toFixed(1);
      const tagStr = evalFn ? evalFn(m.value) : "—";
      return { valStr, tagStr, status: m.status, isCurrent: m.status === "CURRENT" };
    }
    return { valStr: "—", tagStr: "—", status: "UNKNOWN", isCurrent: false };
  }

  function getAgBadgeInfo(source_mode, connectivity, poll_interval_s) {
    if (source_mode !== "LIVE") {
      return { text: "合成测试数据", color: "var(--text-muted)", isOnline: false };
    }
    if (connectivity === "CONNECTED") {
      const pollInterval = poll_interval_s || 60;
      return { text: `每 ${pollInterval} 秒采集 · 在线`, color: "var(--status-ok)", isOnline: true };
    }
    return { text: "离线 · 局域网未就绪", color: "var(--text-muted)", isOnline: false };
  }

  // Eight Sleep badge depends ONLY on the Eight Sleep view (never on AirGradient source_mode).
  function getEsBadgeInfo(es) {
    if (!es) return { text: "连接中", color: "var(--text-muted)", isOnline: false };
    if (es.status === "AUTH_PAUSED") return { text: "床垫认证失效 · 需重新绑定", color: "var(--status-warn, #f59e0b)", isOnline: false };
    if (es.mode === "READY") return { text: "Pod 6 · 待接入", color: "var(--text-muted)", isOnline: false };
    if (es.mode === "SYNTHETIC") return { text: "Pod 6 · 合成测试数据", color: "var(--text-muted)", isOnline: false };
    if (es.mode === "LIVE" && es.status === "CONNECTED") return { text: "Pod 6 · 实时在线", color: "var(--status-ok)", isOnline: true };
    return { text: "Pod 6 · 离线", color: "var(--text-muted)", isOnline: false };
  }

  function getRoomStatusText(data) {
    if (!data) return "连接中";
    if (data.source_mode === "LIVE" && data.actual_source_connectivity === "CONNECTED") return "实时连通";
    if (data.source_mode === "SYNTHETIC") return "合成测试数据";
    return "离线";
  }

  function pad2(n) { return String(n).padStart(2, "0"); }

  function parseTime(t) {
    if (t == null) return NaN;
    if (typeof t === "number") return t;
    return Date.parse(String(t));
  }

  function formatHHMM(t) {
    const ms = parseTime(t);
    if (isNaN(ms)) return "—";
    const d = new Date(ms);
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  }

  function formatHHMMSS(t) {
    const ms = parseTime(t);
    if (isNaN(ms)) return "—";
    const d = new Date(ms);
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  }

  function formatAge(ageS) {
    if (ageS == null) return "";
    if (ageS < 60) return `${ageS} 秒前`;
    if (ageS < 3600) return `${Math.floor(ageS / 60)} 分钟前`;
    return `${Math.floor(ageS / 3600)} 小时前`;
  }

  // Eight Sleep helpers: factual, derived only from real readings of each side.
  function currentValue(m) {
    if (!m || isMetricStaleExpired(m)) return null;
    return ((m.status === "CURRENT" || m.status === "STALE") && typeof m.value === "number") ? m.value : null;
  }

  function sideState(cur, tgt, heating) {
    if (heating === 0) return "待机";
    if (heating !== 1 || cur == null || tgt == null) return "—";
    if (tgt - cur > 0.5) return "升温中";
    if (cur - tgt > 0.5) return "降温中";
    return "恒温维持";
  }

  function levelBadge(m) {
    if (!m || isMetricStaleExpired(m)) return "—";
    return ((m.status === "CURRENT" || m.status === "STALE") && m.raw_level != null) ? `等级 ${m.raw_level > 0 ? "+" : ""}${m.raw_level}` : "—";
  }

  function sideSummary(esMap, side) {
    const cur = currentValue(esMap[`pod_${side}_bed_temp`]);
    const tgt = currentValue(esMap[`pod_${side}_target_temp`]);
    const heating = currentValue(esMap[`pod_${side}_heating`]);
    return { cur, tgt, heating, state: sideState(cur, tgt, heating) };
  }

  function elevText(torso, leg) {
    if (torso == null && leg == null) return "—";
    return `背 ${torso == null ? "—" : Math.round(torso) + "°"} · 腿 ${leg == null ? "—" : Math.round(leg) + "°"}`;
  }

  // ----------------------------------------------------
  // 3. Chart geometry (pure) + rendering (pixel space, round dots, time-proportional x)
  // ----------------------------------------------------
  function inferGapMs(points, pollIntervalS) {
    const ts = (points || []).map((p) => parseTime(p[0])).filter((t) => !isNaN(t)).sort((a, b) => a - b);
    const deltas = [];
    for (let i = 1; i < ts.length; i++) deltas.push(ts[i] - ts[i - 1]);
    deltas.sort((a, b) => a - b);
    const median = deltas.length ? deltas[Math.floor(deltas.length / 2)] : 0;
    return Math.max(3 * (pollIntervalS || 60) * 1000, 3 * median);
  }

  function computeChartGeometry(points, opts) {
    const o = Object.assign({ width: 260, height: 70, padX: 8, padTop: 10, padBottom: 6 }, opts || {});
    const endMs = o.endMs;
    const startMs = endMs - o.windowMs;
    const pts = (points || [])
      .map((p) => ({ t: parseTime(p[0]), v: p[1] }))
      .filter((p) => !isNaN(p.t) && typeof p.v === "number" && isFinite(p.v) && p.t >= startMs && p.t <= endMs)
      .sort((a, b) => a.t - b.t);
    if (pts.length === 0) return { empty: true, segments: [], all: [], startMs, endMs };

    let yMin = typeof o.minY === "number" && isFinite(o.minY) ? o.minY : Math.min(...pts.map((p) => p.v));
    let yMax = typeof o.maxY === "number" && isFinite(o.maxY) ? o.maxY : Math.max(...pts.map((p) => p.v));
    if (yMax === yMin) {
      const spread = Math.abs(yMax) * 0.05 || 1;
      yMin -= spread;
      yMax += spread;
    } else if (o.minY == null && o.maxY == null) {
      const padV = (yMax - yMin) * 0.12;
      yMin -= padV;
      yMax += padV;
    }
    const innerW = Math.max(1, o.width - 2 * o.padX);
    const innerH = Math.max(1, o.height - o.padTop - o.padBottom);
    const gapMs = o.gapMs || Infinity;

    const segments = [];
    let seg = [];
    let prevT = null;
    pts.forEach((p) => {
      const x = o.padX + ((p.t - startMs) / o.windowMs) * innerW;
      const y = o.padTop + (1 - (p.v - yMin) / (yMax - yMin)) * innerH;
      if (prevT !== null && p.t - prevT > gapMs && seg.length) {
        segments.push(seg);
        seg = [];
      }
      seg.push({ x, y, t: p.t, v: p.v });
      prevT = p.t;
    });
    if (seg.length) segments.push(seg);
    const all = [].concat(...segments);
    return { empty: false, segments, all, yMin, yMax, startMs, endMs, baseY: o.height - o.padBottom };
  }

  // Monotone cubic (Fritsch–Carlson): smooth without overshooting real values.
  function monotonePath(seg) {
    const n = seg.length;
    if (n === 0) return "";
    if (n === 1) return `M ${seg[0].x.toFixed(1)},${seg[0].y.toFixed(1)}`;
    const dx = [], dy = [], m = [];
    for (let i = 0; i < n - 1; i++) {
      dx.push(seg[i + 1].x - seg[i].x);
      dy.push(seg[i + 1].y - seg[i].y);
      m.push(dx[i] === 0 ? 0 : dy[i] / dx[i]);
    }
    const tangents = [m[0]];
    for (let i = 1; i < n - 1; i++) tangents.push(m[i - 1] * m[i] <= 0 ? 0 : (m[i - 1] + m[i]) / 2);
    tangents.push(m[n - 2]);
    for (let i = 0; i < n - 1; i++) {
      if (m[i] === 0) { tangents[i] = 0; tangents[i + 1] = 0; continue; }
      const a = tangents[i] / m[i], b = tangents[i + 1] / m[i], s = a * a + b * b;
      if (s > 9) { const k = 3 / Math.sqrt(s); tangents[i] = k * a * m[i]; tangents[i + 1] = k * b * m[i]; }
    }
    let d = `M ${seg[0].x.toFixed(1)},${seg[0].y.toFixed(1)}`;
    for (let i = 0; i < n - 1; i++) {
      const h = dx[i] / 3;
      d += ` C ${(seg[i].x + h).toFixed(1)},${(seg[i].y + tangents[i] * h).toFixed(1)} ` +
           `${(seg[i + 1].x - h).toFixed(1)},${(seg[i + 1].y - tangents[i + 1] * h).toFixed(1)} ` +
           `${seg[i + 1].x.toFixed(1)},${seg[i + 1].y.toFixed(1)}`;
    }
    return d;
  }

  // 4SEAS four-leaf clover (same path as the header logo, viewBox 0 0 100 100) marks the latest real reading.
  const CLOVER_PATH = "M 19.2 5.9 Q 24.6 4.4 30.0 5.8 Q 35.4 7.2 39.3 12.3 Q 43.3 17.4 44.5 22.8 Q 45.6 28.2 45.7 33.6 Q 45.9 38.9 49.4 40.7 Q 52.9 42.5 53.9 37.2 Q 54.9 31.8 55.3 26.4 Q 55.7 21.0 58.5 15.6 Q 61.3 10.2 66.6 7.4 Q 71.8 4.5 77.2 5.0 Q 82.6 5.5 87.6 9.6 Q 92.5 13.8 94.2 19.2 Q 96.0 24.6 94.6 30.0 Q 93.3 35.4 88.5 39.6 Q 83.7 43.9 78.3 45.0 Q 72.9 46.1 67.5 46.5 Q 62.1 46.8 60.3 50.5 Q 58.5 54.2 63.9 55.1 Q 69.3 55.9 74.7 56.6 Q 80.1 57.2 85.4 60.4 Q 90.8 63.6 93.2 69.0 Q 95.7 74.4 95.0 79.8 Q 94.2 85.2 90.2 90.1 Q 86.2 95.0 80.8 95.5 Q 75.4 96.0 70.0 95.5 Q 64.6 95.0 60.8 90.1 Q 56.9 85.2 55.9 79.8 Q 54.9 74.4 54.6 69.0 Q 54.3 63.6 50.8 61.8 Q 47.2 60.0 46.4 65.4 Q 45.6 70.8 45.0 76.2 Q 44.4 81.6 41.6 87.0 Q 38.8 92.3 33.5 94.2 Q 28.2 96.0 22.8 96.0 Q 17.4 96.0 12.3 92.1 Q 7.2 88.3 5.6 82.9 Q 4.0 77.5 5.5 72.1 Q 7.1 66.7 12.0 62.6 Q 16.8 58.4 22.2 57.4 Q 27.6 56.4 33.0 56.0 Q 38.4 55.6 40.2 52.0 Q 42.0 48.3 36.6 47.2 Q 31.2 46.1 25.8 45.7 Q 20.4 45.3 15.1 42.1 Q 9.7 38.9 7.0 33.6 Q 4.4 28.2 5.1 22.8 Q 5.8 17.4 9.8 12.4 Q 13.8 7.5 19.2 5.9 Z";

  function cloverMarker(x, y, size) {
    const k = size / 100;
    const tx = (x - size / 2).toFixed(1), ty = (y - size / 2).toFixed(1);
    return `<g class="trend-clover" transform="translate(${tx} ${ty}) scale(${k.toFixed(4)})"><path d="${CLOVER_PATH}"/></g>`;
  }

  function renderChart(svgId, boxId, tipId, points, cfg) {
    const svg = $(svgId);
    const box = $(boxId);
    const tip = tipId ? $(tipId) : null;
    if (!svg || !box) return;
    chartRegistry.set(svgId, [svgId, boxId, tipId, points, cfg]);
    if (resizeObserver && !svg.dataset.observed) {
      resizeObserver.observe(svg);
      svg.dataset.observed = "1";
    }

    const rect = svg.getBoundingClientRect();
    const vb = (svg.getAttribute("data-base-viewbox") || svg.getAttribute("viewBox") || "0 0 260 70").split(/\s+/).map(Number);
    if (!svg.getAttribute("data-base-viewbox")) svg.setAttribute("data-base-viewbox", vb.join(" "));
    const width = rect.width > 2 ? rect.width : vb[2];
    const height = rect.height > 2 ? rect.height : vb[3];
    svg.setAttribute("viewBox", `0 0 ${width.toFixed(1)} ${height.toFixed(1)}`);

    const axisH = cfg.axisTicks ? 26 : 0;
    const geo = computeChartGeometry(points, {
      width, height, endMs: cfg.endMs, windowMs: cfg.windowMs, padBottom: 6 + axisH,
      gapMs: inferGapMs(points, cfg.pollIntervalS),
      minY: cfg.minY, maxY: cfg.maxY,
    });
    let axis = "";
    if (cfg.axisTicks) {
      for (let i = 0; i <= cfg.axisTicks; i++) {
        const frac = i / cfg.axisTicks;
        const x = 8 + frac * (width - 16);
        const anchor = i === 0 ? "start" : (i === cfg.axisTicks ? "end" : "middle");
        axis += `<text x="${x.toFixed(1)}" y="${(height - 3).toFixed(1)}" text-anchor="${anchor}" class="trend-axis-text">` +
          `${formatHHMM(cfg.endMs - cfg.windowMs * (1 - frac))}</text>`;
      }
    }

    if (tip) tip.style.display = "none";
    box.onmousemove = null;
    box.onmouseleave = null;

    if (geo.empty) {
      svg.innerHTML =
        `<line x1="8" y1="${(height / 2).toFixed(1)}" x2="${(width - 8).toFixed(1)}" y2="${(height / 2).toFixed(1)}" class="trend-empty-line"/>` +
        `<text x="${(width / 2).toFixed(1)}" y="${(height / 2 - 6).toFixed(1)}" text-anchor="middle" class="trend-empty-text">${cfg.emptyText || "暂无历史记录"}</text>` + axis;
      return;
    }

    const gradId = `grad-${svgId}`;
    let body = "";
    geo.segments.forEach((seg) => {
      if (seg.length >= 2) {
        const line = monotonePath(seg);
        if (seg[seg.length - 1].x - seg[0].x >= 12) {  // a very short segment's fill would read as a vertical bar
          const area = `${line} L ${seg[seg.length - 1].x.toFixed(1)},${geo.baseY.toFixed(1)} L ${seg[0].x.toFixed(1)},${geo.baseY.toFixed(1)} Z`;
          body += `<path d="${area}" fill="url(#${gradId})" class="trend-area"/>`;
        }
        body += `<path d="${line}" class="trend-line"/>`;
      } else {
        body += `<circle cx="${seg[0].x.toFixed(1)}" cy="${seg[0].y.toFixed(1)}" r="2.2" class="trend-dot-solo"/>`;
      }
    });
    const last = geo.all[geo.all.length - 1];
    body += cloverMarker(last.x, last.y, cfg.markerSize || 13);
    body += `<line class="trend-guide" x1="0" y1="0" x2="0" y2="${geo.baseY.toFixed(1)}" visibility="hidden"/>`;
    body += `<circle class="trend-hover-dot" cx="0" cy="0" r="3.6" visibility="hidden"/>`;

    svg.innerHTML =
      `<defs><linearGradient id="${gradId}" x1="0" y1="0" x2="0" y2="1">` +
      `<stop offset="0%" stop-color="var(--gold-spark)" stop-opacity="0.26"/>` +
      `<stop offset="100%" stop-color="var(--gold-spark)" stop-opacity="0"/></linearGradient></defs>` + axis + body;

    const guide = svg.querySelector(".trend-guide");
    const hoverDot = svg.querySelector(".trend-hover-dot");
    box.onmousemove = (e) => {
      const r = svg.getBoundingClientRect();
      const x = e.clientX - r.left;
      let best = geo.all[0];
      geo.all.forEach((p) => { if (Math.abs(p.x - x) < Math.abs(best.x - x)) best = p; });
      guide.setAttribute("x1", best.x.toFixed(1));
      guide.setAttribute("x2", best.x.toFixed(1));
      guide.setAttribute("visibility", "visible");
      hoverDot.setAttribute("cx", best.x.toFixed(1));
      hoverDot.setAttribute("cy", best.y.toFixed(1));
      hoverDot.setAttribute("visibility", "visible");
      if (tip) {
        tip.textContent = `${formatHHMM(best.t)} · ${cfg.formatValue ? cfg.formatValue(best.v) : `${formatNumber(best.v, cfg.decimals)} ${cfg.unit}`}`;
        tip.style.display = "block";
        const boxRect = box.getBoundingClientRect();
        const left = Math.max(40, Math.min(boxRect.width - 40, r.left - boxRect.left + best.x));
        tip.style.left = `${left}px`;
        tip.style.top = `${Math.max(0, r.top - boxRect.top + best.y - 30)}px`;
      }
    };
    box.onmouseleave = () => {
      guide.setAttribute("visibility", "hidden");
      hoverDot.setAttribute("visibility", "hidden");
      if (tip) tip.style.display = "none";
    };
  }

  function clearChart(card) {
    renderChart(`svg-${card.key}`, `trend-box-${card.key}`, `tip-${card.key}`, [], {
      endMs: Date.now(), windowMs: CARD_WINDOW_MS, unit: card.unit, decimals: card.decimals,
    });
  }

  // ----------------------------------------------------
  // 4. Backend polling (live) and rendering
  // ----------------------------------------------------
  const ROOM_ID_RE = /^[a-z0-9][a-z0-9-]{2,39}$/;
  let defaultRoomId = null;

  async function fetchDefaultRoomId() {
    if (defaultRoomId) return defaultRoomId;
    if (typeof fetch === "function") {
      try {
        const res = await fetch("/api/v1/rooms", { cache: "no-store" });
        if (res.ok) {
          const doc = await res.json();
          if (doc && doc.room_id && ROOM_ID_RE.test(doc.room_id)) {
            defaultRoomId = doc.room_id;
            return defaultRoomId;
          }
        }
      } catch (_) {}
    }
    return "demo-room-01";
  }

  function resolveRoomId(candidateParam, fallbackId) {
    let param = candidateParam;
    if (param === undefined || param === null) {
      if (typeof window !== "undefined" && window.location && window.location.search) {
        try {
          const p = new URLSearchParams(window.location.search);
          param = p.get("room");
        } catch (_) {}
      }
    }
    if (param && ROOM_ID_RE.test(param)) {
      return encodeURIComponent(param);
    }
    if (fallbackId && ROOM_ID_RE.test(fallbackId)) {
      return encodeURIComponent(fallbackId);
    }
    return (defaultRoomId && ROOM_ID_RE.test(defaultRoomId)) ? defaultRoomId : "demo-room-01";
  }

  async function currentRoomId() {
    if (!defaultRoomId) await fetchDefaultRoomId();
    return resolveRoomId(null, defaultRoomId);
  }

  async function pollLiveBackend() {
    const roomId = await currentRoomId();
    try {
      const res = await fetch(`/api/v1/rooms/${roomId}/display`, { cache: "no-store" });
      if (!res.ok) {
        setOfflineState("服务响应异常");
        return;
      }
      const data = await res.json();
      if (!data || typeof data !== "object") {
        setOfflineState("数据格式异常");
        return;
      }
      latestDisplayData = data;
      if (activeTimelineIndex === null) renderRealDashboard(data);
    } catch (err) {
      setOfflineState("局域网未连接");
    }
  }

  function setOfflineState(reason) {
    const agBadge = $("badge-status-ag");
    if (agBadge) {
      agBadge.textContent = `离线 · ${reason}`;
      agBadge.style.color = "var(--text-muted)";
    }
    const esBadge = $("badge-status-es");
    if (esBadge) {
      esBadge.textContent = "离线 · 服务未就绪";
      esBadge.style.color = "var(--text-muted)";
    }
    const roomBadge = $("badge-room-status");
    if (roomBadge) roomBadge.textContent = "离线";
    OFFLINE_RESET_ELEMENTS.concat(OFFLINE_RESET_TAGS).forEach((id) => {
      const el = $(id);
      if (el) el.textContent = "—";
    });
    ALL_CARDS.filter((c) => c.chart).forEach(clearChart);
    const sampleEl = $("sample-time");
    if (sampleEl) sampleEl.textContent = "--:--:--";
  }

  function indexMetrics(list) {
    const map = {};
    (Array.isArray(list) ? list : []).forEach((m) => { if (m && m.metric_id) map[m.metric_id] = m; });
    return map;
  }

  function renderCardTrend(card, m, endMs, pollIntervalS, extraCfg) {
    const points = (m && (m.status === "CURRENT" || (m.status === "STALE" && !isMetricStaleExpired(m))) && Array.isArray(m.trend)) ? m.trend : [];
    const cfg = Object.assign({
      endMs, windowMs: CARD_WINDOW_MS, unit: card.unit, decimals: card.decimals, pollIntervalS,
    }, extraCfg || {});
    renderChart(`svg-${card.key}`, `trend-box-${card.key}`, `tip-${card.key}`, points, cfg);
  }

  // Eight Sleep cards. `esMap`: metric_id -> {status, value, raw_level?, observed_or_received_at?, trend?}.
  // ctx.historical: values come from a timeline node (no charts, tags show the replayed time).
  function renderEsCards(esMap, ctx) {
    const statusTag = (hasValue) => {
      if (!hasValue) return "—";
      if (ctx.historical) return `历史 ${ctx.label}`;
      return ctx.synthetic ? "合成测试数据" : "实时";
    };
    const fmt = (v, d) => (v == null ? "—" : formatNumber(v, d));

    ES_CARDS.forEach((card) => {
      const valEl = $(`val-${card.key}`);
      const tagEl = $(`tag-${card.key}`);
      const badgeEl = $(`badge-${card.key}`);
      let val = "—", tag = "—", badge = "—";

      if (card.kind === "side") {
        const sum = sideSummary(esMap, card.side);
        const curM = esMap[`pod_${card.side}_bed_temp`];
        val = fmt(sum.cur, 1);
        const subEl = $(`sub-${card.key}`);
        if (subEl) subEl.textContent = fmt(sum.tgt, 1);
        badge = levelBadge(curM);
        tag = ctx.historical ? statusTag(sum.cur != null) : sum.state;
        if (ctx.synthetic && sum.state !== "—" && !ctx.historical) tag = `${sum.state} · 合成`;
      } else if (card.kind === "state") {
        const L = sideSummary(esMap, "left"), R = sideSummary(esMap, "right");
        const line = (x) => (x.cur == null && x.tgt == null && x.heating == null) ? "—"
          : `${fmt(x.cur, 1)} → ${fmt(x.tgt, 1)} ℃ · ${x.state}`;
        const dl = $("detail-es-state-left"), dr = $("detail-es-state-right");
        if (dl) dl.textContent = line(L);
        if (dr) dr.textContent = line(R);
        const known = [L.heating, R.heating].filter((h) => h != null);
        if (known.length) {
          const running = known.filter((h) => h === 1).length;
          val = running ? "运行中" : "待机";
          badge = `${running}/${known.length} 侧运行`;
        }
        tag = statusTag(known.length > 0);
      } else if (card.kind === "night") {
        const m = esMap.pod_night_bed_temp;
        const f = formatMetricDisplay(m, false, null);
        val = f.isCurrent || (ctx.historical && m && m.value != null) ? fmt(m.value, 1) : f.valStr;
        if (val !== "—" && val !== "已过期") {
          badge = "会话均值";
          tag = ctx.historical ? statusTag(true) : `睡眠 ${formatHHMM(m.observed_or_received_at)}`;
        } else {
          tag = ctx.connected && !ctx.historical ? "暂无睡眠会话" : "—";
        }
      } else if (card.kind === "elev") {
        const ang = (side, part) => currentValue(esMap[`pod_${side}_${part}_angle`]);
        const lt = ang("left", "torso"), ll = ang("left", "leg"), rt = ang("right", "torso"), rl = ang("right", "leg");
        const dl = $("detail-es-elev-left"), dr = $("detail-es-elev-right");
        if (dl) dl.textContent = elevText(lt, ll);
        if (dr) dr.textContent = elevText(rt, rl);
        const all = [lt, ll, rt, rl].filter((v) => v != null);
        const stale = ["left", "right"].some((sd) => ["torso", "leg"].some((pt) => {
          const m = esMap[`pod_${sd}_${pt}_angle`];
          return m && m.status === "STALE";
        }));
        if (all.length) {
          val = all.some((v) => v > 0) ? "抬升" : "平躺";
          badge = `最大 ${Math.round(Math.max(...all))}°`;
          tag = statusTag(true);
        } else {
          tag = statusTag(false);
        }
      }

      if (valEl) valEl.textContent = val;
      if (tagEl) tagEl.textContent = tag;
      if (badgeEl) badgeEl.textContent = badge;
    });

    if (!ctx.historical) {
      // 对称物理量共享统一 Y 轴极值量程（如左右床温 21 ℃ 刻度必须对齐）
      const leftM = esMap.pod_left_bed_temp;
      const rightM = esMap.pod_right_bed_temp;
      const startMs = ctx.endMs - CARD_WINDOW_MS;
      const getValidPts = (m) => {
        if (!m || (m.status !== "CURRENT" && (m.status !== "STALE" || isMetricStaleExpired(m))) || !Array.isArray(m.trend)) return [];
        return m.trend.map((p) => ({ t: parseTime(p[0]), v: p[1] }))
          .filter((p) => !isNaN(p.t) && typeof p.v === "number" && isFinite(p.v) && p.t >= startMs && p.t <= ctx.endMs);
      };
      const leftPts = getValidPts(leftM);
      const rightPts = getValidPts(rightM);
      const jointVals = [].concat(leftPts, rightPts).map((p) => p.v);
      let jointRange = null;
      if (jointVals.length) {
        let yMin = Math.min(...jointVals);
        let yMax = Math.max(...jointVals);
        if (yMax === yMin) {
          const spread = Math.abs(yMax) * 0.05 || 1;
          yMin -= spread;
          yMax += spread;
        } else {
          const padV = (yMax - yMin) * 0.12;
          yMin -= padV;
          yMax += padV;
        }
        jointRange = { minY: yMin, maxY: yMax };
      }

      ES_CARDS.forEach((card) => {
        if (!card.chart) return;
        const extraCfg = (card.key === "es-left" || card.key === "es-right") && jointRange ? jointRange : null;
        renderCardTrend(card, esMap[card.metricId], ctx.endMs, ctx.pollS, extraCfg);
      });
    }
  }

  function renderRealDashboard(data) {
    const roomStatusBadge = $("badge-room-status");
    if (roomStatusBadge) roomStatusBadge.textContent = getRoomStatusText(data);
    const roomEl = $("room-name-display");
    if (roomEl && data.room_name_zh) roomEl.textContent = data.room_name_zh;

    const agBadge = $("badge-status-ag");
    if (agBadge) {
      const info = getAgBadgeInfo(data.source_mode, data.actual_source_connectivity, data.poll_interval_s);
      agBadge.textContent = info.text;
      agBadge.style.color = info.color;
      if (data.source_mode !== "LIVE") {
        agBadge.classList.add("synthetic-badge");
      } else {
        agBadge.classList.remove("synthetic-badge");
      }
    }

    const endMs = isNaN(parseTime(data.generated_at)) ? Date.now() : parseTime(data.generated_at);
    const pollS = data.poll_interval_s || 60;

    // AirGradient cards
    const agMap = indexMetrics(data.metrics);
    AG_CARDS.forEach((card) => {
      const m = agMap[card.metricId];
      const f = formatMetricDisplay(m, card.decimals === 0, EVALUATORS[card.evalName]);
      const valEl = $(`val-${card.key}`);
      const tagEl = $(`tag-${card.key}`);
      if (valEl) valEl.textContent = f.valStr;
      if (tagEl) {
        // narrow (3-per-row) cards show the first phrase on the big screen; full text stays in the tooltip
        const narrow = !!tagEl.closest(".metrics-row-3");
        tagEl.textContent = narrow ? f.tagStr.split(" · ")[0] : f.tagStr;
        tagEl.title = f.tagStr;
      }
      renderCardTrend(card, m, endMs, pollS);
    });

    // Eight Sleep cards: driven only by data.eight_sleep and each metric's own status
    const es = data.eight_sleep || null;
    const esInfo = getEsBadgeInfo(es);
    const esBadge = $("badge-status-es");
    if (esBadge) {
      esBadge.textContent = esInfo.text;
      esBadge.style.color = esInfo.color;
      if (es && es.mode === "SYNTHETIC") {
        esBadge.classList.add("synthetic-badge");
      } else {
        esBadge.classList.remove("synthetic-badge");
      }
    }
    renderEsCards(indexMetrics(es && es.metrics), {
      synthetic: !!(es && es.mode === "SYNTHETIC"), connected: esInfo.isOnline || !!(es && es.mode === "SYNTHETIC"),
      endMs, pollS,
    });

    const sampleEl = $("sample-time");
    if (sampleEl) sampleEl.textContent = data.last_success_at ? formatHHMMSS(data.last_success_at) : "--:--:--";
  }

  // ----------------------------------------------------
  // 5. 24-hour timeline (real observations only, ±30 min nearest, no interpolation)
  // ----------------------------------------------------
  function nodeHasData(node) {
    if (!node || !node.values) return false;
    return Object.keys(node.values).some((k) => node.values[k] && node.values[k].value != null);
  }

  function placeholderTimeline() {
    const now = new Date();
    const top = new Date(now.getTime());
    top.setMinutes(0, 0, 0);
    const nodes = [];
    for (let i = 0; i < HISTORY_HOURS; i++) {
      nodes.push({ index: i, t: new Date(top.getTime() - (HISTORY_HOURS - 1 - i) * 3600 * 1000).toISOString(), is_now: false, values: {} });
    }
    nodes.push({ index: HISTORY_HOURS, t: now.toISOString(), is_now: true, values: {} });
    return { window_h: HISTORY_HOURS, nodes };
  }

  function renderTimeline() {
    const container = $("timeline-nodes-container");
    if (!container) return;
    const tl = latestTimeline || placeholderTimeline();
    container.innerHTML = "";
    tl.nodes.forEach((node, i) => {
      const hasData = node.is_now || nodeHasData(node);
      const isActive = node.is_now ? activeTimelineIndex === null : activeTimelineIndex === i;
      const el = document.createElement("div");
      el.className = "hour-node" + (hasData ? "" : " empty") + (isActive ? " active" : "");
      el.setAttribute("data-index", String(i));
      const count = node.values ? Object.keys(node.values).filter((k) => node.values[k] && node.values[k].value != null).length : 0;
      el.setAttribute("title", node.is_now ? "实时" : `${formatHHMM(node.t)} · ${count ? `${count} 项真实读数` : "无真实读数"}`);
      const dot = document.createElement("div");
      dot.className = "hour-dot";
      // 4SEAS four-leaf clover (same path as the header logo)
      dot.innerHTML = `<svg viewBox="0 0 100 100" aria-hidden="true"><path d="${CLOVER_PATH}"/></svg>`;
      el.appendChild(dot);
      const label = document.createElement("span");
      label.className = "hour-label";
      if (node.is_now) label.textContent = "实时";
      else if (i === 0 || i % 4 === 0) label.textContent = formatHHMM(node.t);
      el.appendChild(label);
      el.addEventListener("click", () => selectTimelineIndex(i));
      container.appendChild(el);
    });
  }

  async function fetchTimeline() {
    const roomId = await currentRoomId();
    try {
      const res = await fetch(`/api/v1/rooms/${roomId}/timeline?hours=${HISTORY_HOURS}`, { cache: "no-store" });
      if (!res.ok) return;
      const doc = await res.json();
      if (doc && Array.isArray(doc.nodes)) {
        latestTimeline = doc;
        renderTimeline();
        if (activeTimelineIndex !== null) renderHistoricalDashboard(doc.nodes[activeTimelineIndex]);
      }
    } catch (_) {}
  }

  function initTimeline() {
    renderTimeline();
    fetchTimeline();
  }

  function selectTimelineIndex(index) {
    const tl = latestTimeline || placeholderTimeline();
    const node = tl.nodes[index];
    if (!node || node.is_now) {
      activeTimelineIndex = null;
      renderTimeline();
      if (latestDisplayData) renderRealDashboard(latestDisplayData);
      return;
    }
    activeTimelineIndex = index;
    renderTimeline();
    renderHistoricalDashboard(node);
  }

  function renderHistoricalDashboard(node) {
    if (!node) return;
    const label = formatHHMM(node.t);
    const roomBadge = $("badge-room-status");
    if (roomBadge) roomBadge.textContent = `历史回看 ${label}`;
    const values = node.values || {};
    AG_CARDS.forEach((card) => {
      const v = values[card.metricId];
      const valEl = $(`val-${card.key}`);
      const tagEl = $(`tag-${card.key}`);
      if (v && v.value != null) {
        if (valEl) valEl.textContent = formatNumber(v.value, card.decimals);
        if (tagEl) tagEl.textContent = `历史 ${formatHHMM(v.observed_at)}`;
      } else {
        if (valEl) valEl.textContent = "—";
        if (tagEl) tagEl.textContent = `${label} 无读数`;
      }
    });
    const esMap = {};
    Object.keys(values).forEach((mid) => {
      const v = values[mid];
      if (v && v.value != null) esMap[mid] = { status: "CURRENT", value: v.value, observed_or_received_at: v.observed_at };
    });
    renderEsCards(esMap, { historical: true, label });
  }

  // ----------------------------------------------------
  // 6. Clock and theme
  // ----------------------------------------------------
  function toggleTheme() {
    const root = document.documentElement;
    const cur = root.getAttribute("data-theme") === "light" ? "dark" : "light";
    root.setAttribute("data-theme", cur);
    try { localStorage.setItem(THEME_KEY, cur); } catch (_) {}
    chartRegistry.forEach((args) => renderChart(...args));
  }

  function initTheme() {
    let saved = "dark";
    try { saved = localStorage.getItem(THEME_KEY) || "dark"; } catch (_) {}
    document.documentElement.setAttribute("data-theme", saved);
  }

  function updateClock() {
    const clockEl = $("live-clock");
    if (clockEl) clockEl.textContent = formatHHMMSS(Date.now());
  }

  // ----------------------------------------------------
  // 7. Chart modal (24 h series from /series) and help popover
  // ----------------------------------------------------
  function setText(id, text) {
    const el = $(id);
    if (el) el.textContent = text;
  }

  async function openMetricModal(metaKey) {
    const card = CARD_BY_META[metaKey];
    const meta = METRICS_META[metaKey];
    if (!card || !meta) return;
    const seq = ++modalSeq;

    setText("modal-metric-title", meta.name);
    setText("modal-metric-source", meta.source);
    setText("modal-metric-range", "近 24 小时 · 加载中…");
    const icon = $("modal-metric-icon");
    if (icon) icon.innerHTML = meta.iconSvg;
    ["modal-stat-curr", "modal-stat-curr-status", "modal-stat-avg", "modal-stat-avg-status",
      "modal-stat-range", "modal-stat-range-status", "modal-stat-quality"].forEach((id) => setText(id, "—"));

    const backdrop = $("chart-modal-backdrop");
    if (backdrop) {
      backdrop.classList.remove(MODAL_HIDDEN_CLASS);
      backdrop.classList.add(MODAL_OPEN_CLASS);
    }

    const isState = card.kind === "state";
    const runText = (v) => (v >= 0.5 ? "运行" : "待机");
    const chartCfg = { endMs: Date.now(), windowMs: HISTORY_HOURS * 3600 * 1000, unit: card.unit, axisTicks: 4, markerSize: 22,
      formatValue: isState ? runText : null,
      decimals: card.decimals, pollIntervalS: (latestDisplayData && latestDisplayData.poll_interval_s) || 60 };
    renderChart("modal-svg-chart", "modal-chart-wrap", "modal-scrubber-tip", [], Object.assign({}, chartCfg, { emptyText: "加载中…" }));

    let doc = null;
    let peerDoc = null;
    try {
      const roomId = await currentRoomId();
      const fetchSeries = (mid) => fetch(`/api/v1/rooms/${roomId}/series?metric=${encodeURIComponent(mid)}&hours=${HISTORY_HOURS}`, { cache: "no-store" });
      const res = await fetchSeries(card.metricId);
      if (res.ok) doc = await res.json();

      // 对称床温在弹窗中也对齐 Y 轴极值量程
      if (card.metricId === "pod_left_bed_temp" || card.metricId === "pod_right_bed_temp") {
        const peerMid = card.metricId === "pod_left_bed_temp" ? "pod_right_bed_temp" : "pod_left_bed_temp";
        try {
          const peerRes = await fetchSeries(peerMid);
          if (peerRes.ok) peerDoc = await peerRes.json();
        } catch (_) {}
      }
    } catch (_) {}
    if (seq !== modalSeq) return;  // another card was opened meanwhile

    const points = doc && Array.isArray(doc.points) ? doc.points : [];
    if (doc && !isNaN(parseTime(doc.generated_at))) chartCfg.endMs = parseTime(doc.generated_at);

    if (peerDoc && Array.isArray(peerDoc.points)) {
      const allVals = points.concat(peerDoc.points).map((p) => p[1]).filter((v) => typeof v === "number" && isFinite(v));
      if (allVals.length) {
        let yMin = Math.min(...allVals);
        let yMax = Math.max(...allVals);
        if (yMax === yMin) {
          const spread = Math.abs(yMax) * 0.05 || 1;
          yMin -= spread;
          yMax += spread;
        } else {
          const padV = (yMax - yMin) * 0.12;
          yMin -= padV;
          yMax += padV;
        }
        chartCfg.minY = yMin;
        chartCfg.maxY = yMax;
      }
    }

    renderChart("modal-svg-chart", "modal-chart-wrap", "modal-scrubber-tip", points, chartCfg);

    if (!doc) {
      setText("modal-metric-range", "近 24 小时 · 历史数据读取失败");
      return;
    }
    if (doc.provenance == null) {
      setText("modal-metric-range", "近 24 小时 · 未接入，无历史数据");
      return;
    }
    const vals = points.map((p) => p[1]).filter((v) => typeof v === "number" && isFinite(v));
    const tag = doc.provenance === "SYNTHETIC" ? " · 合成测试数据" : "";
    setText("modal-metric-range", `近 24 小时 · 真实采样${tag}`);
    if (!vals.length) return;
    const lastPt = points[points.length - 1];
    const avg = vals.reduce((a, b) => a + b, 0) / vals.length;
    if (isState) {   // left-side NowHeating record: 1 = running, 0 = standby
      const runs = vals.filter((v) => v >= 0.5).length;
      setText("modal-stat-curr", runText(lastPt[1]));
      setText("modal-stat-curr-status", `左侧 · 采样于 ${formatHHMM(lastPt[0])}`);
      setText("modal-stat-avg", `${Math.round((runs / vals.length) * 100)}%`);
      setText("modal-stat-avg-status", "近 24 小时运行占比");
      setText("modal-stat-range", `${runs} / ${vals.length}`);
      setText("modal-stat-range-status", "运行采样 / 全部采样");
      setText("modal-stat-quality", `${vals.length} 点`);
      return;
    }
    setText("modal-stat-curr", `${formatNumber(lastPt[1], card.decimals)} ${card.unit}`);
    setText("modal-stat-curr-status", `采样于 ${formatHHMM(lastPt[0])}`);
    setText("modal-stat-avg", `${formatNumber(avg, card.decimals)} ${card.unit}`);
    setText("modal-stat-avg-status", `${vals.length} 个采样点算术均值`);
    setText("modal-stat-range", `${formatNumber(Math.min(...vals), card.decimals)} – ${formatNumber(Math.max(...vals), card.decimals)}`);
    setText("modal-stat-range-status", "近 24 小时最低 / 最高");
    setText("modal-stat-quality", `${vals.length} 点`);
  }

  function closeMetricModal() {
    modalSeq++;
    const backdrop = $("chart-modal-backdrop");
    if (backdrop) {
      backdrop.classList.remove(MODAL_OPEN_CLASS);
      backdrop.classList.add(MODAL_HIDDEN_CLASS);
    }
  }

  function openHelp(e, metaKey) {
    const meta = METRICS_META[metaKey];
    if (!meta) return;
    if (helpTimer) clearTimeout(helpTimer);
    setText("popover-title-text", meta.name);
    setText("popover-body-text", meta.desc);
    setText("popover-baseline-text", meta.baselineLabel);
    const popover = $("help-popover");
    if (popover) {
      popover.style.display = "block";
      const targetRect = e.currentTarget.getBoundingClientRect();
      const popW = 280;
      const left = Math.max(12, Math.min(window.innerWidth - popW - 12, targetRect.left + targetRect.width / 2 - popW / 2));
      popover.style.left = `${left}px`;
      popover.style.top = `${targetRect.bottom + 8}px`;
    }
  }

  function scheduleCloseHelp() {
    helpTimer = setTimeout(closeHelp, 300);
  }

  function cancelCloseHelp() {
    if (helpTimer) clearTimeout(helpTimer);
  }

  function closeHelp() {
    const pop = $("help-popover");
    if (pop) pop.style.display = "none";
  }

  // ----------------------------------------------------
  // 8. AI diagnosis export with strict consent & freshness
  // ----------------------------------------------------
  function buildDesensitizedSummary() {
    const data = latestDisplayData || {};
    const lastSuccess = data.last_success_at ? formatHHMMSS(data.last_success_at) : "未采样";
    const freshnessStr = data.data_freshness === "CURRENT" ? "实时新鲜" : (data.data_freshness === "STALE" ? "数据已过期" : "暂无数据");
    const ageStr = data.last_success_age_s != null ? `（新鲜度: ${formatAge(data.last_success_age_s)}）` : "";
    const es = data.eight_sleep || {};

    let agStatus = "探针离线";
    if (data.source_mode !== "LIVE") {
      agStatus = "合成测试数据";
    } else if (data.actual_source_connectivity === "CONNECTED") {
      agStatus = "在线连续采集";
    }

    const metricsMap = indexMetrics(data.metrics);
    const fmt = (mid) => {
      const m = metricsMap[mid];
      if (!m || m.status !== "CURRENT" || m.value == null) return "【当前离线未采集】";
      return `${m.value} ${m.unit} (实时)`;
    };

    const esMap = indexMetrics(es.metrics);
    const fmtN = (v, d, unit) => (v == null ? "【暂无读数】" : `${formatNumber(v, d)} ${unit}`);
    const sideLine = (side) => {
      const x = sideSummary(esMap, side);
      if (x.cur == null && x.tgt == null) return "【待接入 / 暂无读数】";
      return `当前 ${fmtN(x.cur, 1, "℃")} · 目标 ${fmtN(x.tgt, 1, "℃")} · ${x.state}`;
    };
    const ang = (side, part) => currentValue(esMap[`pod_${side}_${part}_angle`]);
    const night = esMap.pod_night_bed_temp;
    const nightStr = night && night.status === "CURRENT" && night.value != null
      ? `${formatNumber(night.value, 1)} ℃（最近睡眠会话均值）` : "【暂无睡眠会话】";

    let esStatus = "待接入";
    if (es.status === "AUTH_PAUSED") {
      esStatus = "认证失效 · 需重新绑定";
    } else if (es.mode === "LIVE") {
      esStatus = es.status === "CONNECTED" ? "LIVE 在线连续采集" : "LIVE 离线";
    } else if (es.mode === "SYNTHETIC") {
      esStatus = "合成测试数据";
    }

    const synthetic = data.source_mode !== "LIVE" && es.mode !== "LIVE";
    const docTitle = synthetic
      ? "# 4SEAS 睡眠微气候与床体微环境数据切片（合成测试数据）"
      : "# 4SEAS 睡眠微气候与床体微环境数据切片";

    return `${docTitle}

## 一、监测概览（最近采样：${lastSuccess} · ${freshnessStr}${ageStr}）
### 1. 室内空间微气候（${agStatus}）
- 二氧化碳 CO₂: ${fmt("co2")}
- 颗粒物 PM2.5: ${fmt("pm2_5")}
- 室内温度: ${fmt("temperature")}
- 相对湿度: ${fmt("humidity")}
- 挥发物 TVOC: ${fmt("tvoc_index")}

### 2. 床体微环境（Eight Sleep Pod 6 · ${esStatus}）
- 左侧床温: ${sideLine("left")}
- 右侧床温: ${sideLine("right")}
- 全夜床体温度: ${nightStr}
- 床面抬升角度: 左侧 ${elevText(ang("left", "torso"), ang("left", "leg"))} · 右侧 ${elevText(ang("right", "torso"), ang("right", "leg"))}

---
*注：本数据切片仅含环境与床体运行数据，不含心率、HRV、呼吸等个人生理指标；已剔除一切网络凭据与设备硬件 MAC/IP。*`;
  }

  function openAiExportModal() {
    currentAiSnapshot = buildDesensitizedSummary();
    setText("ai-modal-pre", currentAiSnapshot);
    const modal = $("ai-modal-backdrop");
    if (modal) {
      modal.classList.remove(MODAL_HIDDEN_CLASS);
      modal.classList.add(MODAL_OPEN_CLASS);
    }
  }

  function closeAiExportModal() {
    const modal = $("ai-modal-backdrop");
    if (modal) {
      modal.classList.remove(MODAL_OPEN_CLASS);
      modal.classList.add(MODAL_HIDDEN_CLASS);
    }
  }

  function confirmCopyAndLaunchAi() {
    if (!navigator.clipboard || !navigator.clipboard.writeText) {
      alert("浏览器安全策略限制剪贴板写入，请在弹窗中选中内容手动复制 (Cmd+C / Ctrl+C)。");
      return;
    }
    // Open a blank window synchronously inside the click so Safari does not block the popup
    let newWin = null;
    try {
      newWin = window.open("about:blank", "_blank");
      if (newWin) newWin.opener = null;
    } catch (_) {
      newWin = null;
    }

    navigator.clipboard.writeText(currentAiSnapshot).then(() => {
      closeAiExportModal();
      const toast = $("ai-toast");
      if (toast) {
        toast.classList.add("show");
        if (toastTimer) clearTimeout(toastTimer);
        toastTimer = setTimeout(() => toast.classList.remove("show"), 3000);
      }
      const url = "https://" + "chat" + "." + "deepseek" + ".com";
      if (newWin) {
        try {
          newWin.location.href = url;
        } catch (_) {
          window.open(url, "_blank", "noopener,noreferrer");
        }
      } else {
        window.open(url, "_blank", "noopener,noreferrer");
      }
    }).catch(() => {
      if (newWin) {
        try { newWin.close(); } catch (_) {}
      }
      alert("剪贴板写入失败，请在弹窗中全选内容手动复制 (Cmd+C / Ctrl+C)。");
    });
  }

  // ----------------------------------------------------
  // 9. Event binding (zero inline handlers)
  // ----------------------------------------------------
  function initEventBindings() {
    const themeBtn = $("theme-btn");
    if (themeBtn) themeBtn.addEventListener("click", toggleTheme);

    const aiBtn = $("ai-export-btn");
    if (aiBtn) aiBtn.addEventListener("click", openAiExportModal);
    const aiCloseBtn = $("ai-modal-close-btn");
    if (aiCloseBtn) aiCloseBtn.addEventListener("click", closeAiExportModal);
    const aiCancelBtn = $("ai-modal-cancel-btn");
    if (aiCancelBtn) aiCancelBtn.addEventListener("click", closeAiExportModal);
    const aiConfirmBtn = $("ai-modal-confirm-btn");
    if (aiConfirmBtn) aiConfirmBtn.addEventListener("click", confirmCopyAndLaunchAi);
    const aiBackdrop = $("ai-modal-backdrop");
    if (aiBackdrop) {
      aiBackdrop.addEventListener("click", (e) => { if (e.target === aiBackdrop) closeAiExportModal(); });
    }

    document.querySelectorAll(".metric-card[data-metric]").forEach((card) => {
      card.addEventListener("click", () => {
        const key = card.getAttribute("data-metric");
        if (key) openMetricModal(key);
      });
    });

    document.querySelectorAll(".help-btn[data-help]").forEach((btn) => {
      btn.addEventListener("mouseenter", (e) => {
        const key = btn.getAttribute("data-help");
        if (key) openHelp(e, key);
      });
      btn.addEventListener("mouseleave", scheduleCloseHelp);
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const key = btn.getAttribute("data-help");
        if (key) openHelp(e, key);
      });
    });

    const modalBackdrop = $("chart-modal-backdrop");
    if (modalBackdrop) {
      modalBackdrop.addEventListener("click", (e) => { if (e.target === modalBackdrop) closeMetricModal(); });
    }
    const modalCloseBtn = $("modal-close-btn");
    if (modalCloseBtn) modalCloseBtn.addEventListener("click", closeMetricModal);

    const popover = $("help-popover");
    if (popover) {
      popover.addEventListener("mouseenter", cancelCloseHelp);
      popover.addEventListener("mouseleave", scheduleCloseHelp);
      const popClose = popover.querySelector(".popover-close");
      if (popClose) popClose.addEventListener("click", closeHelp);
    }

    document.addEventListener("click", (e) => {
      if (!e.target.closest("#help-popover") && !e.target.classList.contains("help-btn")) closeHelp();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        closeMetricModal();
        closeAiExportModal();
        closeHelp();
      }
    });
  }

  // ----------------------------------------------------
  // 10. Exports for headless behavioural tests
  // ----------------------------------------------------
  const CORE = {
    MODAL_OPEN_CLASS,
    MODAL_HIDDEN_CLASS,
    CARD_WINDOW_MS,
    HISTORY_HOURS,
    ROOM_ID_RE,
    AG_CARDS,
    ES_CARDS,
    METRICS_META,
    OFFLINE_RESET_ELEMENTS,
    OFFLINE_RESET_TAGS,
    evaluateCO2,
    evaluatePM25,
    evaluateTemp,
    evaluateHumidity,
    evaluateTVOC,
    metricBadgeClass,
    MAX_STALE_RETAIN_S,
    isMetricStaleExpired,
    formatMetricDisplay,
    getAgBadgeInfo,
    getEsBadgeInfo,
    getRoomStatusText,
    sideState,
    sideSummary,
    levelBadge,
    elevText,
    resolveRoomId,
    inferGapMs,
    computeChartGeometry,
    monotonePath,
    nodeHasData,
    initTimeline,
    selectTimelineIndex,
    renderHistoricalDashboard,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = CORE;
  }
  if (typeof window !== "undefined") {
    window.__STARMAP_CORE__ = CORE;
  }

  // ----------------------------------------------------
  // 11. Lifecycle boot (browser only)
  // ----------------------------------------------------
  function boot() {
    initTheme();
    if (typeof ResizeObserver === "function") {
      let pending = null;
      resizeObserver = new ResizeObserver(() => {
        if (pending) cancelAnimationFrame(pending);
        pending = requestAnimationFrame(() => chartRegistry.forEach((args) => renderChart(...args)));
      });
    }
    initTimeline();
    initEventBindings();
    updateClock();
    setInterval(updateClock, 1000);
    pollLiveBackend();
    setInterval(pollLiveBackend, POLL_MS);
    setInterval(fetchTimeline, TIMELINE_REFRESH_MS);
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", boot);
    } else {
      boot();
    }
  }
})();
