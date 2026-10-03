# Ai Gateway

> **High-Concurrency OpenAI-Compatible LLM Reverse Proxy & Subscription Portal**  
> ออกแบบและพัฒนาตามหลักวิศวกรรมซอฟต์แวร์ (Decoupled Architecture, Non-blocking I/O, Atomic Token Metering) สำหรับแจกโควตา API รายวัน (5,000,000 Tokens) และปล่อยเช่า VIP แบบ Unlimited ผ่านการเชื่อมต่อเข้ากับ **9Router** ตัวเดียว

---

## จุดเด่นของระบบ (Core Features)

1. **OpenAI API Standard Compatibility (100%):**
   - รองรับ Endpoint มาตรฐาน `/v1/chat/completions` และ `/v1/models`
   - ใช้งานได้ทันทีกับ **Cursor IDE, Cline (VS Code), NextChat, LibreChat, Chatbox** และ OpenAI SDKs (Python, Node.js, Go)
   - รองรับการตอบกลับแบบเรียลไทม์ **Streaming (Server-Sent Events - SSE)** กินแรมเซิร์ฟเวอร์ต่ำคงที่ $O(1)$
2. **ระบบสมาชิก & Discord OAuth2:**
   - เข้าสู่ระบบด้วยบัญชี Discord ปลอดภัย ได้รับ User API Key ส่วนตัว (`sk-portal-...`) ทันทีหลังล็อกอิน
   - มีระบบจดจำสิทธิ์ Superadmin อัตโนมัติสำหรับเจ้าของระบบ
3. **ระบบจัดการโควตา & Tier (Atomic Metering):**
   - **สายฟรี (Free Tier):** ได้รับ **5,000,000 Tokens / วัน** รีเซ็ตอัตโนมัติทุกเที่ยงคืน (00:00 น.)
   - **สายเช่า VIP (Daily / Weekly Pass):** ใช้งาน Token ได้ไม่จำกัด 100% พร้อมช่องทางลัดความเร็วสูง (Priority Routing Queue)
4. **Usage Analytics & กราฟแสดงสถิติจริง (Chart.js):**
   - แสดงสถิติการใช้งาน Token ย้อนหลัง 24 ชั่วโมง, 7 วัน และ 30 วัน
   - กราฟแท่ง Interactive คำนวณจากประวัติคำขอจริงในฐานข้อมูล SQLite
5. **หน้าบ้านระดับ Production (Clean Dark Zinc SaaS):**
   - ดีไซน์สไตล์ Developer Console เรียบหรู สะอาดตา ไม่ใช้อิโมจิ
   - ใช้ไอคอนคมชัดจาก **Font Awesome 6**
   - รองรับ **Responsive 100%** ทั้งบนมือถือและหน้าจอคอมพิวเตอร์
   - ระบบ **Universal Clipboard Copy** ก๊อปปี้คีย์และชื่อโมเดลได้ทันที
6. **หน้าหลังบ้านแอดมิน (Admin Control Center):**
   - สลับมุมมอง Admin Panel และ User View ได้แบบ Real-time บนแถบ Header
   - จัดการสมาชิก: เพิ่มวัน VIP (`+1d`, `+7d`), ปลด VIP (`Revoke VIP`), รีเซ็ตโควตารายวัน (`Reset Quota`), แบน/ปลดแบน และลบบัญชี (`Delete User`)
   - ปรับแต่งการตั้งค่าระบบสดผ่านหน้าเว็บ (จำนวนแจกฟรี, URL/Key ของ 9Router, ราคาแพ็กเกจ, ลิงก์ Discord)
7. **เชื่อมต่อ 9Router ตัวเดียว:**
   - ทำหน้าที่เป็น Reverse Proxy กระจายคำขอต่อไปยัง 9Router Master รับภาระการโหลดบาลานซ์และสลับคีย์ AI

---

## สถาปัตยกรรมระบบ (System Architecture)

```mermaid
flowchart TD
    Client([ผู้ใช้งาน: Cursor / NextChat / Python]) -->|Authorization: Bearer sk-portal-...| Gateway[FastAPI Gateway Engine]
    
    Gateway --> Auth[ตรวจสิทธิ์ & ความถูกต้องของ User Key]
    Auth --> Quota[ตรวจโควตารายวัน / สิทธิ์ VIP ใน SQLite WAL]
    
    Quota -->|ผ่านการตรวจโควตา| Stream[Non-blocking SSE Proxy Engine]
    
    Stream -->|Authorization: Bearer Master 9Router Key| Router9[9Router Core: api.thirx.com]
    
    Router9 --> ModelPool[คลังโมเดล AI: DeepSeek, xAI, Qwen, GLM]
    ModelPool -->|Stream Chunks| Router9
    Router9 -->|Stream Chunks| Gateway
    Gateway -->|Atomic Token Metering| DB[(SQLite WAL: portal.db)]
    Gateway -->|Stream กลับทันที| Client
```

---

## โมเดลที่เปิดให้บริการ (Supported Models Catalog)

ระบบถูกล็อกและเปิดให้บริการเฉพาะ 7 โมเดลหลักจาก 9Router:

| รหัสโมเดล (Model ID) | ค่ายผู้พัฒนา (Provider) | คุณสมบัติเด่น |
|---|---|---|
| `deepseek-v4-flash` | DeepSeek | ประมวลผลรวดเร็ว เขียนโค้ดแม่นยำ และตอบคำถามทั่วไป |
| `GLM-5.3-Flash` | ZHIPU AI | โมเดลความเร็วสูง รองรับภาษาไทยและเอกสารขนาดยาว |
| `grok-4.7` | xAI | ฉลาด ทันสมัย วิเคราะห์ข้อมูลเชิงลึกได้ดี |
| `grok-4.7-xhigh` | xAI | โมเดลตัวท็อป ความสามารถด้านการคิดและใช้เหตุผลขั้นสูง |
| `qwen3.8-27b` | Alibaba Qwen | เชี่ยวชาญงานเขียน Logic และคณิตศาสตร์ |
| `MiniMax-M2.7` | MiniMax | โมเดลภาษาคุณภาพสูง บริบทกว้าง |
| `muse-spark-1.3` | Muse | โมเดลขนาดกะทัดรัด ทำงานรวดเร็ว ตอบสนองในเสี้ยววินาที |

---

## โครงสร้างโฟลเดอร์ (Directory Structure)

```text
api-portal/
├── app/
│   ├── core/
│   │   ├── config.py          # การโหลด Environment Variables & Superadmin Binding
│   │   └── database.py        # ฐานข้อมูล SQLite WAL Mode (High Concurrency)
│   ├── routers/
│   │   ├── auth.py            # Discord OAuth2 Authentication
│   │   ├── gateway.py         # OpenAI Compatible /v1 Endpoints
│   │   └── pages.py           # Dashboard, Admin & Analytics API Routes
│   ├── services/
│   │   ├── proxy_service.py   # Streaming Proxy & Upstream Communication
│   │   └── user_service.py    # จัดการบัญชี, Token Metering, Analytics และ VIP
│   ├── templates/
│   │   ├── admin.html         # หน้าหลังบ้านแอดมิน
│   │   ├── dashboard.html     # หน้าแดชบอร์ดลูกค้า & Usage Analytics
│   │   └── login.html         # หน้าเข้าสู่ระบบ (Unified Single Card)
│   └── main.py                # จุดเริ่มต้น FastAPI Application
├── Dockerfile                 # Docker build container image
├── docker-compose.yml         # 1-Command production runner
├── requirements.txt           # Python dependencies
├── .env.example               # ตัวอย่างไฟล์ตั้งค่า
├── start.sh                   # สคริปต์เปิดเซิร์ฟเวอร์
└── stop.sh                    # สคริปต์ปิดเซิร์ฟเวอร์
```

---

## การติดตั้งและเริ่มใช้งาน (Quick Start)

### 1. ติดตั้ง Dependencies และตั้งค่า Environment

```bash
# โคลน Repository
git clone https://github.com/ffxlm/Ai-Gateway.git
cd Ai-Gateway

# สร้าง Virtual Environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# สร้างไฟล์คอนฟิก .env
cp .env.example .env
```

### 2. ตั้งค่าไฟล์ `.env`

```ini
# ─── Server Configuration ───
HOST="0.0.0.0"
PORT=8080
SECRET_KEY="your-random-secret-key"

# ─── 9Router Master Connection ───
MASTER_ROUTER_URL="https://api.thirx.com"
MASTER_ROUTER_KEY="sk-your-9router-key"

# ─── Discord OAuth2 ───
DISCORD_CLIENT_ID="your_discord_client_id"
DISCORD_CLIENT_SECRET="your_discord_client_secret"
DISCORD_REDIRECT_URI="http://localhost:8080/auth/discord/callback"

# ─── Superadmin & Support ───
ADMIN_DISCORD_IDS="your_discord_id"
ADMIN_SECRET="admin-pass-2026"
DISCORD_INVITE_URL="https://discord.gg/your-invite"

# ─── Business Rules ───
DAILY_FREE_TOKENS=5000000
FREE_CONCURRENCY_LIMIT=5
VIP_CONCURRENCY_LIMIT=20
VIP_DAILY_PRICE=10
VIP_WEEKLY_PRICE=50
```

### 3. เปิดใช้งานเซิร์ฟเวอร์

#### วิธีที่ 1: รันด้วยสคริปต์พื้นฐาน (Local / VPS)
```bash
# เปิดเซิร์ฟเวอร์
./start.sh

# ปิดเซิร์ฟเวอร์
./stop.sh
```

#### วิธีที่ 2: รันด้วย Docker Compose (แนะนำสำหรับ VPS)
```bash
docker compose up -d
```

---

## วิธีนำ API ไปใช้งาน (Integration Examples)

### Python (OpenAI Official SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/v1",  # หรือโดเมนจริง https://api.yourdomain.com/v1
    api_key="sk-portal-your-key-here"
)

response = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[{"role": "user", "content": "สวัสดี แนะนำตัวหน่อย"}],
    stream=True
)

for chunk in response:
    content = chunk.choices[0].delta.content or ""
    print(content, end="", flush=True)
print()
```

### cURL

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-portal-your-key-here" \
  -d '{
    "model": "deepseek-v4-flash",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": true
  }'
```

### การตั้งค่าใน Cursor IDE / Cline (VS Code)
1. ไปที่ **Settings -> Models -> OpenAI API Key**
2. ใส่การตั้งค่าดังนี้:
   - **Base URL:** `http://localhost:8080/v1` (หรือโดเมนบน VPS)
   - **API Key:** คีย์ของคุณจากหน้าแดชบอร์ด (`sk-portal-...`)
   - **Model Name:** เลือกใส่โมเดล เช่น `deepseek-v4-flash` หรือ `GLM-5.3-Flash`

---

## การนำขึ้นใช้งานจริงบน VPS (Production Deployment)

### 1. รันเบื้องหลังด้วย Systemd Service

สร้างไฟล์ `/etc/systemd/system/ai-gateway.service`:

```ini
[Unit]
Description=Ai Gateway Service
After=network.target

[Service]
User=film
WorkingDirectory=/home/film/Desktop/api-portal
ExecStart=/home/film/Desktop/api-portal/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ai-gateway
```

### 2. ตั้งค่า Nginx Reverse Proxy พร้อม SSL (HTTPS)

```nginx
server {
    server_name api.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        
        # รองรับ Server-Sent Events (SSE) Streaming
        proxy_set_header Connection '';
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
        
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

## ลิขสิทธิ์และการพัฒนา (Copyright)

© 2026 Ai Gateway. All rights reserved.  
by ffxlm
