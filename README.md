# Yen Carry Monitor

[![Update hourly](https://github.com/kittabunj-tech/yen-carry-monitor/actions/workflows/update_hourly.yml/badge.svg)](https://github.com/kittabunj-tech/yen-carry-monitor/actions/workflows/update_hourly.yml)
[![Update daily](https://github.com/kittabunj-tech/yen-carry-monitor/actions/workflows/update_daily.yml/badge.svg)](https://github.com/kittabunj-tech/yen-carry-monitor/actions/workflows/update_daily.yml)
[![Update weekly COT](https://github.com/kittabunj-tech/yen-carry-monitor/actions/workflows/update_weekly_cot.yml/badge.svg)](https://github.com/kittabunj-tech/yen-carry-monitor/actions/workflows/update_weekly_cot.yml)

> Dashboard เฝ้าระวัง **Yen Carry Trade Unwind** — รวมตัวชี้วัดทั้งหมดเป็นเลขเดียว
> **CUSI** (Carry Unwind Stress Index 0–100) อัพเดทอัตโนมัติ ไม่ต้องดูแล server

🔗 **[เปิด dashboard](https://kittabunj-tech.github.io/yen-carry-monitor/)**

> ⚠️ ข้อมูลเพื่อการศึกษาเท่านั้น ไม่ใช่คำแนะนำการลงทุน

---

## สถาปัตยกรรม

```
┌─────────────────────── GitHub Actions (cron) ────────────────────────┐
│                                                                      │
│  update_hourly    '5 * * * 1-5'    ราคา + intraday → latest.json     │
│  update_daily     '30 23 * * 1-5'  + MOF JGB + cusi_history เต็ม     │
│  update_weekly    '0 2 * * 6'      + CFTC COT                        │
│                            │                                         │
│         yfinance ──────────┤                                         │
│         MOF Japan CSV ─────┼──► pipeline/ ──► data/*.json ──┐        │
│         CFTC Socrata ──────┘      (commit เฉพาะที่เปลี่ยน)    │        │
│                                                              │        │
│                            alert.py ──► Telegram (เมื่อระดับเปลี่ยน)   │
└──────────────────────────────────────────────────────────────┼───────┘
                                                               ▼
┌────────────────────── GitHub Pages (branch: main /) ─────────────────┐
│  index.html (redirect) ──► site/index.html                           │
│    ├── fetch('../data/latest.json')      CUSI + indicators           │
│    ├── Chart.js                          กราฟ 5 ตัว                   │
│    └── TradingView widgets               ราคาสด (client-side)         │
└──────────────────────────────────────────────────────────────────────┘
```

**หลักการแบ่งชั้น** (spec §4.2): ราคาสดเป็นหน้าที่ของ TradingView widgets ฝั่ง client
ส่วน CUSI และ z-score คำนวณฝั่ง pipeline ทุกชั่วโมง — เพราะ cron ของ GitHub
ดีเลย์ 5–20 นาทีเป็นปกติ จะเอามาทำ real-time ไม่ได้

---

## เริ่มใช้งาน

### รันในเครื่อง (Windows, Python 3.11)

```bat
cd dashboard
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

python pipeline\run_all.py --mode weekly    :: ครั้งแรก — ดึง COT ด้วย
python -m http.server 8000
```

เปิด <http://localhost:8000/>

macOS / Linux: `python3.11 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`

### deploy ขึ้น GitHub

ทำตาม **[DEPLOY_CHECKLIST.md](DEPLOY_CHECKLIST.md)** ทีละข้อ (~30–45 นาที)

---

## โหมดการรัน

| คำสั่ง | ใช้เมื่อ | ทำอะไร |
|---|---|---|
| `run_all.py --mode hourly` | ทุกชั่วโมง จ–ศ | ราคา + intraday 1h · JGB/COT จาก cache |
| `run_all.py --mode daily` | หลัง US close | + MOF JGB สด + เขียน `cusi_history.json` ใหม่ |
| `run_all.py --mode weekly` | เสาร์เช้า | + ดึง COT ใหม่จาก CFTC |

**Exit codes** — `0` สำเร็จ · `2` สำเร็จแต่ข้อมูลขาดบางส่วน (ใช้ค่าเก่า + ธง `stale`) · `1` พังจริง
workflow ใช้ `2` เพื่อไม่ให้ badge แดงเวลา API ปลายทางล่มชั่วคราว

---

## โครงสร้าง

```
dashboard/
├── .github/workflows/       hourly · daily · weekly_cot
├── pipeline/
│   ├── config.yaml          ⭐ tickers, weights, windows, thresholds — แก้ที่นี่ที่เดียว
│   ├── config.py            loader + logging
│   ├── fetch_market.py      yfinance batch + retry/backoff + parquet cache
│   ├── fetch_jgb.py         MOF CSV → ETF proxy → cache
│   ├── fetch_cot.py         CFTC Socrata (Legacy, futures-only)
│   ├── indicators.py        pure functions, lookahead-free
│   ├── cusi.py              CUSI engine (§3)
│   ├── build_json.py        ประกอบ JSON ตาม §4.4
│   ├── alert.py             Telegram เมื่อระดับเปลี่ยน
│   └── run_all.py           orchestrator
├── site/                    dashboard (vanilla HTML/CSS/JS + Chart.js)
├── backtest/                validate_cusi.py + VALIDATION_REPORT.md
├── tests/                   154 tests
├── data/                    JSON ที่ commit + raw/ ที่ไม่ commit
├── index.html               redirect ไป site/ (จำเป็นสำหรับ Pages)
└── DEPLOY_CHECKLIST.md
```

---

## CUSI คืออะไร

9 components → composite z-score ถ่วงน้ำหนัก → `CUSI = 50 + (weighted_z / 3) × 50` → 4 ระดับ

| CUSI | ระดับ | ความหมาย |
|---|---|---|
| 0–40 | 🟢 CALM | carry ทำงานปกติ |
| 40–60 | 🟡 WATCH | เริ่มมีแรงตึง — ลด leverage |
| 60–80 | 🟠 STRESS | unwind เริ่มก่อตัว — hedge |
| 80–100 | 🔴 UNWIND | กำลังเกิด — risk-off เต็มรูปแบบ |

### ผล validation (Phase 4)

Backtest **2019-01-01 → 2026-07-17** (1,965 วันทำการ) ด้วยน้ำหนัก v1 ที่ยังไม่เคย tune:

| | |
|---|---|
| Aug 2024 ต้อง ≥ 80 (§3.4) | ✅ **84.1** |
| COVID มี.ค. 2020 | ✅ **95.7** — สูงสุดตลอดช่วง |
| Precision @ 60 | ⚠️ **21%** (8 จาก 38 สัญญาณ) |
| Recall @ 60 | 59% (**91%** ถ้านับเฉพาะเหตุการณ์ที่เยนขยับจริง) |

**อ่านตัวเลขนี้อย่างไร**: ที่เส้น 60 ระบบเตือน ~5 ครั้ง/ปี และส่วนใหญ่ไม่มีอะไรตามมาใน 20 วัน
ใช้เป็น "ให้ระวังตัว" ได้ แต่**ไม่ใช่สัญญาณซื้อขาย** รายละเอียดเต็มใน
[backtest/VALIDATION_REPORT.md](backtest/VALIDATION_REPORT.md)

---

## Event Layer (Phase 6C)

แถบใต้ header แสดง countdown ของ **BOJ MPM / FOMC / CPI ญี่ปุ่น**
และยกธง ⚠️ EVENT RISK ตั้งแต่ 3 วันก่อนเริ่มประชุมจนถึงวันแถลงผล
(ปรับได้ที่ `events.risk_window_days` — CPI แสดงใน countdown แต่ไม่ยกธง)

| แหล่ง | วิธีได้มา |
|---|---|
| BOJ MPM | scrape ตารางหน้า EN ของ BOJ (การประชุมจริง = แถวคู่สองวัน) |
| FOMC | scrape หน้า calendar ของ Fed |
| CPI ญี่ปุ่น | **กติกา "ศุกร์ที่สาม" (≈ โดยประมาณ)** — ปฏิทิน e-stat เป็น JS จึง scrape ไม่ได้ pin วันจริงได้ที่ `events.jp_cpi_overrides` |

ยิง network เฉพาะโหมด daily/weekly · hourly ใช้ cache (`data/events.json`) แต่คำนวณธงใหม่ทุกรอบ
field ใหม่ใน latest.json: `events` (additive — ผู้อ่านเก่าไม่กระทบ)

## CUSI-Lite (Phase 6D)

mini-CUSI จากแท่ง **1 ชั่วโมง** แสดงใต้ gauge หลัก อัพเดททุกชั่วโมงคู่ daily CUSI
ใช้ 3 components ที่มี intraday จริง: JPY momentum · VIX · FX vol
(น้ำหนัก 0.45/0.33/0.22 จากสัดส่วนเดียวกันใน CUSI เต็ม, สเกล/ระดับเดียวกัน)

ข้อออกแบบ: แต่ละ component คำนวณ z บนแท่งของตัวเอง (VIX มี ~6.5 แท่ง/วัน
FX มี ~23 — ไม่ join ข้ามซีรีส์) · component ที่แท่งค้าง >72 ชม. ถูกตัด +
re-normalize · เหลือน้ำหนัก <50% → `available: false` ไม่โชว์ตัวเลขมั่ว
field ใหม่: `cusi_lite` (additive)

## Implied Vol (Phase 6B)

`indicators.fx_implied` ใน latest.json: **1M ATM IV + 25Δ risk reversal** ของ JPY
จาก FXY options (RR ติดลบ = ตลาดจ่ายแพง hedge เยนแข็ง — USDJPY convention)

**ผลประเมินแหล่งข้อมูล** (ทดสอบจริง): Databento = ดีสุดแต่จ่ายเงิน+key ·
CME delayed = โดน Akamai บล็อกบอท+ขัด ToS · **Yahoo FXY options = เลือก**
(ฟรี ไม่มี key แลกกับ chain บาง) — IV คำนวณเองจาก `lastPrice` ด้วย
Black-Scholes inversion เพราะ field IV ของ Yahoo เป็น placeholder นอกเวลาตลาด
(ตรวจแล้วเป็นกับทุก ticker รวม SPY)

**การแทน realized vol ใน CUSI**: กลไกพร้อมแล้วที่ `cusi.fx_vol_source: implied`
แต่ default ยังเป็น `realized` — z window ต้องการ history 252 วัน ส่วน implied
เพิ่งเริ่มออมลง `data/implied_vol_history.json` วันละจุด (สลับก่อนครบ =
z-score จากตัวอย่างจิ๋ว ระบบจึง fallback อัตโนมัติพร้อม log จนกว่าจะครบ ~1 ปี)

## วิธีปรับ weights

> อ่าน [VALIDATION_REPORT.md](backtest/VALIDATION_REPORT.md) หัวข้อ 5b และ 8 **ก่อน**ปรับ
> — ช่วงความเชื่อมั่นของ precision ทุก threshold ทับซ้อนกันหมด แปลว่ายังแยกไม่ออกว่าอะไรดีกว่า

แก้ที่ `pipeline/config.yaml` → `cusi.weights` แล้วรัน:

```bat
python -m pytest tests\test_cusi.py -q          :: ต้องผ่าน — มีเทสต์ว่าน้ำหนักรวม = 1.0
python pipeline\run_all.py --mode daily
python backtest\validate_cusi.py --use-cache    :: ดูว่า Aug 2024 ยังผ่านไหม
```

ระบบ **validate ตอนโหลด**: ถ้ารวมกันไม่เท่า 1.0 หรือมีชื่อ component ที่ไม่รู้จัก จะโยน
`ValueError` ทันทีแทนที่จะคำนวณเงียบ ๆ ผิด ๆ

**ถ้าจะขยับ threshold แทน** (แนะนำมากกว่า) แก้ `cusi.levels[].max` —
เปลี่ยนแค่จุดที่เรียกว่า "เตือน" ไม่ได้แก้ตัวโมเดล และหน้าเว็บอ่านค่าจาก JSON
จึงเปลี่ยนตามเองทั้ง gauge, แถบสีในกราฟ และตาราง methodology

---

## Troubleshooting

<details>
<summary><b>Actions รันแล้วขึ้น 403 ตอน push</b></summary>

Settings → Actions → General → Workflow permissions → เลือก **Read and write permissions** → Save
(DEPLOY_CHECKLIST ข้อ 5)
</details>

<details>
<summary><b>หน้าเว็บขึ้นแต่ตัวเลขเป็น — ทั้งหมด / toast แดง "โหลดข้อมูลไม่สำเร็จ"</b></summary>

เกือบทุกครั้งคือ **Pages ตั้ง source ผิด** ไปที่ Settings → Pages →
Branch ต้องเป็น `main` + folder **`/ (root)`** ไม่ใช่ `/site`

เหตุผล: `site/` ไม่มีโฟลเดอร์ `data/` อยู่ข้างใน ถ้า publish แค่ `site/` หน้าเว็บจะหาข้อมูลไม่เจอ

ตรวจเร็ว ๆ: เปิด `https://kittabunj-tech.github.io/yen-carry-monitor/data/latest.json` ตรง ๆ
ถ้าได้ 404 แปลว่าตั้งผิด
</details>

<details>
<summary><b>ข้อมูลไม่อัพเดท / ไม่มี commit ใหม่</b></summary>

1. แท็บ Actions → workflow ถูก disable หรือเปล่า (GitHub ปิดให้เองถ้า repo เงียบ 60 วัน)
2. cron ดีเลย์ 5–20 นาทีเป็นปกติ ลอง **Run workflow** ด้วยมือดูก่อน
3. ถ้า run เขียว แต่ไม่มี commit = ข้อมูลไม่เปลี่ยนจริง ๆ (นอกเวลาตลาด) — ถูกต้องแล้ว
4. เช็ค `data_as_of` บนหน้าเว็บ ถ้าเก่ากว่า 48 ชม. จะมีธงแดงขึ้นเตือนเอง
</details>

<details>
<summary><b>ธง "ข้อมูลค้าง: move" บนหน้าเว็บ</b></summary>

ปกติ — `^MOVE` บน Yahoo อัพเดทไม่สม่ำเสมอ ระบบตรวจเจอแล้วติดธงให้ และ
**re-normalize น้ำหนัก** ที่เหลือให้อัตโนมัติ (ไม่ได้แทนค่าด้วย z = 0)
CUSI ยังคำนวณต่อได้ตราบใดที่ยังมีน้ำหนักรวม ≥ 50%
</details>

<details>
<summary><b>ธง "JGB ใช้ค่า proxy"</b></summary>

MOF CSV ล่ม ระบบสลับไปใช้ ETF `1482.T` แปลงเป็น yield ด้วย modified duration
**บอกทิศทางได้ แต่ระดับไม่ใช่ค่าจริง** ถ้าขึ้นค้างหลายวันให้เช็คว่า MOF เปลี่ยน URL หรือยัง
(`pipeline/config.yaml` → `jgb.mof_history_url`)
</details>

<details>
<summary><b>Telegram ไม่เข้า</b></summary>

1. ทดสอบก่อน: `python pipeline\alert.py --test`
2. ยังไม่ได้ทัก bot ก่อน? Telegram ห้าม bot ทักก่อน — ต้องส่งข้อความหา bot ก่อน 1 ครั้ง
3. ระบบส่งเฉพาะตอน**ระดับเปลี่ยน** ไม่ได้ส่งทุกชั่วโมง — ลอง `--force` เพื่อส่งทันที
4. `data/alert_state.json` **ต้องถูก commit** ไม่งั้นทุก run จะเป็น "ครั้งแรก" และไม่ส่งเลย
</details>

<details>
<summary><b>pytest ล้มหลังแก้ config</b></summary>

`test_cusi.py` มีเทสต์ยืนยันว่าน้ำหนักรวม = 1.0 และตรงตามตาราง §3.2
ถ้าตั้งใจเปลี่ยนน้ำหนักจริง ต้องแก้ `test_matches_spec_table` ให้ตรงกับค่าใหม่ด้วย
(เทสต์นี้จงใจให้ "เจ็บ" เวลาแก้ เพื่อบังคับให้เป็นการตัดสินใจที่รู้ตัว)
</details>

---

## การดูแลระยะยาว

* **repo บวม** — commit ข้อมูลชั่วโมงละครั้ง ≈ 20 KB → ~40 MB/ปี
  ถ้าเริ่มช้าให้ squash ประวัติของ `data/` เป็นระยะ:
  `git checkout --orphan tmp && git commit && git branch -D main && git branch -m main`
  (⚠️ ทำลายประวัติ — สำรองก่อน)
* **cusi_history.json เก็บ 3 ปีโดยตั้งใจ** ไม่ prune เหลือ 90 วันเหมือน
  `indicators_history.json` เพราะกราฟหน้าเว็บต้องใช้ 1Y+ และไฟล์ยังเล็ก (~67 KB)
* **ประเมิน CUSI ใหม่** เมื่อมี unwind episode ใหม่ 2–3 ครั้งที่ระบบไม่เคยเห็น
  — นั่นคือ out-of-sample จริง ต่างจาก backtest ปัจจุบันที่เป็น in-sample ทั้งหมด

---

## ที่มาข้อมูล

| ข้อมูล | แหล่ง | ความถี่ |
|---|---|---|
| ราคา 13 ตัว | Yahoo Finance (yfinance) | daily + 1h |
| JGB 10Y | MOF Japan CSV (Shift-JIS, ศักราชญี่ปุ่น) | daily |
| COT JPY | CFTC Socrata (Legacy, futures-only) | weekly (ศุกร์) |
| ราคาสดบนหน้าเว็บ | TradingView widgets | real-time |

### สิ่งที่ต่างจาก spec (ตรวจสอบกับ API จริงแล้ว)

1. **MOF URL ใน §2.3 คืน HTML 404** — ของจริงอยู่บนเว็บภาษาญี่ปุ่น เป็น Shift-JIS
   วันที่ศักราชญี่ปุ่น (`R8.7.1`) และแยกเป็น 2 ไฟล์ (ประวัติ + เดือนปัจจุบัน) ต้องรวมกัน
2. **`JGB=F` ไม่มีอยู่บน Yahoo** — Fallback B ใช้ ETF `1482.T` + modified duration แทน
3. **`history_years` = 4 ไม่ใช่ 3** — z-score กิน warm-up 252 วันทำการแรก
4. **COT มี `available_from`** (= report_date + 3 วัน) — ป้องกัน lookahead ตอน forward-fill
5. **z-score ต้องคำนวณบนปฏิทินวันทำการ** — frame เป็น union ของทุก ticker ซึ่งมีเสาร์–อาทิตย์
   ติดมาด้วยเพราะ BTC เทรด 24/7

---

## Lookahead discipline

จุดที่ spec §6 เตือนว่าอันตรายที่สุด — คุมด้วยเทสต์ 2 ชั้น:

* `TestNoLookahead` — คำนวณบน series เต็ม vs ตัดท้ายทิ้ง ค่าช่วงต้นต้องเท่ากันเป๊ะ
  และยัด spike 10,000 ที่แถวสุดท้ายแล้วค่าก่อนหน้าต้องไม่ขยับ
* `TestCotNoLookahead` — เลื่อนวันประกาศ COT ออกไปแล้วค่าต้องเลื่อนตาม
  (ถ้าโค้ดแอบอิง `report_date` แทน `available_from` เทสต์นี้จะแดงทันที)

```bat
python -m pytest tests\ -q                 :: 154 tests
python -m pytest tests\ -k Lookahead -v
```
