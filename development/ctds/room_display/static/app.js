/* StarMap C01 · Production Astra Single-Page Cockpit Client
   Day/Night theme switching; interactive 11:50–12:00 timeline scrubbing; frosted glass; icon-first presentation. */
(() => {
  "use strict";

  const POLL_MS = 5000;
  const PRIMARY_IDS = new Set(["pm2_5", "co2", "temperature", "humidity", "tvoc_index"]);
  const SECONDARY_IDS = new Set(["pm1", "pm10", "nox_index"]);
  const THEME_KEY = "starmap_space_theme_v1";
  let roomId = "demo-room-01";
  let latestData = null;
  let pinnedIdx = null;
  let hoverIdx = null;
  let trailSignature = "";

  const $ = (id) => document.getElementById(id);

  function esc(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /* Bespoke Lucide 1.5-stroke vector icons */
  const ICONS = {
    pm2_5: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v3M12 18v3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M3 12h3M18 12h3"/><circle cx="12" cy="12" r="2.5"/></svg>',
    co2: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17.7 7.7a2.5 2.5 0 1 1 1.8 4.3H2"/><path d="M9.6 4.6A2 2 0 1 1 11 8H2"/><path d="M12.6 19.4A2 2 0 1 0 14 16H2"/></svg>',
    temperature: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4v10.54a4 4 0 1 1-4 0V4a2 2 0 0 1 4 0Z"/></svg>',
    humidity: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 16.3c2.2 0 4-1.83 4-4.05 0-1.16-.57-2.26-1.71-3.19S7.29 6.75 7 5.3c-.29 1.45-1.14 2.84-2.29 3.76S3 11.1 3 12.25c0 2.22 1.8 4.05 4 4.05Z"/><path d="M12.56 6.6A10.97 10.97 0 0 0 14 3.02c.5 2.5 2 4.9 4 6.5s3 3.5 3 5.5a6.98 6.98 0 0 1-11.91 4.97"/></svg>',
    tvoc_index: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 6c.6.5 1.2 1 2.5 1C7 7 7 5 9.5 5c2.6 0 2.4 2 5 2 2.5 0 2.5-2 5-2 1.3 0 1.9.5 2.5 1"/><path d="M2 12c.6.5 1.2 1 2.5 1 2.5 0 2.5-2 5-2 2.6 0 2.4 2 5 2 2.5 0 2.5-2 5-2 1.3 0 1.9.5 2.5 1"/><path d="M2 18c.6.5 1.2 1 2.5 1 2.5 0 2.5-2 5-2 2.6 0 2.4 2 5 2 2.5 0 2.5-2 5-2 1.3 0 1.9.5 2.5 1"/></svg>',
    pm1: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9" stroke-dasharray="3 3"/><circle cx="12" cy="12" r="2.5"/></svg>',
    pm10: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"/></svg>',
    nox_index: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5Z"/></svg>',
    wifi: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h.01"/><path d="M2 8.82a15 15 0 0 1 20 0"/><path d="M5 12.859a10 10 0 0 1 14 0"/><path d="M8.5 16.429a5 5 0 0 1 7 0"/></svg>',
    unplug: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m19 5 3-3"/><path d="m2 22 3-3"/><path d="M6.3 20.3a2.4 2.4 0 0 0 3.4 0L12 18l-6-6-2.3 2.3a2.4 2.4 0 0 0 0 3.4Z"/><path d="M7.5 13.5 10 11"/><path d="M10.5 16.5 13 14"/><path d="m12 6 6 6 2.3-2.3a2.4 2.4 0 0 0 0-3.4l-2.6-2.6a2.4 2.4 0 0 0-3.4 0Z"/></svg>',
    range: '<svg viewBox="0 0 24 24" aria-hidden="true"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>',
    clock: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg>',
    pod_heart_rate: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"/></svg>',
    pod_hrv: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>',
    pod_respiratory_rate: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v16"/><path d="M8 8c-3 0-5 2.5-5 6s2 5 5 5h2V8Z"/><path d="M16 8c3 0 5 2.5 5 6s-2 5-5 5h-2V8Z"/></svg>',
    pod_bed_temp: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 4v16"/><path d="M2 8h18a2 2 0 0 1 2 2v10"/><path d="M2 17h20"/><path d="M6 8v9"/></svg>',
    pod_target_bed_temp: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.5"/></svg>',
    pod_room_temp: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/><path d="M12 11v5"/><circle cx="12" cy="17" r="1"/></svg>',
  };

  function metricIcon(mid) {
    return ICONS[mid] || ICONS.pm2_5;
  }

  /* G17 星光背景层（配合 .cockpit-shell::before 毛玻璃柔焦） */
  function drawSky() {
    const el = $("sky");
    if (!el) return;
    const rings = [260, 420, 600, 800, 1020]
      .map((rx) => `<ellipse class="sky-ring" cx="990" cy="420" rx="${rx}" ry="${Math.round(rx * 0.6)}"/>`)
      .join("");
    const P = [
      [260, 190], [625, 238], [410, 414], [752, 408], [893, 172],
      [1233, 233], [1452, 344], [1540, 577], [1218, 662], [738, 618],
      [1650, 265], [1380, 820], [180, 760], [560, 880], [980, 790],
    ];
    const L = [[0, 1], [2, 1], [1, 4], [4, 5], [1, 3], [5, 6], [6, 7], [7, 8], [8, 9], [9, 14], [12, 13]];
    const lines = L
      .map((e) => `<line class="sky-line" x1="${P[e[0]][0]}" y1="${P[e[0]][1]}" x2="${P[e[1]][0]}" y2="${P[e[1]][1]}"/>`)
      .join("");
    const stars = P
      .map((q, i) =>
        i % 3 === 1
          ? `<circle class="sky-pt" cx="${q[0]}" cy="${q[1]}" r="6"/><circle class="sky-star" cx="${q[0]}" cy="${q[1]}" r="1.6"/>`
          : `<circle class="sky-star" cx="${q[0]}" cy="${q[1]}" r="${i % 2 ? 1.8 : 2.4}"/>`
      )
      .join("");
    el.setAttribute("viewBox", "0 0 1600 1000");
    el.setAttribute("preserveAspectRatio", "xMidYMid slice");
    el.innerHTML = rings + lines + stars;
  }

  /* 深翠黑夜（默认 dark） / 暖白日间（light）模式切换 */
  function initTheme() {
    const root = document.documentElement;
    let saved = "dark";
    try {
      saved = localStorage.getItem(THEME_KEY) || "dark";
    } catch (_) {}
    root.setAttribute("data-theme", saved === "light" ? "light" : "dark");

    const btn = $("theme-toggle");
    if (btn) {
      btn.addEventListener("click", () => {
        const cur = root.getAttribute("data-theme") === "light" ? "dark" : "light";
        root.setAttribute("data-theme", cur);
        try {
          localStorage.setItem(THEME_KEY, cur);
        } catch (_) {}
      });
    }
  }

  function formatHHMM(iso) {
    if (!iso) return "--:--";
    try {
      const d = new Date(iso);
      return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
    } catch (_) {
      return "--:--";
    }
  }

  function formatHHMMSS(iso) {
    if (!iso) return "--:--:--";
    try {
      const d = new Date(iso);
      return d.toLocaleTimeString("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      });
    } catch (_) {
      return "--:--";
    }
  }

  function formatNumber(val) {
    if (val == null || Number.isNaN(Number(val))) return "—";
    const v = Number(val);
    if (Number.isInteger(v)) return v.toString();
    return v.toFixed(1);
  }

  function formatMetricRaw(metricId, rawVal) {
    if (rawVal == null || Number.isNaN(Number(rawVal))) return "—";
    if (
      metricId === "co2" ||
      metricId === "tvoc_index" ||
      metricId === "nox_index" ||
      metricId === "pod_heart_rate" ||
      metricId === "pod_hrv" ||
      metricId === "pod_bed_temp" ||
      metricId === "pod_target_bed_temp"
    ) {
      return Math.round(Number(rawVal)).toString();
    }
    return formatNumber(rawVal);
  }

  function getRefPts(metrics) {
    const refTrend = (metrics || []).find((m) => Array.isArray(m.trend) && m.trend.length > 0)?.trend || [];
    return refTrend.slice(-12);
  }

  function getActiveIndex(totalPts) {
    if (totalPts <= 0) return 0;
    const raw = hoverIdx !== null ? hoverIdx : pinnedIdx !== null ? pinnedIdx : totalPts - 1;
    return Math.max(0, Math.min(totalPts - 1, raw));
  }

  function getMetricPointAt(m, activeIdx, totalPts) {
    const slice = Array.isArray(m.trend) ? m.trend.slice(-12) : [];
    if (slice.length === 0) {
      return {
        value: m.value,
        ts: m.observed_or_received_at,
        idx: 0,
      };
    }
    const mappedIdx =
      slice.length === totalPts
        ? activeIdx
        : Math.min(slice.length - 1, Math.max(0, slice.length - (totalPts - activeIdx)));
    const pt = slice[mappedIdx] || slice[slice.length - 1];
    return {
      value: pt[1],
      ts: pt[0],
      idx: mappedIdx,
    };
  }

  /* 11:50 – 12:00 可交互时点轨 */
  function renderUpdateTrail(metrics, lastSuccessAt) {
    const wrap = $("update-trail");
    if (!wrap) return;

    const pts = getRefPts(metrics);
    if (pts.length === 0) {
      trailSignature = "empty";
      wrap.innerHTML =
        `<div class="trail-line-bg"></div>` +
        `<div class="trail-nodes" style="justify-content:flex-end">` +
        `<button type="button" class="t-node is-active" aria-label="最新时点"><span class="t-dot"></span></button>` +
        `</div>`;
      if ($("trail-start")) $("trail-start").textContent = "--:--";
      if ($("trail-end")) $("trail-end").textContent = formatHHMM(lastSuccessAt);
      return;
    }

    const sig = pts.map((p) => p[0]).join("|");
    const activeIdx = getActiveIndex(pts.length);

    if (trailSignature !== sig) {
      trailSignature = sig;
      const nodesHtml = pts
        .map((p, i) => {
          const hhmmss = formatHHMMSS(p[0]);
          return (
            `<button type="button" class="t-node" data-idx="${i}" aria-label="采样时点 ${esc(hhmmss)}" title="${esc(hhmmss)}">` +
            `<span class="t-tip">${esc(hhmmss)}</span>` +
            `<span class="t-dot"></span>` +
            `</button>`
          );
        })
        .join("");

      wrap.innerHTML = `<div class="trail-line-bg"></div><div class="trail-nodes">${nodesHtml}</div>`;

      const buttons = wrap.querySelectorAll(".t-node");
      buttons.forEach((btn) => {
        const idx = Number(btn.getAttribute("data-idx"));
        btn.addEventListener("mouseenter", () => {
          hoverIdx = idx;
          syncInteractiveState();
        });
        btn.addEventListener("mouseleave", () => {
          hoverIdx = null;
          syncInteractiveState();
        });
        btn.addEventListener("focus", () => {
          hoverIdx = idx;
          syncInteractiveState();
        });
        btn.addEventListener("blur", () => {
          hoverIdx = null;
          syncInteractiveState();
        });
        btn.addEventListener("click", () => {
          if (pinnedIdx === idx) {
            pinnedIdx = null;
          } else {
            pinnedIdx = idx === pts.length - 1 ? null : idx;
          }
          syncInteractiveState();
        });
        btn.addEventListener("keydown", (e) => {
          if (e.key === "ArrowLeft" && idx > 0) {
            e.preventDefault();
            const prev = buttons[idx - 1];
            if (prev) {
              pinnedIdx = idx - 1;
              prev.focus();
              syncInteractiveState();
            }
          } else if (e.key === "ArrowRight" && idx < buttons.length - 1) {
            e.preventDefault();
            const next = buttons[idx + 1];
            if (next) {
              pinnedIdx = idx + 1 === pts.length - 1 ? null : idx + 1;
              next.focus();
              syncInteractiveState();
            }
          }
        });
      });
    }

    syncTrailHighlight(pts, activeIdx);
  }

  function syncTrailHighlight(pts, activeIdx) {
    const wrap = $("update-trail");
    if (!wrap || pts.length === 0) return;

    const buttons = wrap.querySelectorAll(".t-node");
    buttons.forEach((btn, i) => {
      btn.classList.toggle("is-active", i === activeIdx);
      btn.classList.toggle("is-pinned", pinnedIdx === i);
    });

    if ($("trail-start")) $("trail-start").textContent = formatHHMM(pts[0][0]);
    const endEl = $("trail-end");
    if (endEl) {
      const activePt = pts[activeIdx] || pts[pts.length - 1];
      endEl.textContent = formatHHMM(activePt[0]);
      endEl.classList.toggle("is-scrubbing", activeIdx !== pts.length - 1 || pinnedIdx !== null);
    }
  }

  function sparklineSvg(trend, activeIdx, totalPts, W = 220, H = 40) {
    const slice = Array.isArray(trend) ? trend.slice(-12) : [];
    if (slice.length < 2) {
      return `<svg viewBox="0 0 ${W} ${H}" aria-hidden="true"><line class="spark" x1="6" y1="${H / 2}" x2="${W - 6}" y2="${H / 2}" stroke-dasharray="3 3"/></svg>`;
    }
    const nums = slice.map((p) => Number(p[1]));
    const mn = Math.min(...nums);
    const mx = Math.max(...nums);
    const padX = 6;
    const padY = 6;
    const pts = slice.map((p, i) => {
      const x = padX + (i / (slice.length - 1)) * (W - padX * 2);
      const y = mx === mn ? H / 2 : H - padY - ((Number(p[1]) - mn) / (mx - mn)) * (H - padY * 2);
      return [x, y];
    });
    const mappedIdx =
      slice.length === totalPts
        ? Math.min(slice.length - 1, Math.max(0, activeIdx))
        : slice.length - 1;
    const poly = pts.map((q) => `${q[0].toFixed(1)},${q[1].toFixed(1)}`).join(" ");
    const dots = pts
      .map((q, i) =>
        i === mappedIdx
          ? ""
          : `<circle class="spark-pt" cx="${q[0].toFixed(1)}" cy="${q[1].toFixed(1)}" r="2"/>`
      )
      .join("");
    const activePt = pts[mappedIdx] || pts[pts.length - 1];
    return (
      `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">` +
      `<line class="spark-guide" x1="${activePt[0].toFixed(1)}" y1="2" x2="${activePt[0].toFixed(1)}" y2="${H - 2}"/>` +
      `<polyline class="spark" points="${poly}"/>` +
      dots +
      `<circle class="spark-last" cx="${activePt[0].toFixed(1)}" cy="${activePt[1].toFixed(1)}" r="3.4"/>` +
      `</svg>`
    );
  }

  function rangeText(trend) {
    const slice = Array.isArray(trend) ? trend.slice(-12) : [];
    if (slice.length === 0) return "—";
    const nums = slice.map((p) => Number(p[1]));
    const mn = Math.min(...nums);
    const mx = Math.max(...nums);
    return mn === mx ? formatNumber(mn) : `${formatNumber(mn)} – ${formatNumber(mx)}`;
  }

  function cleanLabel(m) {
    if (m.metric_id === "humidity") return "湿度";
    if (m.metric_id === "tvoc_index") return "TVOC";
    if (m.metric_id === "nox_index") return "NOx";
    if (m.metric_id === "pod_hrv") return "HRV";
    return m.label_zh;
  }

  function cleanUnit(m) {
    if (m.unit === "%RH") return "%";
    if (m.unit === "指数") return "";
    return m.unit;
  }

  function renderPrimaryTile(m, activeIdx, totalPts) {
    const el = document.createElement("article");
    el.className = "tile-primary";
    el.setAttribute("data-metric", m.metric_id);
    const unit = cleanUnit(m);
    const pt = getMetricPointAt(m, activeIdx, totalPts);
    el.innerHTML =
      `<div class="tile-head">` +
        `<span class="metric-icon">${metricIcon(m.metric_id)}</span>` +
        `<span>${esc(cleanLabel(m))}</span>` +
      `</div>` +
      `<div class="tile-reading">` +
        `<span class="v">${esc(formatMetricRaw(m.metric_id, pt.value))}</span>` +
        (unit ? `<span class="u">${esc(unit)}</span>` : "") +
      `</div>` +
      `<div class="tile-spark">${sparklineSvg(m.trend, activeIdx, totalPts, 230, 42)}</div>` +
      `<div class="tile-foot">` +
        `<span class="foot-item" title="波动区间">${ICONS.range}<span>${esc(rangeText(m.trend))}</span></span>` +
        `<span class="foot-item" title="采样时间">${ICONS.clock}<span class="foot-ts">${esc(formatHHMM(pt.ts))}</span></span>` +
      `</div>`;
    return el;
  }

  function renderSecondaryTile(m, activeIdx, totalPts) {
    const el = document.createElement("article");
    el.className = "tile-secondary";
    el.setAttribute("data-metric", m.metric_id);
    const unit = cleanUnit(m);
    const pt = getMetricPointAt(m, activeIdx, totalPts);
    el.innerHTML =
      `<div class="sec-left">` +
        `<span class="metric-icon">${metricIcon(m.metric_id)}</span>` +
        `<div class="sec-info">` +
          `<span class="sec-label">${esc(cleanLabel(m))}</span>` +
          `<div class="sec-reading">` +
            `<span class="v">${esc(formatMetricRaw(m.metric_id, pt.value))}</span>` +
            (unit ? `<span class="u">${esc(unit)}</span>` : "") +
          `</div>` +
        `</div>` +
      `</div>` +
      `<div class="sec-right">` +
        `<div class="sec-spark">${sparklineSvg(m.trend, activeIdx, totalPts, 128, 26)}</div>` +
        `<span class="sec-range">${esc(rangeText(m.trend))}</span>` +
      `</div>`;
    return el;
  }

  function renderPodTile(m, activeIdx, totalPts) {
    const el = document.createElement("article");
    el.className = "tile-pod";
    el.setAttribute("data-metric", m.metric_id);
    const unit = cleanUnit(m);
    const pt = getMetricPointAt(m, activeIdx, totalPts);
    el.innerHTML =
      `<div class="pod-tile-top">` +
        `<span class="pod-tile-label">` +
          `<span class="metric-icon">${metricIcon(m.metric_id)}</span>` +
          `<span>${esc(cleanLabel(m))}</span>` +
        `</span>` +
      `</div>` +
      `<div class="pod-tile-mid">` +
        `<div class="pod-reading">` +
          `<span class="v">${esc(formatMetricRaw(m.metric_id, pt.value))}</span>` +
          (unit ? `<span class="u">${esc(unit)}</span>` : "") +
        `</div>` +
        `<div class="pod-spark">${sparklineSvg(m.trend, activeIdx, totalPts, 80, 24)}</div>` +
      `</div>` +
      `<div class="pod-tile-foot">` +
        `<span>${esc(rangeText(m.trend))}</span>` +
        `<span>${esc(formatHHMM(pt.ts))}</span>` +
      `</div>`;
    return el;
  }

  function renderCards(metrics, activeIdx, totalPts) {
    const primary = [];
    const secondary = [];
    metrics.forEach((m) => {
      if (SECONDARY_IDS.has(m.metric_id) && metrics.length > 4) {
        secondary.push(m);
      } else if (PRIMARY_IDS.has(m.metric_id) || primary.length < 5) {
        primary.push(m);
      } else {
        secondary.push(m);
      }
    });

    const pGrid = $("metrics-grid");
    if (pGrid) {
      pGrid.innerHTML = "";
      primary.forEach((m) => pGrid.appendChild(renderPrimaryTile(m, activeIdx, totalPts)));
    }

    const sGrid = $("secondary-grid");
    if (sGrid) {
      sGrid.innerHTML = "";
      if (secondary.length > 0) {
        sGrid.classList.remove("hidden");
        secondary.forEach((m) => sGrid.appendChild(renderSecondaryTile(m, activeIdx, totalPts)));
      } else {
        sGrid.classList.add("hidden");
      }
    }
  }

  function renderPodCards(eightSleep, activeIdx, totalPts) {
    const grid = $("pod-metrics-grid");
    if (!grid) return;
    const podMetrics = Array.isArray(eightSleep?.metrics) ? eightSleep.metrics : [];
    grid.innerHTML = "";
    podMetrics.forEach((m) => {
      grid.appendChild(renderPodTile(m, activeIdx, totalPts));
    });

    if ($("pod-label")) {
      $("pod-label").textContent = "床微环境";
    }
    const statusEl = $("pod-status");
    if (statusEl && eightSleep) {
      statusEl.textContent = eightSleep.status_zh || "待接入";
      statusEl.classList.toggle("is-live", eightSleep.status === "CONNECTED");
    }
  }

  function syncInteractiveState() {
    if (!latestData) return;
    const metrics = Array.isArray(latestData.metrics) ? latestData.metrics : [];
    const pts = getRefPts(metrics);
    const activeIdx = getActiveIndex(pts.length);
    syncTrailHighlight(pts, activeIdx);
    renderCards(metrics, activeIdx, pts.length);
    if (latestData.eight_sleep) {
      renderPodCards(latestData.eight_sleep, activeIdx, pts.length);
    }
  }

  function updateScreen(data) {
    latestData = data;
    if ($("room-name")) {
      $("room-name").textContent = data.room_name_zh || "睡眠房";
    }

    const connected = data.actual_source_connectivity === "CONNECTED";
    const lastHHMM = formatHHMM(data.last_success_at);
    const stateBadge = $("state-badge");
    if (stateBadge) {
      stateBadge.innerHTML = connected
        ? `${ICONS.wifi}<span>在线</span>`
        : `${ICONS.unplug}<span>离线 · ${esc(lastHHMM)}</span>`;
    }

    const metrics = Array.isArray(data.metrics) ? data.metrics : [];
    const pts = getRefPts(metrics);
    const activeIdx = getActiveIndex(pts.length);
    renderUpdateTrail(metrics, data.last_success_at);
    renderCards(metrics, activeIdx, pts.length);

    if ($("pod-label")) {
      $("pod-label").textContent = "床微环境";
    }
    if (data.pod && $("pod-status")) {
      $("pod-status").textContent = data.pod.status_zh || "待接入";
    }
    if (data.eight_sleep) {
      renderPodCards(data.eight_sleep, activeIdx, pts.length);
    }
  }

  async function poll() {
    try {
      const res = await fetch(`/api/v1/rooms/${encodeURIComponent(roomId)}/display`, {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (res.ok) {
        const data = await res.json();
        roomId = data.room_id || roomId;
        updateScreen(data);
      }
    } catch (_) {}
  }

  function startClock() {
    const el = $("clock");
    if (!el) return;
    const tick = () => {
      el.textContent = new Date().toLocaleTimeString("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      });
    };
    tick();
    setInterval(tick, 1000);
  }

  initTheme();
  drawSky();
  startClock();
  poll();
  setInterval(poll, POLL_MS);
})();
