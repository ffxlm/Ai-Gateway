# Ai Gateway

> **High-Concurrency [OI]-Compatible LLM Reverse Proxy & Subscription Portal**
> Built on solid software engineering principles (decoupled architecture, non-blocking I/O, atomic token metering) to hand out daily API quotas (5,000,000 tokens) and rent out Unlimited VIP access through a single **9Router** connection.

---

## Core Features

1. **[OI] API Standard Compatibility (100%):**
   - Supports the standard `/v1/chat/completions` and `/v1/models` endpoints.
   - Works out of the box with **Cursor IDE, Cline (VS Code), NextChat, LibreChat, Chatbox**, and [OI] SDKs (Python, Node.js, Go).
   - Supports real-time **Streaming (Server-Sent Events - SSE)** responses with low, constant $O(1)$ server memory usage.
2. **Membership System & Discord OAuth2:**
   - Sign in securely with a Discord account and receive a personal User API Key (`sk-portal-...`) immediately after login.
   - Automatic Superadmin recognition for the system owner.
3. **Quota & Tier Management (Atomic Metering):**
   - **Free Tier:** **5,000,000 tokens / day**, automatically reset every midnight (00:00).
   - **VIP Pass (Daily / Weekly):** 100% unlimited token usage with a high-speed Priority Routing Queue.
4. **Usage Analytics & Real Statistics Charts (Chart.js):**
   - Displays token usage history for the last 24 hours, 7 days, and 30 days.
   - Interactive bar charts computed from real request history stored in SQLite.
5. **Production-Grade Frontend (Clean Dark Zinc SaaS):**
   - A polished, clean Developer Console design with no emoji clutter.
   - Crisp icons from **Font Awesome 6**.
   - **100% Responsive** across mobile and desktop.
   - **Universal Clipboard Copy** to instantly copy keys and model names.
6. **Admin Control Center:**
   - Switch between Admin Panel and User View in real time from the header bar.
   - Member management: add VIP days (`+1d`, `+7d`), revoke VIP (`Revoke VIP`), reset daily quota (`Reset Quota`), ban/unban, and delete accounts (`Delete User`).
   - Edit live system settings from the web UI (free quota amount, 9Router URL/Key, package pricing, Discord link).
7. **Single 9Router Connection:**
   - Acts as a reverse proxy that forwards requests to the 9Router Master, offloading load balancing and AI key rotation.

---

## System Architecture

```mermaid
flowchart TD
    Client([Client: Cursor / NextChat / Python]) -->|Authorization: Bearer sk-portal-...| Gateway[FastAPI Gateway Engine]

    Gateway --> Auth[Validate User Key & Permissions]
    Auth --> Quota[Check Daily Quota / VIP Status in SQLite WAL]

    Quota -->|Quota check passed| Stream[Non-blocking SSE Proxy Engine]

    Stream -->|Authorization: Bearer Master 9Router Key| Router9[9Router Core: api.thirx.com]

    Router9 --> ModelPool[AI Model Pool: DeepSeek, xAI, Qwen, GLM]
    ModelPool -->|Stream Chunks| Router9
    Router9 -->|Stream Chunks| Gateway
    Gateway -->|Atomic Token Metering| DB[(SQLite WAL: portal.db)]
    Gateway -->|Stream back immediately| Client
```

---

## Supported Models Catalog

The system is locked to exactly 7 core models served through 9Router:

| Model ID | Provider | Highlights |
|---|---|---|
| `deepseek-v4-flash` | DeepSeek | Fast inference, accurate code generation, and general Q&A |
| `GLM-5.3-Flash` | ZHIPU AI | High-speed model supporting Thai and long documents |
| `grok-4.7` | xAI | Smart and modern, strong at in-depth data analysis |
| `grok-4.7-xhigh` | xAI | Flagship model with advanced reasoning capabilities |
| `qwen3.8-27b` | Alibaba Qwen | Excels at writing logic and mathematics |
| `MiniMax-M2.7` | MiniMax | High-quality language model with a wide context window |
| `muse-spark-1.3` | Muse | Compact, fast model with sub-second responses |

---

## Directory Structure

```text
api-portal/
├── app/
│   ├── core/
│   │   ├── config.py          # Environment variable loading & Superadmin binding
│   │   └── database.py        # SQLite WAL mode database (high concurrency)
│   ├── routers/
│   │   ├── auth.py            # Discord OAuth2 authentication
│   │   ├── gateway.py         # [OI]-compatible /v1 endpoints
│   │   └── pages.py           # Dashboard, Admin & Analytics API routes
│   ├── services/
│   │   ├── proxy_service.py   # Streaming proxy & upstream communication
│   │   └── user_service.py    # Accounts, token metering, analytics, and VIP
│   ├── templates/
│   │   ├── admin.html         # Admin control center
│   │   ├── dashboard.html     # Customer dashboard & usage analytics
│   │   └── login.html         # Sign-in page (unified single card)
│   └── main.py                # FastAPI application entry point
├── Dockerfile                 # Docker build container image
├── docker-compose.yml         # One-command production runner
├── requirements.txt           # Python dependencies
├── .env.example               # Example configuration file
├── start.sh                   # Server startup script
└── stop.sh                    # Server shutdown script
```

---

## Installation & Quick Start

### 1. Install Dependencies and Configure the Environment

```bash
# Clone the repository
git clone https://github.com/ffxlm/Ai-Gateway.git
cd Ai-Gateway

# Create a virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Create the config file
cp .env.example .env
```

### 2. Configure the `.env` File

```ini
# ─── Server Configuration ───
HOST="0.0.0.0"
PORT=8080
SECRET_KEY="your-random-secret-key"
TIMEZONE="Asia/Bangkok"

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

### 3. Start the Server

#### Option 1: Run with the basic scripts (Local / VPS)
```bash
# Start the server
./start.sh

# Stop the server
./stop.sh
```

#### Option 2: Run with Docker Compose (recommended for VPS)
```bash
docker compose up -d
```

---

## Daily Quota Reset

Free-tier token usage is reset to `0` at **00:00 in the configured `TIMEZONE`** (default `Asia/Bangkok`). This is enforced in three complementary ways:

- **Midnight scheduler** — a background task in `app/main.py` (`daily_quota_reset_loop`) wakes at the next local midnight and resets every stale user.
- **Startup catch-up** — on boot, `reset_all_stale_quota()` resets any user whose `last_usage_date` is behind, covering downtime across midnight.
- **Lazy rollover** — read paths (`get_user_by_api_key`, `get_or_create_user`, and the dashboard session lookup via `apply_daily_rollover`) also reset a user when their usage date is stale, so the UI and API always reflect the current day.

> When deploying with Docker, the image sets `TZ=Asia/Bangkok` so the reset happens at local midnight rather than UTC.

---

## API Integration Examples

### Python ([OI] Official SDK)

```python
from openai import [OI]

client = [OI](
    base_url="http://localhost:8080/v1",  # or the real domain, e.g. https://api.yourdomain.com/v1
    api_key="sk-portal-your-key-here"
)

response = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[{"role": "user", "content": "Hello, introduce yourself briefly."}],
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

### Cursor IDE / Cline (VS Code) Setup
1. Go to **Settings -> Models -> [OI] API Key**.
2. Enter the following settings:
   - **Base URL:** `http://localhost:8080/v1` (or your VPS domain)
   - **API Key:** your key from the dashboard (`sk-portal-...`)
   - **Model Name:** pick a model such as `deepseek-v4-flash` or `GLM-5.3-Flash`

---

## Production Deployment (VPS)

### 1. Run in the Background with a Systemd Service

Create `/etc/systemd/system/ai-gateway.service`:

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

### 2. Configure an Nginx Reverse Proxy with SSL (HTTPS)

```nginx
server {
    server_name api.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;

        # Support Server-Sent Events (SSE) streaming
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

## Copyright

© 2026 Ai Gateway. All rights reserved.
by ffxlm
