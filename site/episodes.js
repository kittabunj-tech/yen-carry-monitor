/* ==========================================================================
   episodes.js — Historical Episode Explorer (Phase 6E)
   โหลด data/episodes.json → เลือกเหตุการณ์ → replay ตัวชี้วัดก่อน/ระหว่าง/หลัง
   ========================================================================== */

(function () {
  "use strict";

  var CSS = {
    text: "#f0f6fc", muted: "#8b949e", dim: "#6e7681",
    grid: "rgba(139,148,158,.13)", border: "#30363d",
    accent: "#58a6ff", up: "#3fb950", down: "#f85149",
    watch: "#d29922", stress: "#db6d28"
  };
  var MONO = '"IBM Plex Mono", monospace';
  var FONT = '"Inter", system-ui, sans-serif';

  /** นิยามกราฟย่อยรายตัวชี้วัด */
  var PANELS = [
    { key: "usdjpy", title: "USDJPY (idx100)", color: CSS.accent,
      hint: "ลง = เยนแข็ง = แรง unwind" },
    { key: "audjpy", title: "AUDJPY (idx100)", color: "#79c0ff",
      hint: "carry pair คลาสสิก" },
    { key: "nikkei", title: "Nikkei 225 (idx100)", color: "#d2a8ff",
      hint: "feedback loop กับเยนแข็ง" },
    { key: "vix", title: "VIX", color: CSS.stress, hint: "ตัวจุดชนวน deleverage" },
    { key: "spread_us_jp", title: "US–JP 10Y Spread (%)", color: CSS.watch,
      hint: "แคบลง = แรงบีบ carry" },
    { key: "fx_realized_vol_10d", title: "FX Realized Vol (%)", color: "#f0883e",
      hint: "vol ระเบิดตอน unwind" },
    { key: "cot_net_k", title: "COT Net (พันสัญญา)", color: "#56d364",
      hint: "ติดลบลึก = short เยนหนาแน่น" }
  ];

  var state = { data: null, ep: null, charts: {}, cursor: 0 };

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  /* ---------------------------------------------------------------- data */

  var BASES = ["../data/", "data/", "./data/", "/data/"];

  function loadData() {
    var attempt = function (i) {
      if (i >= BASES.length) return Promise.reject(new Error("หา episodes.json ไม่เจอ"));
      return fetch(BASES[i] + "episodes.json?t=" + Date.now(), { cache: "no-store" })
        .then(function (r) { if (!r.ok) throw new Error("no"); return r.json(); })
        .catch(function () { return attempt(i + 1); });
    };
    return attempt(0);
  }

  /* ---------------------------------------------------------------- plugins */

  /** แรเงาช่วงก่อน/ระหว่าง/หลัง + เส้น day 0 + cursor ของ scrubber */
  var phasePlugin = {
    id: "epPhase",
    beforeDatasetsDraw: function (chart, args, opts) {
      var ep = opts.episode;
      if (!ep) return;
      var ctx = chart.ctx, area = chart.chartArea, x = chart.scales.x;
      if (!area || !x) return;
      var off = ep.day_offset;

      function px(dayOffset) {
        var idx = off.indexOf(dayOffset);
        if (idx < 0) { // clamp เข้าขอบ
          idx = dayOffset < off[0] ? 0 : off.length - 1;
        }
        return x.getPixelForValue(idx);
      }

      ctx.save();
      // ระหว่างเหตุการณ์ = แดงจาง
      ctx.fillStyle = "rgba(248,81,73,.10)";
      ctx.fillRect(px(ep.phases.during[0]), area.top,
                   px(ep.phases.during[1]) - px(ep.phases.during[0]), area.bottom - area.top);
      // หลัง = ฟ้าจางมาก
      ctx.fillStyle = "rgba(88,166,255,.05)";
      ctx.fillRect(px(ep.phases.post[0]), area.top,
                   px(ep.phases.post[1]) - px(ep.phases.post[0]), area.bottom - area.top);

      // เส้น day 0
      ctx.strokeStyle = CSS.down; ctx.setLineDash([4, 3]); ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.moveTo(px(0), area.top); ctx.lineTo(px(0), area.bottom); ctx.stroke();
      ctx.setLineDash([]);

      // cursor ของ scrubber
      var cx = px(opts.cursor || 0);
      ctx.strokeStyle = CSS.accent; ctx.lineWidth = 1.6; ctx.globalAlpha = .9;
      ctx.beginPath(); ctx.moveTo(cx, area.top); ctx.lineTo(cx, area.bottom); ctx.stroke();
      ctx.restore();
    }
  };

  /* ---------------------------------------------------------------- charts */

  function baseOpts(ep) {
    return {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        epPhase: { episode: ep, cursor: state.cursor },
        tooltip: {
          backgroundColor: "#0d1117", borderColor: CSS.border, borderWidth: 1,
          titleColor: CSS.text, bodyColor: CSS.text, padding: 8,
          titleFont: { family: MONO, size: 11 }, bodyFont: { family: MONO, size: 11 },
          callbacks: {
            title: function (items) {
              var i = items[0].dataIndex;
              return "day " + (ep.day_offset[i] >= 0 ? "+" : "") + ep.day_offset[i] +
                     " · " + ep.dates[i];
            }
          }
        }
      },
      scales: {
        x: {
          grid: { display: false }, border: { color: CSS.border },
          ticks: {
            color: CSS.dim, font: { family: MONO, size: 9.5 },
            maxRotation: 0, autoSkip: true, maxTicksLimit: 8,
            callback: function (v) {
              var o = ep.day_offset[v];
              return o === 0 ? "0" : (o > 0 ? "+" + o : String(o));
            }
          }
        },
        y: {
          grid: { color: CSS.grid, drawTicks: false }, border: { display: false },
          ticks: { color: CSS.dim, font: { family: MONO, size: 10 }, maxTicksLimit: 5 }
        }
      }
    };
  }

  function makeChart(canvasId, ep, seriesKey, color, fill) {
    var el = $(canvasId);
    if (!el) return null;
    if (state.charts[canvasId]) { state.charts[canvasId].destroy(); }
    var chart = new Chart(el.getContext("2d"), {
      type: "line",
      data: {
        labels: ep.dates,
        datasets: [{
          data: ep.series[seriesKey] || [],
          borderColor: color, borderWidth: 1.7,
          backgroundColor: fill ? color.replace(")", ",.08)").replace("rgb", "rgba") : undefined,
          pointRadius: 0, pointHoverRadius: 3, tension: .18,
          fill: !!fill, spanGaps: true
        }]
      },
      options: baseOpts(ep),
      plugins: [phasePlugin]
    });
    state.charts[canvasId] = chart;
    return chart;
  }

  /* ---------------------------------------------------------------- render */

  function renderSelector() {
    $("ep-selector").innerHTML = state.data.episodes.map(function (e) {
      return "<button type='button' class='ep-chip" +
        (state.ep && state.ep.id === e.id ? " is-active" : "") +
        "' data-id='" + esc(e.id) + "'>" + esc(e.title) + "</button>";
    }).join("");
  }

  /**
   * peak-to-trough: จุดสูงสุดช่วงก่อนเหตุการณ์ (ถึง day 0) → จุดต่ำสุดตั้งแต่ day 0
   * วัดแบบนี้เพราะบางเหตุการณ์ (เช่น ส.ค. 2024) วันร่วงหนักคือ day 0 เอง —
   * ถ้าวัดเทียบ day 0 จะได้ 0% ซึ่งจริงแต่ไร้ความหมาย
   */
  function fmtMove(series, ep) {
    var peakPre = null, trough = null;
    (series || []).forEach(function (v, i) {
      if (v == null) return;
      if (ep.day_offset[i] <= 0 && (peakPre === null || v > peakPre)) peakPre = v;
      if (ep.day_offset[i] >= 0 && (trough === null || v < trough)) trough = v;
    });
    if (peakPre === null || trough === null || peakPre === 0) return null;
    return (trough / peakPre - 1) * 100;
  }

  function renderStats(ep) {
    var s = ep.stats || {};
    var jpy = fmtMove(ep.series.usdjpy, ep);
    var nik = fmtMove(ep.series.nikkei, ep);
    var vixVals = (ep.series.vix || []).filter(function (v) { return v != null; });
    var cards = [
      ["CUSI peak", s.cusi_peak != null ? s.cusi_peak.toFixed(1) : "—",
       s.cusi_peak_offset != null ? "day " + (s.cusi_peak_offset >= 0 ? "+" : "") + s.cusi_peak_offset : ""],
      ["CUSI ณ day 0", s.cusi_at_anchor != null ? s.cusi_at_anchor.toFixed(1) : "—", ""],
      ["USDJPY peak→trough", jpy != null ? jpy.toFixed(1) + "%" : "—", "สูงสุดก่อน → ต่ำสุดหลัง"],
      ["Nikkei peak→trough", nik != null ? nik.toFixed(1) + "%" : "—", "สูงสุดก่อน → ต่ำสุดหลัง"],
      ["VIX สูงสุดในหน้าต่าง", vixVals.length ? Math.max.apply(null, vixVals).toFixed(1) : "—", ""]
    ];
    $("ep-stats").innerHTML = cards.map(function (c) {
      return "<div class='ep-stat'><div class='ep-stat__label'>" + esc(c[0]) + "</div>" +
        "<div class='ep-stat__value'>" + esc(c[1]) + "</div>" +
        (c[2] ? "<div class='ep-stat__label'>" + esc(c[2]) + "</div>" : "") + "</div>";
    }).join("");
  }

  function renderGrid(ep) {
    $("ep-grid").innerHTML = PANELS.map(function (p) {
      if (!ep.series[p.key]) return "";
      return "<article class='card'><header class='card__head'>" +
        "<h3 class='card__title card__title--sm'>" + esc(p.title) + "</h3></header>" +
        "<div class='chart-wrap'><canvas id='ep-mini-" + p.key + "'></canvas></div>" +
        "<p class='ep-mini__foot'>" + esc(p.hint) + "</p></article>";
    }).join("");
    PANELS.forEach(function (p) {
      if (ep.series[p.key]) makeChart("ep-mini-" + p.key, ep, p.key, p.color, false);
    });
  }

  function updateCursorReadout(ep) {
    var idx = ep.day_offset.indexOf(state.cursor);
    if (idx < 0) return;
    $("ep-cursor-day").textContent = "day " + (state.cursor >= 0 ? "+" : "") + state.cursor;
    $("ep-cursor-date").textContent = ep.dates[idx];
    var bits = [];
    var show = [["cusi", "CUSI"], ["usdjpy", "JPY"], ["vix", "VIX"], ["spread_us_jp", "spread"]];
    show.forEach(function (pair) {
      var v = (ep.series[pair[0]] || [])[idx];
      if (v != null) bits.push(pair[1] + " " + v.toFixed(1));
    });
    $("ep-cursor-values").textContent = bits.join(" · ");
  }

  function refreshCursor() {
    var ep = state.ep;
    Object.keys(state.charts).forEach(function (k) {
      var ch = state.charts[k];
      ch.options.plugins.epPhase.cursor = state.cursor;
      ch.update("none");
    });
    updateCursorReadout(ep);
  }

  function selectEpisode(id) {
    var ep = state.data.episodes.filter(function (e) { return e.id === id; })[0];
    if (!ep) return;
    state.ep = ep;
    state.cursor = 0;

    renderSelector();
    $("ep-title").textContent = ep.title + "  ·  " + ep.anchor;
    $("ep-desc").textContent = ep.desc || "";
    renderStats(ep);

    makeChart("ep-cusi", ep, "cusi", CSS.accent, false);
    renderGrid(ep);

    var slider = $("ep-slider");
    slider.min = ep.day_offset[0];
    slider.max = ep.day_offset[ep.day_offset.length - 1];
    slider.value = 0;
    updateCursorReadout(ep);
  }

  /* ---------------------------------------------------------------- init */

  function init() {
    loadData().then(function (data) {
      state.data = data;
      renderSelector();
      $("ep-selector").addEventListener("click", function (e) {
        var btn = e.target.closest("[data-id]");
        if (btn) selectEpisode(btn.dataset.id);
      });
      $("ep-slider").addEventListener("input", function (e) {
        state.cursor = parseInt(e.target.value, 10);
        refreshCursor();
      });
      // เริ่มที่เหตุการณ์อ้างอิงหลักของโปรเจกต์
      selectEpisode("aug2024");
    }).catch(function (err) {
      $("ep-selector").innerHTML = "<span class='na'>โหลดข้อมูลไม่สำเร็จ: " +
        esc(err.message) + " — รัน backtest/build_episodes.py ก่อน</span>";
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else { init(); }
})();
