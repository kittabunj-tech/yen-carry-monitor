# Deploy Checklist — ครั้งแรก (Windows + git)

ทำตามทีละข้อ ทุกข้อมี "วิธีเช็คว่าผ่าน" กำกับ ถ้าข้อไหนไม่ผ่านให้หยุดแก้ก่อนไปต่อ
เวลาโดยประมาณทั้งหมด: **30–45 นาที**

> คำสั่งทั้งหมดรันใน **PowerShell** หรือ **Git Bash** จากโฟลเดอร์ `dashboard`

---

## ☐ 0. เตรียมเครื่อง (ทำครั้งเดียว)

```powershell
py -3.11 --version          # ต้องเห็น Python 3.11.x
git --version               # ต้องเห็น git version 2.x
```

ถ้ายังไม่มี: [python.org/downloads](https://www.python.org/downloads/) (ติ๊ก **Add python.exe to PATH**) · [git-scm.com](https://git-scm.com/download/win)

---

## ☐ 1. รัน pipeline ในเครื่องให้ผ่านก่อน

**อย่าข้ามข้อนี้** — ถ้าในเครื่องยังไม่ผ่าน บน GitHub ก็ไม่ผ่าน แต่ debug ยากกว่ามาก

```powershell
cd dashboard
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

python pipeline\run_all.py --mode weekly
python -m pytest tests\ -q
```

**ผ่านเมื่อ**: บรรทัดสุดท้ายของ pipeline ขึ้น `=== done ... CUSI=xx.x (LEVEL) ...`
และ pytest ขึ้น `154 passed`

**ผ่านเมื่อ**: มีไฟล์ครบใน `data\` — `latest.json`, `cusi_history.json`,
`indicators_history.json`, `cot_history.json`

---

## ☐ 2. ดูหน้าเว็บในเครื่อง

```powershell
python -m http.server 8000
```

เปิด <http://localhost:8000/> → ต้องเด้งไปที่ `/site/index.html` เอง

**ผ่านเมื่อ**: เห็นเข็ม gauge ชี้ค่า CUSI · กราฟทั้ง 5 ขึ้นครบ · ตาราง indicators มีตัวเลข
(รายละเอียดว่าแต่ละ panel ควรเห็นอะไร: [site/CHECKLIST.md](site/CHECKLIST.md))

กด `Ctrl+C` เพื่อปิด server

---

## ☐ 3. สร้าง repo บน GitHub

1. ไปที่ <https://github.com/new>
2. ตั้งชื่อ เช่น `yen-carry-monitor`
3. เลือก **Public** — จำเป็นถ้าใช้ GitHub Pages ฟรี
4. **ไม่ต้อง**ติ๊ก "Add a README" / .gitignore / license (มีอยู่แล้วในโปรเจกต์)
5. กด **Create repository**

---

## ☐ 4. push โค้ดขึ้นไป

แทน `<OWNER>` ด้วยชื่อผู้ใช้ GitHub และ `<REPO>` ด้วยชื่อ repo

```powershell
cd dashboard
git init
git add .
git status                  # ← ดูให้แน่ใจก่อน commit
```

**ตรวจก่อน commit** — ใน `git status` ต้อง**เห็น**:
`data/latest.json` · `data/cusi_history.json` · `data/indicators_history.json` · `data/cot_history.json`

และต้อง**ไม่เห็น**: `.venv/` · `data/raw/` · `__pycache__/`

ถ้าเห็น `.venv/` แปลว่า `.gitignore` ไม่ทำงาน → หยุดแก้ก่อน

```powershell
git commit -m "Yen Carry Monitor: pipeline + CUSI + dashboard"
git branch -M main
git remote add origin https://github.com/<OWNER>/<REPO>.git
git push -u origin main
```

**ผ่านเมื่อ**: refresh หน้า repo แล้วเห็นไฟล์ครบ

---

## ☐ 5. เปิดสิทธิ์ให้ Actions เขียน repo ได้

> ถ้าข้ามข้อนี้ workflow จะรันได้แต่ **push ไม่ได้** — error `403` ตอน commit

1. repo → **Settings** → **Actions** → **General**
2. เลื่อนลงหา **Workflow permissions**
3. เลือก **Read and write permissions**
4. กด **Save**

---

## ☐ 6. ทดสอบ workflow ด้วยมือ (ก่อนรอ cron)

1. repo → แท็บ **Actions**
2. ถ้าเห็นปุ่ม "I understand my workflows, go ahead and enable them" ให้กด
3. เลือก **Update hourly** จากเมนูซ้าย → **Run workflow** → **Run workflow**
4. รอ ~2 นาที แล้วกดเข้าไปดูผล

**ผ่านเมื่อ**: ทุก step เป็นสีเขียว และในหน้า summary เห็นค่า CUSI

**ผ่านเมื่อ**: กลับไปดูแท็บ **Code** แล้วเห็น commit ใหม่ชื่อ `data: hourly update ...`
(ถ้าไม่มี commit ใหม่ แปลว่าข้อมูลไม่เปลี่ยน ซึ่งก็ถูกต้อง — ลองใหม่อีกชั่วโมงถัดไป)

> ถ้าขึ้นสีแดง: ดูหัวข้อ Troubleshooting ใน [README.md](README.md)

---

## ☐ 7. เปิด GitHub Pages

> **สำคัญที่สุดของขั้นตอนทั้งหมด** — เลือกผิดแล้วหน้าเว็บจะขึ้นแต่ไม่มีข้อมูล

1. repo → **Settings** → **Pages**
2. **Source**: `Deploy from a branch`
3. **Branch**: `main` และ folder: **`/ (root)`** ← **ห้ามเลือก `/site`**
4. **Save** แล้วรอ 1–2 นาที

### ทำไมต้อง `/ (root)` ไม่ใช่ `/site`

หน้า dashboard อยู่ที่ `site/index.html` แต่ดึงข้อมูลจาก `../data/latest.json`
ถ้าตั้ง root เป็น `/site` โฟลเดอร์ `data/` จะไม่ถูก publish เลย →
หน้าเว็บขึ้นปกติแต่ตัวเลขว่างทั้งหมด และขึ้น toast แดง "โหลดข้อมูลไม่สำเร็จ"

(ตรวจสอบแล้วจริง: `site/` มีแต่ `index.html`, `app.js`, `charts.js`, `style.css`
ไม่มี `data/` อยู่ข้างใน)

ตั้ง `/ (root)` แล้วได้ทั้งสองโฟลเดอร์ และไฟล์ `index.html` ที่ root จะพาไป `site/` ให้เอง

**ผ่านเมื่อ**: เปิด `https://<OWNER>.github.io/<REPO>/` แล้วเด้งไปหน้า dashboard
พร้อมตัวเลข CUSI ครบ (ไม่ใช่ `—`)

---

## ☐ 8. Telegram alert (ไม่บังคับ แต่แนะนำ)

### 8.1 สร้าง bot

1. เปิด Telegram หา **@BotFather**
2. พิมพ์ `/newbot`
3. ตั้งชื่อ bot (อะไรก็ได้) แล้วตั้ง username ที่ลงท้ายด้วย `bot` เช่น `john_carry_bot`
4. BotFather จะส่ง **token** หน้าตาแบบ `8123456789:AAH...` ← เก็บไว้

### 8.2 หา chat_id

1. **ทักหา bot ของตัวเองก่อน** แล้วพิมพ์อะไรก็ได้ เช่น `hi`
   (ถ้าไม่ทักก่อน bot จะส่งหาเราไม่ได้ — Telegram บล็อกไว้)
2. เปิด URL นี้ในเบราว์เซอร์ (แทน `<TOKEN>` ด้วย token จริง):

   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```

3. หาเลขในช่อง `"chat":{"id":123456789` ← นั่นคือ **chat_id**
   (ถ้าได้ `{"ok":true,"result":[]}` แปลว่ายังไม่ได้ทัก bot — กลับไปข้อ 1)

### 8.3 ทดสอบในเครื่องก่อน

```powershell
$env:TELEGRAM_BOT_TOKEN="8123456789:AAH..."
$env:TELEGRAM_CHAT_ID="123456789"
python pipeline\alert.py --test
```

**ผ่านเมื่อ**: ได้ข้อความ "การเชื่อมต่อ Telegram ทำงานปกติ" ใน Telegram

### 8.4 ใส่ secrets บน GitHub

repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token จาก BotFather |
| `TELEGRAM_CHAT_ID` | เลข chat id |

แล้วเพิ่ม **variable** (คนละแท็บกับ secret — แท็บ *Variables*):

| Name | Value |
|---|---|
| `DASHBOARD_URL` | `https://<OWNER>.github.io/<REPO>/` |

> ระบบจะส่งเฉพาะตอน**ระดับเปลี่ยน**หรือ CUSI กระโดด >10 จุดใน 1 วัน
> ไม่ได้ส่งทุกชั่วโมง — ถ้าอยากลองส่งเดี๋ยวนั้นใช้ `python pipeline\alert.py --force`

---

## ☐ 9. ตรวจครั้งสุดท้าย

| เช็ค | ผ่านเมื่อ |
|---|---|
| หน้าเว็บออนไลน์ | `https://<OWNER>.github.io/<REPO>/` แสดง CUSI เป็นตัวเลข |
| ข้อมูลอัพเดทเอง | รอถึงชั่วโมงถัดไป (จ.–ศ.) แล้วเห็น commit ใหม่ |
| badge ใน README | เป็นสีเขียว (แก้ `<OWNER>/<REPO>` ใน README ก่อน) |
| Actions ไม่แดง | แท็บ Actions ไม่มี run สีแดงติดกันหลายครั้ง |

---

## ☐ 10. แก้ badge ใน README

เปิด `README.md` แล้วแทนที่ `<OWNER>/<REPO>` ทั้งหมดด้วยของจริง

```powershell
# Git Bash
sed -i 's|<OWNER>/<REPO>|johndoe/yen-carry-monitor|g' README.md
git add README.md && git commit -m "docs: update badge URLs" && git push
```

---

## หลัง deploy — สิ่งที่ควรรู้

* **cron ดีเลย์ได้ 5–20 นาที** เป็นเรื่องปกติของ GitHub Actions free tier
  ไม่ใช่ความผิดพลาด — ดู `data_as_of` บนหน้าเว็บเป็นหลัก
* **workflow จะหยุดเองถ้า repo ไม่มีกิจกรรม 60 วัน** GitHub จะส่งอีเมลเตือน
  กด "Enable workflow" ในแท็บ Actions เพื่อเปิดใหม่
* **ถ้า repo เริ่มบวม** (commit ทุกชั่วโมงสะสมหลายเดือน) ดูหัวข้อ
  "การดูแลระยะยาว" ใน README
