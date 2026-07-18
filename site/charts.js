/* ==========================================================================
   charts.js — Chart.js panels (Row 1 history + Row 3 computed panels)
   ต้องโหลดก่อน app.js; export ผ่าน window.YCMCharts

   หมายเหตุการออกแบบ:
   * ใช้ category axis (label เป็น string วันที่) ไม่ใช่ time axis
     → ไม่ต้องพึ่ง chartjs-adapter-date-fns เพิ่มอีก CDN หนึ่งตัว
   * แถบสีระดับ + annotation เหตุการณ์ implement เป็น inline plugin
     → ไม่ต้องพึ่ง chartjs-plugin-annotation
   * ทุก series มี null คั่น (เสาร์–อาทิตย์ / วันหยุด) → ตั้ง spanGaps: true
     ไม่งั้นเส้นจะขาดเป็นท่อน ๆ
   ========================================================================== */

(function (global) {
  "use strict";

  var CSS = {
    text:     "#f0f6fc",
    muted:    "#8b949e",
    dim:      "#6e7681",
    grid:     "rgba(139, 148, 158, 0.13)",
    border:   "#30363d",
    card:     "#161b22",
    accent:   "#58a6ff",
    up:       "#3fb950",
    down:     "#f85149",
    watch:    "#d29922",
    stress:   "#db6d28"
  };

  var FONT = '"Inter", system-ui, sans-serif';
  var MONO = '"IBM Plex Mono", monospace';

  /** เก็บ instance ไว้ทำลายก่อนวาดใหม่ (กัน memory leak ตอน auto-refresh) */
  var registry = {};

  /* ---------------------------------------------------------------- utils */

  /**
   * ทำลาย chart เดิมที่ id นี้ (ถ้ามี) แล้วคืน canvas context
   * @param {string} id - id ของ <canvas>
   * @returns {CanvasRenderingContext2D|null}
   */
  function claim(id) {
    if (registry[id]) {
      registry[id].destroy();
      delete registry[id];
    }
    var el = document.getElementById(id);
    return el ? el.getContext("2d") : null;
  }

  /**
   * ย่อวันที่ ISO เป็น label สั้น ๆ สำหรับแกน x
   * @param {string} iso - "YYYY-MM-DD"
   * @returns {string} "DD/MM"
   */
  function shortDate(iso) {
    var p = String(iso).split("-");
    return p.length === 3 ? p[2] + "/" + p[1] : String(iso);
  }

  /**
   * ตัดข้อมูลให้เหลือ n จุดสุดท้าย
   * @param {Array} arr
   * @param {number|string} n - จำนวนจุด หรือ "all"
   * @returns {Array}
   */
  function tail(arr, n) {
    if (!Array.isArray(arr)) return [];
    if (n === "all" || !n || arr.length <= n) return arr.slice();
    return arr.slice(arr.length - n);
  }

  /**
   * คำนวณ percentile ของชุดตัวเลข (ละเว้น null/NaN)
   * @param {Array<number|null>} values
   * @param {number} p - 0–100
   * @returns {number|null}
   */
  function percentile(values, p) {
    var clean = (values || []).filter(function (v) {
      return v !== null && v !== undefined && isFinite(v);
    }).sort(function (a, b) { return a - b; });
    if (!clean.length) return null;
    var idx = (p / 100) * (clean.length - 1);
    var lo = Math.floor(idx), hi = Math.ceil(idx);
    if (lo === hi) return clean[lo];
    return clean[lo] + (clean[hi] - clean[lo]) * (idx - lo);
  }

  /** ตัวเลือกพื้นฐานร่วมของทุก chart */
  function baseOptions(extra) {
    var opts = {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          display: false,
          labels: { color: CSS.muted, font: { family: FONT, size: 11 }, boxWidth: 10, boxHeight: 10 }
        },
        tooltip: {
          backgroundColor: "#0d1117",
          borderColor: CSS.border,
          borderWidth: 1,
          titleColor: CSS.text,
          bodyColor: CSS.muted,
          titleFont: { family: FONT, size: 11.5 },
          bodyFont: { family: MONO, size: 11.5 },
          padding: 9,
          displayColors: true,
          boxWidth: 8,
          boxHeight: 8
        }
      },
      scales: {
        x: {
          grid: { color: CSS.grid, drawTicks: false },
          border: { color: CSS.border },
          ticks: {
            color: CSS.dim,
            font: { family: MONO, size: 9.5 },
            maxRotation: 0,
            autoSkip: true,
            maxTicksLimit: 8
          }
        },
        y: {
          grid: { color: CSS.grid, drawTicks: false },
          border: { display: false },
          ticks: { color: CSS.dim, font: { family: MONO, size: 10 }, maxTicksLimit: 6 }
        }
      }
    };
    return Object.assign(opts, extra || {});
  }

  /* ------------------------------------------------------------- plugins */

  /**
   * วาดแถบสีพื้นหลัง 4 ระดับของ CUSI (§3.3)
   * อ่าน threshold จาก options.plugins.levelBands.levels (มาจาก cusi_history.json)
   */
  var levelBandsPlugin = {
    id: "levelBands",
    beforeDatasetsDraw: function (chart, args, opts) {
      var levels = (opts && opts.levels) || [];
      if (!levels.length) return;
      var ctx = chart.ctx;
      var yAxis = chart.scales.y;
      var area = chart.chartArea;
      if (!yAxis || !area) return;

      ctx.save();
      var lower = 0;
      levels.forEach(function (lv) {
        var upper = Math.min(lv.max, 100);
        var yTop = yAxis.getPixelForValue(upper);
        var yBottom = yAxis.getPixelForValue(lower);
        // clamp ให้อยู่ในกรอบ chart
        var top = Math.max(area.top, Math.min(yTop, yBottom));
        var bottom = Math.min(area.bottom, Math.max(yTop, yBottom));
        if (bottom > top) {
          ctx.fillStyle = lv.color;
          ctx.globalAlpha = 0.10;
          ctx.fillRect(area.left, top, area.right - area.left, bottom - top);

          // เส้นแบ่งระดับ + ป้ายชื่อ
          ctx.globalAlpha = 0.5;
          ctx.strokeStyle = lv.color;
          ctx.lineWidth = 1;
          ctx.setLineDash([3, 3]);
          ctx.beginPath();
          ctx.moveTo(area.left, top);
          ctx.lineTo(area.right, top);
          ctx.stroke();
          ctx.setLineDash([]);

          ctx.globalAlpha = 0.85;
          ctx.fillStyle = lv.color;
          ctx.font = "10px " + FONT;
          ctx.textAlign = "left";
          ctx.textBaseline = "top";
          if (bottom - top > 16) ctx.fillText(lv.name, area.left + 6, top + 3);
        }
        lower = upper;
      });
      ctx.restore();
    }
  };

  /**
   * วาดเส้นแนวตั้ง + ป้ายกำกับเหตุการณ์สำคัญ (เช่น Aug 2024 unwind)
   * options.plugins.eventMarkers.events = [{date, label}]
   */
  var eventMarkersPlugin = {
    id: "eventMarkers",
    afterDatasetsDraw: function (chart, args, opts) {
      var events = (opts && opts.events) || [];
      if (!events.length) return;
      var labels = chart.data.labels || [];
      var ctx = chart.ctx;
      var area = chart.chartArea;
      var xAxis = chart.scales.x;
      if (!area || !xAxis) return;

      events.forEach(function (ev) {
        var idx = labels.indexOf(ev.date);
        if (idx < 0) return;                       // เหตุการณ์อยู่นอกช่วงที่แสดง
        var x = xAxis.getPixelForValue(idx);
        if (x < area.left || x > area.right) return;

        ctx.save();
        ctx.strokeStyle = ev.color || CSS.down;
        ctx.lineWidth = 1.2;
        ctx.setLineDash([4, 3]);
        ctx.globalAlpha = 0.9;
        ctx.beginPath();
        ctx.moveTo(x, area.top);
        ctx.lineTo(x, area.bottom);
        ctx.stroke();
        ctx.setLineDash([]);

        // ป้ายชื่อ — พลิกไปทางซ้ายถ้าชนขอบขวา
        var text = ev.label;
        ctx.font = "600 10px " + FONT;
        var w = ctx.measureText(text).width + 12;
        var flip = (x + w + 6) > area.right;
        var bx = flip ? x - w - 5 : x + 5;

        ctx.globalAlpha = 1;
        ctx.fillStyle = "rgba(13,17,23,.92)";
        ctx.strokeStyle = ev.color || CSS.down;
        ctx.lineWidth = 1;
        var by = area.top + 5;
        if (ctx.roundRect) {
          ctx.beginPath();
          ctx.roundRect(bx, by, w, 18, 4);
          ctx.fill();
          ctx.stroke();
        } else {
          ctx.fillRect(bx, by, w, 18);
          ctx.strokeRect(bx, by, w, 18);
        }
        ctx.fillStyle = ev.color || CSS.down;
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.fillText(text, bx + 6, by + 9.5);
        ctx.restore();
      });
    }
  };

  /**
   * วาดเส้นแนวนอนอ้างอิง (เช่น P80 ของ realized vol)
   * options.plugins.hLines.lines = [{value, label, color, dash}]
   */
  var hLinesPlugin = {
    id: "hLines",
    afterDatasetsDraw: function (chart, args, opts) {
      var lines = (opts && opts.lines) || [];
      if (!lines.length) return;
      var ctx = chart.ctx;
      var y = chart.scales.y;
      var area = chart.chartArea;
      if (!y || !area) return;

      lines.forEach(function (ln) {
        if (ln.value === null || ln.value === undefined || !isFinite(ln.value)) return;
        var py = y.getPixelForValue(ln.value);
        if (py < area.top || py > area.bottom) return;

        ctx.save();
        ctx.strokeStyle = ln.color || CSS.watch;
        ctx.lineWidth = 1.2;
        ctx.setLineDash(ln.dash || [5, 4]);
        ctx.globalAlpha = 0.85;
        ctx.beginPath();
        ctx.moveTo(area.left, py);
        ctx.lineTo(area.right, py);
        ctx.stroke();
        ctx.setLineDash([]);

        if (ln.label) {
          ctx.font = "10px " + MONO;
          ctx.fillStyle = ln.color || CSS.watch;
          ctx.textAlign = "right";
          ctx.textBaseline = "bottom";
          ctx.fillText(ln.label, area.right - 4, py - 3);
        }
        ctx.restore();
      });
    }
  };

  /* ------------------------------------------------------------- charts */

  /**
   * CUSI history — line + แถบสี 4 ระดับ + annotation เหตุการณ์
   * @param {Object} history - payload ของ cusi_history.json
   * @param {number|string} range - จำนวนวันล่าสุด หรือ "all"
   * @param {Array<Object>} events - [{date, label}]
   * @returns {Object|null} Chart instance
   */
  function renderCusiHistory(history, range, events) {
    var ctx = claim("chart-cusi");
    if (!ctx || !history || !Array.isArray(history.history)) return null;

    var rows = tail(history.history, range);
    var labels = rows.map(function (r) { return r.date; });
    var values = rows.map(function (r) { return r.value; });

    var chart = new Chart(ctx, {
      type: "line",
      data: {
        labels: labels,
        datasets: [{
          label: "CUSI",
          data: values,
          borderColor: CSS.accent,
          borderWidth: 1.8,
          pointRadius: 0,
          pointHoverRadius: 4,
          pointHoverBackgroundColor: CSS.accent,
          tension: 0.16,
          fill: false,
          spanGaps: true
        }]
      },
      options: baseOptions({
        plugins: {
          legend: { display: false },
          levelBands: { levels: history.levels || [] },
          eventMarkers: { events: events || [] },
          tooltip: {
            backgroundColor: "#0d1117",
            borderColor: CSS.border,
            borderWidth: 1,
            titleColor: CSS.text,
            bodyColor: CSS.text,
            padding: 9,
            callbacks: {
              title: function (items) { return items[0].label; },
              label: function (item) {
                var row = rows[item.dataIndex];
                return "CUSI " + item.parsed.y.toFixed(1) + "  ·  " + (row ? row.level : "");
              }
            }
          }
        },
        scales: {
          x: {
            grid: { display: false },
            border: { color: CSS.border },
            ticks: {
              color: CSS.dim, font: { family: MONO, size: 9.5 },
              maxRotation: 0, autoSkip: true, maxTicksLimit: 9,
              callback: function (v) {
                var lbl = this.getLabelForValue(v);
                return String(lbl).slice(0, 7); // YYYY-MM
              }
            }
          },
          y: {
            min: 0, max: 100,
            grid: { color: CSS.grid, drawTicks: false },
            border: { display: false },
            ticks: { color: CSS.dim, font: { family: MONO, size: 10 }, stepSize: 20 }
          }
        }
      }),
      plugins: [levelBandsPlugin, eventMarkersPlugin]
    });

    registry["chart-cusi"] = chart;
    return chart;
  }

  /**
   * US–JP 10Y spread
   * @param {Object} hist - payload ของ indicators_history.json
   */
  function renderSpread(hist) {
    var ctx = claim("chart-spread");
    if (!ctx || !hist || !hist.series || !hist.series.spread_us_jp) return null;

    var chart = new Chart(ctx, {
      type: "line",
      data: {
        labels: hist.dates.map(shortDate),
        datasets: [{
          label: "US–JP 10Y (%)",
          data: hist.series.spread_us_jp,
          borderColor: CSS.accent,
          backgroundColor: "rgba(88,166,255,.10)",
          borderWidth: 1.7,
          pointRadius: 0,
          pointHoverRadius: 3.5,
          tension: 0.2,
          fill: true,
          spanGaps: true
        }]
      },
      options: baseOptions({
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "#0d1117", borderColor: CSS.border, borderWidth: 1,
            titleColor: CSS.text, bodyColor: CSS.text, padding: 9,
            callbacks: {
              label: function (i) { return "spread " + i.parsed.y.toFixed(3) + "%"; }
            }
          }
        }
      })
    });

    registry["chart-spread"] = chart;
    return chart;
  }

  /**
   * FX realized vol 10d + เส้น P80 (คำนวณจากหน้าต่างที่แสดงอยู่)
   * @param {Object} hist - payload ของ indicators_history.json
   * @returns {Object} {chart, p80}
   */
  function renderFxVol(hist) {
    var ctx = claim("chart-fxvol");
    if (!ctx || !hist || !hist.series || !hist.series.fx_realized_vol_10d) return { chart: null, p80: null };

    var series = hist.series.fx_realized_vol_10d;
    var p80 = percentile(series, 80);

    var chart = new Chart(ctx, {
      type: "line",
      data: {
        labels: hist.dates.map(shortDate),
        datasets: [
          {
            label: "Realized vol 10d (%)",
            data: series,
            borderColor: CSS.watch,
            backgroundColor: "rgba(210,153,34,.11)",
            borderWidth: 1.7,
            pointRadius: 0,
            pointHoverRadius: 3.5,
            tension: 0.2,
            fill: true,
            spanGaps: true
          },
          {
            label: "ATR% 14",
            data: hist.series.fx_atr_pct_14 || [],
            borderColor: CSS.dim,
            borderWidth: 1.1,
            borderDash: [4, 3],
            pointRadius: 0,
            tension: 0.2,
            fill: false,
            spanGaps: true,
            yAxisID: "y1"
          }
        ]
      },
      options: baseOptions({
        plugins: {
          legend: {
            display: true,
            position: "bottom",
            labels: { color: CSS.muted, font: { family: FONT, size: 10 }, boxWidth: 9, boxHeight: 9, padding: 10 }
          },
          hLines: {
            lines: [{ value: p80, label: "P80 " + (p80 === null ? "" : p80.toFixed(1)), color: CSS.stress }]
          },
          tooltip: {
            backgroundColor: "#0d1117", borderColor: CSS.border, borderWidth: 1,
            titleColor: CSS.text, bodyColor: CSS.text, padding: 9
          }
        },
        scales: {
          x: {
            grid: { display: false },
            border: { color: CSS.border },
            ticks: { color: CSS.dim, font: { family: MONO, size: 9.5 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 7 }
          },
          y: {
            grid: { color: CSS.grid, drawTicks: false },
            border: { display: false },
            ticks: { color: CSS.dim, font: { family: MONO, size: 10 }, maxTicksLimit: 6 }
          },
          y1: {
            position: "right",
            grid: { display: false },
            border: { display: false },
            ticks: { color: CSS.dim, font: { family: MONO, size: 9.5 }, maxTicksLimit: 4 }
          }
        }
      }),
      plugins: [hLinesPlugin]
    });

    registry["chart-fxvol"] = chart;
    return { chart: chart, p80: p80 };
  }

  /**
   * COT net speculative positions — bar, ติดลบ = แดง (short crowded)
   * @param {Object} cot - payload ของ cot_history.json
   * @param {number} weeks - จำนวนสัปดาห์ล่าสุดที่จะแสดง
   */
  function renderCot(cot, weeks) {
    var ctx = claim("chart-cot");
    if (!ctx || !cot || !Array.isArray(cot.reports)) return null;

    var rows = tail(cot.reports, weeks || 104);
    var values = rows.map(function (r) { return r.net_speculative; });

    var chart = new Chart(ctx, {
      type: "bar",
      data: {
        labels: rows.map(function (r) { return r.report_date; }),
        datasets: [{
          label: "Net speculative",
          data: values,
          backgroundColor: values.map(function (v) {
            return v < 0 ? "rgba(248,81,73,.72)" : "rgba(63,185,80,.72)";
          }),
          borderWidth: 0,
          barPercentage: 1.0,
          categoryPercentage: 0.94
        }]
      },
      options: baseOptions({
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "#0d1117", borderColor: CSS.border, borderWidth: 1,
            titleColor: CSS.text, bodyColor: CSS.text, padding: 9,
            callbacks: {
              title: function (items) { return "รายงาน " + items[0].label; },
              label: function (i) { return "net " + i.parsed.y.toLocaleString("en-US"); }
            }
          }
        },
        scales: {
          x: {
            grid: { display: false },
            border: { color: CSS.border },
            ticks: {
              color: CSS.dim, font: { family: MONO, size: 9.5 },
              maxRotation: 0, autoSkip: true, maxTicksLimit: 6,
              callback: function (v) { return String(this.getLabelForValue(v)).slice(0, 7); }
            }
          },
          y: {
            grid: { color: CSS.grid, drawTicks: false },
            border: { display: false },
            ticks: {
              color: CSS.dim, font: { family: MONO, size: 10 }, maxTicksLimit: 6,
              callback: function (v) { return (v / 1000).toFixed(0) + "k"; }
            }
          }
        }
      })
    });

    registry["chart-cot"] = chart;
    return chart;
  }

  /**
   * MOVE vs VIX — สอง scale (bond vol มักนำ FX vol, §2.2 #12)
   * @param {Object} hist - payload ของ indicators_history.json
   */
  function renderMoveVix(hist) {
    var ctx = claim("chart-movevix");
    if (!ctx || !hist || !hist.series) return null;

    var chart = new Chart(ctx, {
      type: "line",
      data: {
        labels: hist.dates.map(shortDate),
        datasets: [
          {
            label: "MOVE",
            data: hist.series.move || [],
            borderColor: CSS.stress,
            borderWidth: 1.7,
            pointRadius: 0,
            pointHoverRadius: 3.5,
            tension: 0.2,
            fill: false,
            spanGaps: true,
            yAxisID: "y"
          },
          {
            label: "VIX",
            data: hist.series.vix || [],
            borderColor: CSS.accent,
            borderWidth: 1.7,
            pointRadius: 0,
            pointHoverRadius: 3.5,
            tension: 0.2,
            fill: false,
            spanGaps: true,
            yAxisID: "y1"
          }
        ]
      },
      options: baseOptions({
        plugins: {
          legend: {
            display: true,
            position: "bottom",
            labels: { color: CSS.muted, font: { family: FONT, size: 10 }, boxWidth: 9, boxHeight: 9, padding: 10 }
          },
          tooltip: {
            backgroundColor: "#0d1117", borderColor: CSS.border, borderWidth: 1,
            titleColor: CSS.text, bodyColor: CSS.text, padding: 9
          }
        },
        scales: {
          x: {
            grid: { display: false },
            border: { color: CSS.border },
            ticks: { color: CSS.dim, font: { family: MONO, size: 9.5 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 7 }
          },
          y: {
            position: "left",
            grid: { color: CSS.grid, drawTicks: false },
            border: { display: false },
            ticks: { color: CSS.stress, font: { family: MONO, size: 10 }, maxTicksLimit: 5 }
          },
          y1: {
            position: "right",
            grid: { display: false },
            border: { display: false },
            ticks: { color: CSS.accent, font: { family: MONO, size: 10 }, maxTicksLimit: 5 }
          }
        }
      })
    });

    registry["chart-movevix"] = chart;
    return chart;
  }

  /** ทำลาย chart ทั้งหมด (ใช้ตอน teardown) */
  function destroyAll() {
    Object.keys(registry).forEach(function (k) {
      registry[k].destroy();
      delete registry[k];
    });
  }

  global.YCMCharts = {
    renderCusiHistory: renderCusiHistory,
    renderSpread: renderSpread,
    renderFxVol: renderFxVol,
    renderCot: renderCot,
    renderMoveVix: renderMoveVix,
    destroyAll: destroyAll,
    percentile: percentile,
    available: function () { return typeof global.Chart !== "undefined"; }
  };
})(window);
