/* ==========================================================================
   app.js — Yen Carry Monitor dashboard
   fetch data/*.json → render gauge, components, table, alerts, charts, widgets
   auto-refresh ทุก 5 นาที
   ========================================================================== */

(function () {
  "use strict";

  /* ============================== CONFIG ============================== */

  var REFRESH_MS = 5 * 60 * 1000;   // 5 นาที ตาม spec Phase 3 ข้อ 3

  /**
   * ผู้ใช้อาจเสิร์ฟไซต์ได้ 2 แบบ (spec §5 Phase 5 เตือนเรื่อง path นี้ไว้):
   *   1. `python -m http.server` จาก repo root → หน้าอยู่ /site/ ข้อมูลอยู่ /data/
   *   2. GitHub Pages ที่ตั้ง root เป็น /site → ข้อมูลถูก copy มาไว้ข้าง ๆ
   * จึงลองหลาย base แล้วจำตัวที่ใช้ได้
   */
  var DATA_BASES = ["../data/", "data/", "./data/", "/data/"];
  var dataBase = null;

  /** เหตุการณ์อ้างอิงบนกราฟ CUSI (spec §3.4) */
  var EVENTS = [
    { date: "2024-08-05", label: "Aug 2024 unwind" },
    { date: "2022-12-20", label: "BOJ YCC tweak" }
  ];

  /** TradingView mini-chart widgets (spec Phase 3 ข้อ 5)
   *
   * NOTE เรื่อง symbol (ตรวจสอบในเบราว์เซอร์จริงแล้ว):
   *   TradingView บล็อก index/yield/vol ของ exchange ในwidget ฟรี
   *   (ขึ้น "This symbol is only available on TradingView" เพราะข้อมูล
   *    real-time มีลิขสิทธิ์) จึงต้องใช้ feed ของโบรกเกอร์ CFD แทน:
   *     VIX  → CAPITALCOM:VIX      (CBOE:VIX / TVC:VIX ใช้ไม่ได้)
   *     SPX  → CAPITALCOM:US500    (SP:SPX ใช้ไม่ได้)
   *     NDX  → CAPITALCOM:US100    (NASDAQ:IXIC ใช้ไม่ได้)
   *     N225 → CAPITALCOM:J225     (TVC:NI225 ใช้ไม่ได้)
   *
   *   JGB 10Y yield: **ไม่มี symbol ใน widget ฟรีเลย** (bond yield ทุกตัว
   *   รวม US10Y/DXY embed ไม่ได้) — ต่างจาก spec §2.3 ที่เสนอ TVC:JP10Y
   *   ค่า JGB จริงจาก MOF ยังแสดงในตาราง indicators ด้านล่าง จึงสลับช่องนี้
   *   เป็น Gold (safe-haven confirmation ตาม spec §2.2 #15) ที่ทำงานได้
   */
  var TV_SYMBOLS = [
    { symbol: "FX:USDJPY",      label: "USD/JPY" },
    { symbol: "FX:AUDJPY",      label: "AUD/JPY" },
    { symbol: "CAPITALCOM:J225", label: "Nikkei 225" },
    { symbol: "CAPITALCOM:VIX", label: "VIX" },
    { symbol: "CAPITALCOM:US100", label: "Nasdaq 100" },
    { symbol: "CAPITALCOM:US500", label: "S&P 500" },
    { symbol: "TVC:GOLD",       label: "Gold" },
    { symbol: "FX_IDC:MXNJPY",  label: "MXN/JPY" }
  ];

  /** ระดับสำรอง เผื่อ cusi_history.json โหลดไม่ได้ (ปกติอ่านจากไฟล์) */
  var FALLBACK_LEVELS = [
    { name: "CALM",   max: 40,    color: "#3fb950" },
    { name: "WATCH",  max: 60,    color: "#d29922" },
    { name: "STRESS", max: 80,    color: "#db6d28" },
    { name: "UNWIND", max: 100.1, color: "#f85149" }
  ];

  /** นิยามแถวของตาราง indicators */
  var INDICATOR_ROWS = [
    { key: "usdjpy", label: "USD/JPY",   sub: "USDJPY=X",  dec: 3, d1: "chg_1d_pct", d5: "chg_5d_pct", extra: zExtra("z_5d") },
    { key: "audjpy", label: "AUD/JPY",   sub: "AUDJPY=X",  dec: 3, d1: "chg_1d_pct", d5: null,          extra: ddExtra() },
    { key: "mxnjpy", label: "MXN/JPY",   sub: "MXNJPY=X",  dec: 4, d1: null,          d5: null,          extra: ddExtra() },
    { key: "nikkei", label: "Nikkei 225", sub: "^N225",    dec: 0, d1: null,          d5: "chg_5d_pct",  extra: null },
    { key: "ndx",    label: "Nasdaq 100", sub: "^NDX",     dec: 0, d1: null,          d5: "chg_5d_pct",  extra: null },
    { key: "spx",    label: "S&P 500",   sub: "^GSPC",     dec: 0, d1: null,          d5: "chg_5d_pct",  extra: null },
    { key: "vix",    label: "VIX",       sub: "^VIX",      dec: 2, d1: null,          d5: null,
      d5raw: "chg_5d", extra: pctileExtra("percentile_3y") },
    { key: "move",   label: "MOVE",      sub: "^MOVE",     dec: 2, d1: null,          d5: null,          extra: null },
    { key: "us10y",  label: "US 10Y",    sub: "^TNX · %",  dec: 3, d1: null,          d5: null,
      bps: "chg_5d_bps", extra: null },
    { key: "us2y",   label: "US 2Y",     sub: "2YY=F · %", dec: 3, d1: null,          d5: null,
      bps: "chg_5d_bps", extra: null },
    { key: "jgb10y", label: "JGB 10Y",   sub: "MOF · %",   dec: 3, d1: null,          d5: null,
      bps: "chg_5d_bps", extra: sourceExtra() },
    { key: "spread_us_jp", label: "US–JP Spread", sub: "US10Y − JGB10Y · %", dec: 3, d1: null, d5: null,
      bps: "chg_20d_bps", bpsLabel: "20d", extra: zExtra("z") },
    { key: "fx_vol", label: "FX Realized Vol", sub: "USDJPY 10d · %", dec: 2, valueKey: "realized_10d",
      d1: null, d5: null, extra: fxVolExtra() },
    { key: "fx_implied", label: "FX Implied Vol", sub: "FXY options 1M ATM · %", dec: 2,
      valueKey: "iv_atm_1m_pct", d1: null, d5: null, extra: impliedExtra() },
    { key: "cot_jpy", label: "COT JPY Net", sub: "CFTC · contracts", dec: 0, valueKey: "net_position",
      d1: null, d5: null, extra: cotExtra() },
    { key: "dxy",  label: "DXY",     sub: "DX-Y.NYB", dec: 3, d1: null, d5: "chg_5d_pct", extra: null },
    { key: "gold", label: "Gold",    sub: "GC=F",     dec: 2, d1: null, d5: "chg_5d_pct", extra: null },
    { key: "btc",  label: "Bitcoin", sub: "BTC-USD",  dec: 0, d1: null, d5: "chg_5d_pct", extra: null }
  ];

  /** ชื่อไทยของ component (§3.2) */
  var COMPONENT_LABELS = {
    jpy_momentum:       "JPY Momentum",
    audjpy_drawdown:    "AUDJPY Drawdown",
    spread_compression: "Spread Compression",
    vix:                "VIX Level + Change",
    fx_realized_vol:    "FX Realized Vol",
    cot_positioning:    "COT Positioning",
    move:               "MOVE",
    nikkei_momentum:    "Nikkei Momentum",
    mxnjpy_drawdown:    "MXNJPY Drawdown"
  };

  /* ============================== STATE ============================== */

  var state = {
    latest: null,
    cusiHistory: null,
    indicatorsHistory: null,
    cotHistory: null,
    range: "all",
    timer: null
  };

  /* ============================== UTILS ============================== */

  /** @returns {HTMLElement|null} */
  function $(id) { return document.getElementById(id); }

  /**
   * ตรวจว่าค่าเป็นตัวเลขที่ใช้ได้จริง (ไม่ใช่ null/undefined/NaN)
   * @param {*} v
   * @returns {boolean}
   */
  function isNum(v) { return v !== null && v !== undefined && typeof v === "number" && isFinite(v); }

  /**
   * จัดรูปแบบตัวเลขพร้อม thousand separator
   * @param {*} v
   * @param {number} dec - ตำแหน่งทศนิยม
   * @returns {string} "—" ถ้าไม่มีค่า
   */
  function fmt(v, dec) {
    if (!isNum(v)) return "—";
    return v.toLocaleString("en-US", { minimumFractionDigits: dec, maximumFractionDigits: dec });
  }

  /**
   * จัดรูปแบบค่าเปลี่ยนแปลงพร้อมเครื่องหมาย +/-
   * @param {*} v
   * @param {number} dec
   * @param {string} suffix
   * @returns {string}
   */
  function fmtSigned(v, dec, suffix) {
    if (!isNum(v)) return "—";
    return (v > 0 ? "+" : "") + v.toFixed(dec) + (suffix || "");
  }

  /**
   * คืน class สีตามเครื่องหมายของค่า
   * @param {*} v
   * @returns {string}
   */
  function signClass(v) {
    if (!isNum(v)) return "na";
    if (v > 0) return "pos";
    if (v < 0) return "neg";
    return "flat";
  }

  /**
   * คืน class สีสำหรับ "ความเสี่ยง" — ตรงข้ามกับ signClass ของราคา
   * z บวก = เสี่ยงมากขึ้น = สีร้อน (ไม่ใช่เขียวแบบราคาขึ้น)
   * @param {*} v
   * @returns {string}
   */
  function riskClass(v) {
    if (!isNum(v)) return "na";
    if (v > 0) return "risk-hi";
    if (v < 0) return "risk-lo";
    return "risk-flat";
  }

  /**
   * escape HTML กัน XSS จากค่าที่มาจาก JSON
   * @param {*} s
   * @returns {string}
   */
  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /**
   * แปลง ISO timestamp เป็นข้อความสั้นอ่านง่าย
   * @param {string} iso
   * @param {boolean} withTime
   * @returns {string}
   */
  function fmtStamp(iso, withTime) {
    if (!iso) return "—";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso).slice(0, 16).replace("T", " ");
    var date = d.getFullYear() + "-" +
      String(d.getMonth() + 1).padStart(2, "0") + "-" +
      String(d.getDate()).padStart(2, "0");
    if (!withTime) return date;
    return date + " " + String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }

  /**
   * บอกว่าผ่านมานานเท่าไหร่แล้ว
   * @param {string} iso
   * @returns {string}
   */
  function timeAgo(iso) {
    if (!iso) return "";
    var then = new Date(iso).getTime();
    if (isNaN(then)) return "";
    var mins = Math.round((Date.now() - then) / 60000);
    if (mins < 1) return "เมื่อครู่";
    if (mins < 60) return mins + " นาทีที่แล้ว";
    var hrs = Math.round(mins / 60);
    if (hrs < 24) return hrs + " ชม.ที่แล้ว";
    return Math.round(hrs / 24) + " วันที่แล้ว";
  }

  /** แสดง toast แจ้งเตือน */
  function toast(msg, isError) {
    var el = $("toast");
    if (!el) return;
    el.innerHTML = esc(msg);
    el.style.borderColor = isError ? "var(--unwind)" : "var(--border)";
    el.hidden = false;
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.hidden = true; }, 6500);
  }

  /* ============================== FETCH ============================== */

  /**
   * ดึง JSON พร้อม cache-busting (GitHub Pages cache แรง)
   * @param {string} name - ชื่อไฟล์ เช่น "latest.json"
   * @returns {Promise<Object>}
   */
  function fetchJSON(name) {
    var url = dataBase + name + "?t=" + Date.now();
    return fetch(url, { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error(name + " → HTTP " + r.status);
      return r.json();
    });
  }

  /**
   * หา base path ของโฟลเดอร์ data/ ที่ใช้ได้จริง (ลองทีละอัน)
   * @returns {Promise<string>}
   */
  function resolveDataBase() {
    if (dataBase) return Promise.resolve(dataBase);

    var attempt = function (i) {
      if (i >= DATA_BASES.length) {
        return Promise.reject(new Error(
          "หา data/latest.json ไม่เจอ — ลองรัน `python -m http.server` จาก root ของ repo"
        ));
      }
      var base = DATA_BASES[i];
      return fetch(base + "latest.json?probe=" + Date.now(), { cache: "no-store" })
        .then(function (r) {
          if (!r.ok) throw new Error("no");
          dataBase = base;
          return base;
        })
        .catch(function () { return attempt(i + 1); });
    };
    return attempt(0);
  }

  /**
   * โหลดข้อมูลทั้งหมด — latest.json บังคับ ที่เหลือขาดได้ (degrade แบบสุภาพ)
   * @returns {Promise<void>}
   */
  function loadAll() {
    return resolveDataBase()
      .then(function () {
        return Promise.all([
          fetchJSON("latest.json"),
          fetchJSON("cusi_history.json").catch(function () { return null; }),
          fetchJSON("indicators_history.json").catch(function () { return null; }),
          fetchJSON("cot_history.json").catch(function () { return null; })
        ]);
      })
      .then(function (res) {
        state.latest = res[0];
        state.cusiHistory = res[1];
        state.indicatorsHistory = res[2];
        state.cotHistory = res[3];
        renderAll();
      });
  }

  /* ============================== GAUGE ============================== */

  var GAUGE = { cx: 160, cy: 160, rOuter: 128, rInner: 104 };

  /**
   * แปลงค่า 0–100 เป็นพิกัดบนครึ่งวงกลม
   * 0 = ซ้ายสุด, 50 = บนสุด, 100 = ขวาสุด
   * @param {number} value
   * @param {number} radius
   * @returns {{x: number, y: number}}
   */
  function polar(value, radius) {
    var deg = 180 + (Math.max(0, Math.min(100, value)) / 100) * 180;
    var rad = (deg * Math.PI) / 180;
    return { x: GAUGE.cx + radius * Math.cos(rad), y: GAUGE.cy + radius * Math.sin(rad) };
  }

  /**
   * สร้าง path ของแถบวงแหวน (annulus sector) จาก v1 ถึง v2
   * @param {number} v1
   * @param {number} v2
   * @returns {string} SVG path d
   */
  function bandPath(v1, v2) {
    var o1 = polar(v1, GAUGE.rOuter), o2 = polar(v2, GAUGE.rOuter);
    var i2 = polar(v2, GAUGE.rInner), i1 = polar(v1, GAUGE.rInner);
    var large = (v2 - v1) > 50 ? 1 : 0;
    return [
      "M", o1.x, o1.y,
      "A", GAUGE.rOuter, GAUGE.rOuter, 0, large, 1, o2.x, o2.y,
      "L", i2.x, i2.y,
      "A", GAUGE.rInner, GAUGE.rInner, 0, large, 0, i1.x, i1.y,
      "Z"
    ].join(" ");
  }

  /** วาดแถบสี 4 ระดับ + ขีดบอกค่า (ทำครั้งเดียวตอนโหลด) */
  function drawGaugeBands() {
    var levels = getLevels();
    var bands = $("gauge-bands");
    var ticks = $("gauge-ticks");
    if (!bands || !ticks) return;

    var svgNS = "http://www.w3.org/2000/svg";
    bands.innerHTML = "";
    ticks.innerHTML = "";

    var lower = 0;
    levels.forEach(function (lv) {
      var upper = Math.min(lv.max, 100);
      var p = document.createElementNS(svgNS, "path");
      p.setAttribute("d", bandPath(lower, upper));
      p.setAttribute("fill", lv.color);
      p.setAttribute("opacity", "0.32");
      p.setAttribute("data-level", lv.name);
      bands.appendChild(p);

      // ขีดแบ่ง + ตัวเลขที่ขอบล่างของระดับ
      if (lower > 0) {
        var a = polar(lower, GAUGE.rInner - 3);
        var b = polar(lower, GAUGE.rOuter + 3);
        var line = document.createElementNS(svgNS, "line");
        line.setAttribute("x1", a.x); line.setAttribute("y1", a.y);
        line.setAttribute("x2", b.x); line.setAttribute("y2", b.y);
        line.setAttribute("stroke", "#0d1117");
        line.setAttribute("stroke-width", "2");
        ticks.appendChild(line);

        var t = polar(lower, GAUGE.rOuter + 15);
        var txt = document.createElementNS(svgNS, "text");
        txt.setAttribute("x", t.x); txt.setAttribute("y", t.y);
        txt.setAttribute("fill", "#6e7681");
        txt.setAttribute("font-size", "10.5");
        txt.setAttribute("font-family", "IBM Plex Mono, monospace");
        txt.setAttribute("text-anchor", "middle");
        txt.setAttribute("dominant-baseline", "middle");
        txt.textContent = String(lower);
        ticks.appendChild(txt);
      }
      lower = upper;
    });

    // ป้าย 0 และ 100 ที่ปลายทั้งสองข้าง
    [[0, "0"], [100, "100"]].forEach(function (pair) {
      var pt = polar(pair[0], GAUGE.rOuter + 15);
      var txt = document.createElementNS(svgNS, "text");
      txt.setAttribute("x", pt.x); txt.setAttribute("y", pt.y);
      txt.setAttribute("fill", "#6e7681");
      txt.setAttribute("font-size", "10.5");
      txt.setAttribute("font-family", "IBM Plex Mono, monospace");
      txt.setAttribute("text-anchor", "middle");
      txt.setAttribute("dominant-baseline", "middle");
      txt.textContent = pair[1];
      ticks.appendChild(txt);
    });
  }

  /**
   * อ่านนิยามระดับจาก cusi_history.json (fallback เป็นค่าคงที่)
   * @returns {Array<{name: string, max: number, color: string}>}
   */
  function getLevels() {
    if (state.cusiHistory && Array.isArray(state.cusiHistory.levels) && state.cusiHistory.levels.length) {
      return state.cusiHistory.levels;
    }
    return FALLBACK_LEVELS;
  }

  /**
   * หา metadata ของระดับจากชื่อ
   * @param {string} name
   * @returns {{name: string, max: number, color: string}}
   */
  function levelMeta(name) {
    var found = getLevels().filter(function (l) { return l.name === name; })[0];
    return found || { name: name || "—", color: "#8b949e", max: 100 };
  }

  /** อัพเดทเข็มและตัวเลขบน gauge */
  function renderGauge() {
    var cusi = state.latest && state.latest.cusi;
    var valueEl = $("cusi-value");
    var levelEl = $("cusi-level");
    var changeEl = $("cusi-change");
    var needle = $("needle-line");

    if (!cusi || !isNum(cusi.value)) {
      if (valueEl) { valueEl.textContent = "—"; valueEl.style.color = "var(--text-muted)"; }
      if (levelEl) { levelEl.textContent = "ไม่มีข้อมูล"; levelEl.style.color = "var(--text-dim)"; }
      if (changeEl) changeEl.textContent = "Δ1d —";
      if (needle) needle.style.transform = "rotate(-90deg)";
      return;
    }

    var meta = levelMeta(cusi.level);
    var color = cusi.level_color || meta.color;

    if (valueEl) {
      valueEl.textContent = cusi.value.toFixed(1);
      valueEl.style.color = color;
    }
    if (levelEl) {
      levelEl.textContent = (cusi.level_emoji ? cusi.level_emoji + " " : "") + (cusi.level || "");
      levelEl.style.color = color;
    }
    if (changeEl) {
      var d = cusi.change_1d;
      // CUSI สูงขึ้น = เสี่ยงขึ้น → ใช้ riskClass ไม่ใช่ signClass
      changeEl.innerHTML = "Δ1d <span class='" + riskClass(d) + "'>" + fmtSigned(d, 1) + "</span>" +
        (cusi.as_of ? " <span class='na'>· " + esc(cusi.as_of) + "</span>" : "");
    }
    if (needle) {
      // เข็มวาดชี้ขึ้นเป็นค่าเริ่มต้น → หมุน -90° ที่ค่า 0, +90° ที่ค่า 100
      var angle = (Math.max(0, Math.min(100, cusi.value)) / 100) * 180 - 90;
      needle.style.transform = "rotate(" + angle + "deg)";
      needle.setAttribute("stroke", color);
    }
  }

  /** Phase 6D: แสดง CUSI-Lite (1h) ใต้ gauge หลัก */
  function renderLite() {
    var el = $("cusi-lite");
    if (!el) return;
    var lite = state.latest && state.latest.cusi_lite;
    if (!lite || !lite.available) {
      // ไม่มีข้อมูล/ค้างทั้งหมด → ซ่อน (ไม่โชว์ตัวเลขเก่าให้เข้าใจผิด)
      el.hidden = true;
      return;
    }
    var color = lite.level_color || "var(--text-muted)";
    var staleNote = (lite.stale && lite.stale.length)
      ? " · <span class='na'>ตัด " + lite.stale.length + " ตัวที่ค้าง</span>" : "";
    var t = lite.as_of ? new Date(lite.as_of) : null;
    var tstr = t && !isNaN(t) ? String(t.getHours()).padStart(2, "0") + ":" +
               String(t.getMinutes()).padStart(2, "0") : "";
    el.innerHTML = "CUSI-Lite (1h): <span class='lite-val' style='color:" + color + "'>" +
      lite.value.toFixed(1) + " " + esc(lite.level) + "</span>" +
      (tstr ? " <span class='na'>· แท่ง " + tstr + "</span>" : "") + staleNote;
    el.title = "mini-CUSI จากแท่ง 1 ชั่วโมง (JPY momentum / VIX / FX vol) — " +
      "สเกลเดียวกับ CUSI หลัก · coverage " + Math.round((lite.weight_coverage || 0) * 100) + "%";
    el.hidden = false;
  }

  /* ========================== COMPONENT BARS ========================== */

  /** วาด contribution bars (แนวนอน เรียงจากมากไปน้อย) */
  function renderComponents() {
    var host = $("components-list");
    if (!host) return;

    var cusi = state.latest && state.latest.cusi;
    var comps = cusi && cusi.components;

    if (!comps || !Object.keys(comps).length) {
      host.innerHTML = "<p class='empty-state'>ไม่มีข้อมูล component</p>";
      return;
    }

    // latest.json เรียงมาให้แล้ว แต่ JSON object order ไม่รับประกัน → เรียงซ้ำ
    var rows = Object.keys(comps).map(function (k) {
      return { key: k, z: comps[k].z, weight: comps[k].weight, contribution: comps[k].contribution };
    }).sort(function (a, b) { return b.contribution - a.contribution; });

    var maxAbs = Math.max.apply(null, rows.map(function (r) {
      return isNum(r.contribution) ? Math.abs(r.contribution) : 0;
    }).concat([0.01]));

    host.innerHTML = rows.map(function (r) {
      var pos = r.contribution >= 0;
      // ครึ่งซ้าย = ลบ, ครึ่งขวา = บวก; ความกว้างเทียบกับ contribution สูงสุด
      var pct = Math.abs(r.contribution) / maxAbs * 50;
      var style = pos
        ? "left:50%;width:" + pct.toFixed(1) + "%"
        : "left:" + (50 - pct).toFixed(1) + "%;width:" + pct.toFixed(1) + "%";
      var label = COMPONENT_LABELS[r.key] || r.key;

      return "" +
        "<div class='comp' title='" + esc(label) + " · z=" + fmt(r.z, 2) +
        " · weight=" + fmt(r.weight, 2) + " · contribution=" + fmt(r.contribution, 3) + "'>" +
          "<span class='comp__label'>" + esc(label) + "</span>" +
          "<span class='comp__z " + riskClass(r.z) + "'>" + fmtSigned(r.z, 2) + "</span>" +
          "<div class='comp__track'>" +
            "<div class='comp__fill comp__fill--" + (pos ? "pos" : "neg") + "' style='" + style + "'></div>" +
          "</div>" +
        "</div>";
    }).join("");

    // เตือนถ้า component หายไปจาก 9 ตัว (เช่น MOVE stale → re-normalize)
    var missing = Object.keys(COMPONENT_LABELS).filter(function (k) { return !(k in comps); });
    if (missing.length) {
      host.innerHTML += "<p class='card__foot'>ขาด " + missing.length + " component (" +
        missing.map(function (m) { return esc(COMPONENT_LABELS[m] || m); }).join(", ") +
        ") — ระบบ re-normalize น้ำหนักที่เหลือให้แล้ว</p>";
    }
  }

  /* ========================== INDICATORS TABLE ========================== */

  /** ตัวสร้างคอลัมน์ "เพิ่มเติม" แบบต่าง ๆ */
  function zExtra(field) {
    return function (d) {
      return isNum(d[field]) ? "<span class='pill'>z " + fmtSigned(d[field], 2) + "</span>" : "";
    };
  }
  function ddExtra() {
    return function (d) {
      var v = d.dd_from_20d_high_pct;
      return isNum(v) ? "<span class='" + signClass(v) + "'>" + v.toFixed(2) + "% จาก 20d high</span>" : "";
    };
  }
  function pctileExtra(field) {
    return function (d) {
      return isNum(d[field]) ? "<span class='pill'>P" + Math.round(d[field]) + " (3y)</span>" : "";
    };
  }
  function sourceExtra() {
    return function (d) {
      if (!d.source) return "";
      var proxy = String(d.source).indexOf("proxy") >= 0;
      return "<span class='pill" + (proxy ? " pill--warn" : "") + "'>" + esc(d.source) + "</span>";
    };
  }
  function fxVolExtra() {
    return function (d) {
      var bits = [];
      if (isNum(d.atr_pct_14)) bits.push("ATR " + d.atr_pct_14.toFixed(2) + "%");
      if (isNum(d.percentile_3y)) bits.push("<span class='pill'>P" + Math.round(d.percentile_3y) + " (3y)</span>");
      return bits.join(" · ");
    };
  }
  function impliedExtra() {
    return function (d) {
      var bits = [];
      if (isNum(d.rr_25d_pct)) {
        // RR ติดลบ = ตลาดจ่ายแพง hedge เยนแข็ง = สัญญาณเสี่ยง → สีร้อน
        bits.push("<span class='pill'>RR25Δ <span class='" + riskClass(-d.rr_25d_pct) + "'>" +
                  fmtSigned(d.rr_25d_pct, 2) + "</span></span>");
      }
      if (d.quality && d.quality !== "ok") {
        bits.push("<span class='pill pill--warn'>" + esc(d.quality) + "</span>");
      }
      if (d.expiry) bits.push("<span class='na'>exp " + esc(d.expiry) + "</span>");
      return bits.join(" ");
    };
  }

  function cotExtra() {
    return function (d) {
      var bits = [];
      if (isNum(d.z)) bits.push("<span class='pill'>z " + fmtSigned(d.z, 2) + "</span>");
      if (d.report_date) bits.push("<span class='na'>" + esc(d.report_date) + "</span>");
      return bits.join(" ");
    };
  }

  /** วาดตาราง indicators */
  function renderTable() {
    var body = $("indicators-body");
    if (!body) return;

    var ind = state.latest && state.latest.indicators;
    if (!ind) {
      body.innerHTML = "<tr><td colspan='5' class='td-empty'>ไม่มีข้อมูล</td></tr>";
      return;
    }

    var stale = (state.latest.data_quality && state.latest.data_quality.stale) || [];

    var html = INDICATOR_ROWS.map(function (row) {
      var d = ind[row.key];
      if (!d) return "";

      var value = isNum(d[row.valueKey || "last"]) ? d[row.valueKey || "last"] : null;
      var isStale = stale.indexOf(row.key) >= 0;

      // Δ1d
      var c1 = row.d1 ? d[row.d1] : null;
      var c1html = row.d1
        ? "<span class='" + signClass(c1) + "'>" + fmtSigned(c1, 2, "%") + "</span>"
        : "<span class='na'>—</span>";

      // Δ5d — เป็น % หรือ bps หรือ point แล้วแต่ชนิดข้อมูล
      var c5html;
      if (row.bps) {
        var b = d[row.bps];
        c5html = "<span class='" + signClass(b) + "'>" + fmtSigned(b, 1, " bps") + "</span>";
        if (row.bpsLabel) c5html += " <span class='na'>(" + row.bpsLabel + ")</span>";
      } else if (row.d5raw) {
        c5html = "<span class='" + signClass(d[row.d5raw]) + "'>" + fmtSigned(d[row.d5raw], 2) + "</span>";
      } else if (row.d5) {
        c5html = "<span class='" + signClass(d[row.d5]) + "'>" + fmtSigned(d[row.d5], 2, "%") + "</span>";
      } else {
        c5html = "<span class='na'>—</span>";
      }

      var extra = row.extra ? row.extra(d) : "";
      if (isStale) extra = "<span class='pill pill--warn'>stale</span> " + extra;

      return "" +
        "<tr>" +
          "<td><span class='td-name'>" +
            "<span class='td-name__main'>" + esc(row.label) + "</span>" +
            "<span class='td-name__sub'>" + esc(row.sub) + "</span>" +
          "</span></td>" +
          "<td class='ta-r td-num'>" + fmt(value, row.dec) + "</td>" +
          "<td class='ta-r td-num'>" + c1html + "</td>" +
          "<td class='ta-r td-num'>" + c5html + "</td>" +
          "<td class='ta-r'>" + extra + "</td>" +
        "</tr>";
    }).join("");

    body.innerHTML = html || "<tr><td colspan='5' class='td-empty'>ไม่มีข้อมูล</td></tr>";
  }

  /* ============================== ALERTS ============================== */

  /** วาด alert log */
  function renderAlerts() {
    var host = $("alerts-list");
    if (!host) return;

    var alerts = (state.latest && state.latest.alerts) || [];
    if (!alerts.length) {
      host.innerHTML = "<p class='empty-state'>ยังไม่มี alert — ระดับ CUSI ยังไม่เปลี่ยน<br>" +
        "<span style='font-size:11px'>(ระบบส่ง alert จะเริ่มทำงานใน Phase 5)</span></p>";
      return;
    }

    host.innerHTML = alerts.slice().reverse().map(function (a) {
      var to = a.to || "";
      var title = a.type === "level_change"
        ? "เปลี่ยนระดับ " + esc(a.from || "?") + " → " + esc(to)
        : esc(a.type || "alert");
      return "" +
        "<div class='alert alert--" + esc(to) + "'>" +
          "<div class='alert__head'>" +
            "<span class='alert__type'>" + title + "</span>" +
            "<span class='alert__ts'>" + esc(fmtStamp(a.ts, true)) + "</span>" +
          "</div>" +
          (a.reason ? "<p class='alert__reason'>" + esc(a.reason) + "</p>" : "") +
        "</div>";
    }).join("");
  }

  /* ========================== EVENTS (Phase 6C) ========================== */

  /** ชื่อสั้นของ event แต่ละประเภทบนแถบ */
  var EVENT_SHORT = { boj_mpm: "BOJ MPM", fomc: "FOMC", jp_cpi: "JP CPI" };

  /**
   * จำนวนวันจากวันนี้ (เที่ยงคืน local) ถึงวันที่ ISO — คำนวณฝั่ง client
   * เพื่อให้ countdown สดเสมอแม้ latest.json จะสร้างไว้หลายชั่วโมงก่อน
   * @param {string} iso - "YYYY-MM-DD"
   * @returns {number|null}
   */
  function daysFromToday(iso) {
    if (!iso) return null;
    var parts = String(iso).split("-");
    if (parts.length !== 3) return null;
    var target = new Date(+parts[0], +parts[1] - 1, +parts[2]);
    var now = new Date();
    var today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    return Math.round((target - today) / 86400000);
  }

  /**
   * ข้อความ countdown ภาษาไทย
   * @param {number|null} d - จำนวนวัน
   * @returns {string}
   */
  function countdownText(d) {
    if (d === null) return "";
    if (d < 0) return "ผ่านแล้ว";
    if (d === 0) return "วันนี้";
    if (d === 1) return "พรุ่งนี้";
    return "อีก " + d + " วัน";
  }

  /** วาดแถบเหตุการณ์ + ธง event risk (จาก latest.events — field ใหม่ Phase 6C) */
  function renderEvents() {
    var bar = $("events-bar");
    if (!bar) return;

    var ev = state.latest && state.latest.events;
    // ผู้อ่านเก่า/ข้อมูลเก่าที่ไม่มี field นี้ → ซ่อนแถบเฉย ๆ (backward compatible)
    if (!ev || !Array.isArray(ev.upcoming) || !ev.upcoming.length) {
      bar.hidden = true;
      return;
    }

    var windowDays = isNum(ev.risk_window_days) ? ev.risk_window_days : 3;
    var riskKeys = {};
    (ev.risk_events || []).forEach(function (r) { riskKeys[r.type + ":" + r.date] = true; });

    var html = "";
    // ธงใหญ่เมื่อ risk ทำงาน — คำนวณซ้ำฝั่ง client จากวันเริ่มประชุม
    // (server คำนวณไว้ตอน generate ซึ่งอาจข้ามเที่ยงคืนไปแล้ว)
    var anyRisk = false;

    var chips = ev.upcoming.map(function (e) {
      var toStart = daysFromToday(e.start_date || e.date);
      var toEnd = daysFromToday(e.date);
      if (toEnd !== null && toEnd < 0) return "";        // จบไปแล้วระหว่างวัน

      var isMeeting = e.type === "boj_mpm" || e.type === "fomc";
      var inWindow = isMeeting && toStart !== null && toEnd !== null &&
                     toStart <= windowDays && toEnd >= 0;
      var serverSaysRisk = !!riskKeys[e.type + ":" + e.date];
      var risk = inWindow || serverSaysRisk;
      if (risk) anyRisk = true;

      var cls = "evt" + (risk ? " evt--risk" : "") +
                (toStart === 0 || toEnd === 0 ? " evt--today" : "");
      var count = countdownText(toStart !== null && toStart >= 0 ? toStart : toEnd);

      return "<span class='" + cls + "' title='" + esc(e.label) + " · " + esc(e.date) +
             (e.approx ? " (วันที่โดยประมาณ)" : "") + "'>" +
             "<span class='evt__label'>" + esc(EVENT_SHORT[e.type] || e.label) + "</span>" +
             "<span class='evt__count'>" + esc(count) + "</span>" +
             (e.approx ? "<span class='evt__approx'>≈</span>" : "") +
             "</span>";
    }).join("");

    if (anyRisk) {
      html += "<span class='evt evt--flag'>⚠️ EVENT RISK — มีการประชุมนโยบายใน " +
              windowDays + " วัน</span>";
    }
    html += chips;

    bar.innerHTML = html;
    bar.hidden = !html;
  }

  /* ========================== HEADER / QUALITY ========================== */

  /** อัพเดท timestamp + ธงคุณภาพข้อมูล */
  function renderHeader() {
    var latest = state.latest;
    if (!latest) return;

    var asOf = $("data-as-of");
    var gen = $("generated-at");
    if (asOf) asOf.textContent = fmtStamp(latest.data_as_of, false);
    if (gen) gen.textContent = fmtStamp(latest.generated_at, true) + " (" + timeAgo(latest.generated_at) + ")";

    var bar = $("quality-bar");
    if (!bar) return;

    var dq = latest.data_quality || {};
    var flags = [];

    if (Array.isArray(dq.missing) && dq.missing.length) {
      flags.push("<span class='flag flag--danger'>⛔ ข้อมูลขาด: " + esc(dq.missing.join(", ")) + "</span>");
    }
    if (Array.isArray(dq.stale) && dq.stale.length) {
      flags.push("<span class='flag flag--warn'>⚠️ ข้อมูลค้าง: " + esc(dq.stale.join(", ")) + "</span>");
    }
    if (dq.jgb_is_proxy) {
      flags.push("<span class='flag flag--warn'>⚠️ JGB ใช้ค่า proxy (" + esc(dq.jgb_source || "") + ") — บอกทิศทางเท่านั้น</span>");
    }
    if (isNum(dq.tickers_ok) && isNum(dq.tickers_total) && dq.tickers_ok < dq.tickers_total) {
      flags.push("<span class='flag flag--warn'>ดึงราคาได้ " + dq.tickers_ok + "/" + dq.tickers_total + " ตัว</span>");
    }
    // เตือนถ้าข้อมูลเก่ากว่า 2 วัน
    var asOfTime = new Date(latest.data_as_of).getTime();
    if (!isNaN(asOfTime) && (Date.now() - asOfTime) > 48 * 3600 * 1000) {
      flags.push("<span class='flag flag--danger'>⛔ ข้อมูลเก่ากว่า 48 ชม. — pipeline อาจหยุดทำงาน</span>");
    }

    bar.innerHTML = flags.join("");
    bar.hidden = flags.length === 0;
  }

  /* ============================== CHARTS ============================== */

  /** วาด/อัพเดทกราฟทั้งหมด */
  function renderCharts() {
    if (!window.YCMCharts || !window.YCMCharts.available()) {
      showChartFallback();
      return;
    }

    // CUSI history
    if (state.cusiHistory) {
      window.YCMCharts.renderCusiHistory(state.cusiHistory, state.range, EVENTS);
      var rows = state.cusiHistory.history || [];
      var shown = state.range === "all" ? rows.length : Math.min(rows.length, state.range);
      var foot = $("cusi-history-foot");
      if (foot && rows.length) {
        var first = rows[Math.max(0, rows.length - shown)];
        foot.textContent = "แสดง " + shown + " วันทำการ (" + first.date + " → " +
          rows[rows.length - 1].date + ") · ทั้งหมด " + rows.length + " วัน";
      }
    }

    var hist = state.indicatorsHistory;

    // spread
    if (hist) {
      window.YCMCharts.renderSpread(hist);
      var sp = state.latest && state.latest.indicators && state.latest.indicators.spread_us_jp;
      var spBadge = $("spread-badge");
      if (spBadge && sp) spBadge.textContent = fmt(sp.last, 3) + "%";
      var spFoot = $("spread-foot");
      if (spFoot) {
        spFoot.textContent = "หน้าต่าง " + (hist.window_days || hist.dates.length) +
          " วัน · Δ20d " + (sp && isNum(sp.chg_20d_bps) ? fmtSigned(sp.chg_20d_bps, 1, " bps") : "—") +
          " · spread แคบลงเร็ว = สัญญาณเตือนอันดับต้น";
      }

      // FX vol
      var fx = window.YCMCharts.renderFxVol(hist);
      var fxData = state.latest && state.latest.indicators && state.latest.indicators.fx_vol;
      var fxBadge = $("fxvol-badge");
      if (fxBadge && fxData) fxBadge.textContent = fmt(fxData.realized_10d, 2) + "%";
      var fxFoot = $("fxvol-foot");
      if (fxFoot) {
        fxFoot.textContent = "เส้นประ = P80 ของหน้าต่างที่แสดง (" +
          (fx.p80 === null ? "—" : fx.p80.toFixed(2) + "%") + ") · percentile 3 ปี = " +
          (fxData && isNum(fxData.percentile_3y) ? Math.round(fxData.percentile_3y) : "—");
      }

      // MOVE vs VIX
      window.YCMCharts.renderMoveVix(hist);
      var vix = state.latest && state.latest.indicators && state.latest.indicators.vix;
      var mv = state.latest && state.latest.indicators && state.latest.indicators.move;
      var mvBadge = $("movevix-badge");
      if (mvBadge) mvBadge.textContent = "MOVE " + fmt(mv && mv.last, 1) + " · VIX " + fmt(vix && vix.last, 1);
      var mvFoot = $("movevix-foot");
      if (mvFoot) mvFoot.textContent = "bond vol (ซ้าย) มักนำ FX vol · VIX (ขวา)";
    }

    // COT
    if (state.cotHistory) {
      window.YCMCharts.renderCot(state.cotHistory, 104);
      var cot = state.latest && state.latest.indicators && state.latest.indicators.cot_jpy;
      var cotBadge = $("cot-badge");
      if (cotBadge && cot && isNum(cot.net_position)) {
        cotBadge.textContent = cot.net_position.toLocaleString("en-US");
      }
      var cotFoot = $("cot-foot");
      if (cotFoot && cot) {
        cotFoot.textContent = "แดง = net short (เชื้อเพลิง unwind) · รายงาน " +
          esc(cot.report_date || "—") +
          (cot.available_from ? " · ใช้ได้ตั้งแต่ " + esc(cot.available_from) : "");
      }
    }
  }

  /** ถ้า Chart.js โหลดไม่ได้ (ออฟไลน์/CDN ถูกบล็อก) ให้บอกผู้ใช้ตรง ๆ */
  function showChartFallback() {
    ["chart-cusi", "chart-spread", "chart-fxvol", "chart-cot", "chart-movevix"].forEach(function (id) {
      var el = $(id);
      if (el && el.parentElement) {
        el.parentElement.innerHTML =
          "<div class='tv-cell__fallback'>โหลด Chart.js จาก CDN ไม่ได้<br>" +
          "<span style='font-size:10.5px'>ตรวจสอบการเชื่อมต่ออินเทอร์เน็ต</span></div>";
      }
    });
  }

  /* ========================== TRADINGVIEW ========================== */

  /**
   * สร้าง TradingView mini-chart widget หนึ่งตัว
   * innerHTML ไม่รัน <script> → ต้อง createElement เอง
   * @param {{symbol: string, label: string}} item
   * @returns {HTMLElement}
   */
  function makeTvCell(item) {
    var cell = document.createElement("div");
    cell.className = "tv-cell";

    var label = document.createElement("span");
    label.className = "tv-cell__label";
    label.textContent = item.label;
    cell.appendChild(label);

    var fallback = document.createElement("div");
    fallback.className = "tv-cell__fallback";
    fallback.innerHTML = "โหลด widget ไม่ได้<br><span style='font-size:10.5px'>" +
      esc(item.symbol) + "</span>";
    cell.appendChild(fallback);

    var container = document.createElement("div");
    container.className = "tradingview-widget-container";
    container.style.height = "100%";

    var widget = document.createElement("div");
    widget.className = "tradingview-widget-container__widget";
    widget.style.height = "100%";
    container.appendChild(widget);

    var script = document.createElement("script");
    script.type = "text/javascript";
    script.async = true;
    script.src = "https://s3.tradingview.com/external-embedding/embed-widget-mini-symbol-overview.js";
    script.text = JSON.stringify({
      symbol: item.symbol,
      width: "100%",
      height: "100%",
      locale: "en",
      dateRange: "3M",
      colorTheme: "dark",
      isTransparent: true,
      autosize: true,
      largeChartUrl: "",
      chartOnly: false,
      noTimeScale: false
    });
    container.appendChild(script);
    cell.appendChild(container);

    // ถ้า 6 วินาทีแล้วยังไม่มี iframe ให้คง fallback ไว้
    setTimeout(function () {
      if (cell.querySelector("iframe")) fallback.remove();
    }, 6000);

    return cell;
  }

  /** ฉีด widget ทั้งหมดลง grid (ทำครั้งเดียว — ไม่ reload ตอน refresh) */
  function renderTradingView() {
    var grid = $("tv-grid");
    if (!grid || grid.dataset.ready === "1") return;
    TV_SYMBOLS.forEach(function (item) { grid.appendChild(makeTvCell(item)); });
    grid.dataset.ready = "1";
  }

  /* ========================== METHODOLOGY ========================== */

  /** เติมตารางน้ำหนัก + ระดับใน methodology (อ่านจากข้อมูลจริง ไม่ hardcode) */
  function renderMethodology() {
    var weightsEl = $("methodology-weights");
    var levelsEl = $("methodology-levels");

    if (weightsEl) {
      var cusi = state.latest && state.latest.cusi;
      // ใช้ cusi.weights (ครบ 9 ตัวเสมอ) ถ้ามี — ไม่งั้นถอยไปใช้ components
      // ซึ่งอาจขาดตัวที่ข้อมูล stale ในวันนั้น
      var w = (cusi && cusi.weights) || null;
      if (!w && cusi && cusi.components) {
        w = {};
        Object.keys(cusi.components).forEach(function (k) { w[k] = cusi.components[k].weight; });
      }
      if (w && Object.keys(w).length) {
        var active = (cusi && cusi.components) || {};
        weightsEl.innerHTML = Object.keys(w)
          .sort(function (a, b) { return w[b] - w[a]; })
          .map(function (k) {
            var off = !(k in active);
            return "<li><span>" + esc(COMPONENT_LABELS[k] || k) +
              (off ? " <span class='na'>(ไม่มีข้อมูลวันนี้)</span>" : "") +
              "</span><span>" + (w[k] * 100).toFixed(0) + "%</span></li>";
          }).join("");
      } else {
        weightsEl.innerHTML = "<li>ไม่มีข้อมูล</li>";
      }
    }

    if (levelsEl) {
      var levels = getLevels();
      var lower = 0;
      levelsEl.innerHTML = levels.map(function (lv) {
        var upper = Math.min(lv.max, 100);
        var row = "<li><span><span class='swatch' style='background:" + esc(lv.color) + "'></span>" +
          esc(lv.name) + "</span><span>" + lower + "–" + upper + "</span></li>";
        lower = upper;
        return row;
      }).join("");
    }
  }

  /* ============================== RENDER ============================== */

  /** วาดทุกอย่างจาก state ปัจจุบัน */
  function renderAll() {
    renderHeader();
    renderEvents();
    drawGaugeBands();
    renderLite();
    renderGauge();
    renderComponents();
    renderTable();
    renderAlerts();
    renderMethodology();
    renderCharts();
  }

  /**
   * โหลดข้อมูลใหม่แล้ววาดซ้ำ
   * @param {boolean} manual - ผู้ใช้กดปุ่มเองหรือไม่ (แสดง toast ตอนสำเร็จ)
   */
  function refresh(manual) {
    var btn = $("refresh-btn");
    if (btn) btn.classList.add("is-busy");
    document.body.classList.add("is-loading");

    return loadAll()
      .then(function () {
        if (manual) toast("อัพเดทข้อมูลแล้ว");
      })
      .catch(function (err) {
        console.error("[YCM] refresh failed:", err);
        toast("โหลดข้อมูลไม่สำเร็จ: " + err.message, true);
      })
      .then(function () {
        if (btn) btn.classList.remove("is-busy");
        document.body.classList.remove("is-loading");
      });
  }

  /* ============================== INIT ============================== */

  function init() {
    renderTradingView();

    // ปุ่มเลือกช่วงเวลาของกราฟ CUSI
    var toolbar = $("cusi-range-toolbar");
    if (toolbar) {
      toolbar.addEventListener("click", function (e) {
        var btn = e.target.closest("[data-range]");
        if (!btn) return;
        var raw = btn.dataset.range;
        state.range = raw === "all" ? "all" : parseInt(raw, 10);
        toolbar.querySelectorAll(".chip").forEach(function (c) { c.classList.remove("is-active"); });
        btn.classList.add("is-active");
        renderCharts();
      });
    }

    var btn = $("refresh-btn");
    if (btn) btn.addEventListener("click", function () { refresh(true); });

    // auto-refresh ทุก 5 นาที — หยุดเมื่อแท็บถูกซ่อนเพื่อไม่ยิง fetch ทิ้ง
    function startTimer() {
      stopTimer();
      state.timer = setInterval(function () { refresh(false); }, REFRESH_MS);
    }
    function stopTimer() {
      if (state.timer) { clearInterval(state.timer); state.timer = null; }
    }
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) {
        stopTimer();
      } else {
        refresh(false);
        startTimer();
      }
    });

    refresh(false);
    startTimer();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
