# Ai Gateway

> **High-Concurrency [OI]-Compatible LLM Reverse Proxy & Prepaid Wallet Portal**
> Built on solid software engineering principles (decoupled architecture, non-blocking I/O, atomic token metering) to offer **unlimited free models** plus **pay-as-you-go premium models** billed from a prepaid **USD wallet** (topped up in THB), through a single **9Router** connection.

---

## Core Features

1. **[OI] API Standard Compatibility (100%):**
   - Supports the standard `/v1/chat/completions` and `/v1/models` endpoints.
   - Works out of the box with **Cursor IDE, Cline (VS Code), NextChat, LibreChat, Chatbox**, and [OI] SDKs (Python, Node.js, Go).
   - Supports real-time **Streaming (Server-Sent Events - SSE)** responses with low, constant $O(1)$ server memory usage.
2. **Membership System & Discord OAuth2:**
   - Sign in securely with a Discord account and receive a personal User API Key (`sk-portal-...`) immediately after login.
   - Automatic Superadmin recognition for the system owner.
3. **Prepaid Wallet & Tiered Models (Atomic Metering):**
   - **Free Models:** **Unlimited** usage at no cost — never charged.
   - **Premium "xHigh" Models:** A **daily free trial** (default **1,000,000 tokens/day**) per user, then billed **per token** from a prepaid USD wallet.
   - **Top-up in THB:** PromptPay/SlipOK payments are credited to the wallet in USD at the configured rate (default **฿35 = $1**). The modal shows exactly how many USD you'll receive.
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
   - Member management: adjust wallet balance (credit/debit USD), ban/unban, and delete accounts (`Delete User`).
   - Wallet ledger + verified top-up history.
   - Edit live system settings from the web UI (USD→THB rate, min top-up, packages, 9Router URL/Key, Discord link).
7. **Single 9Router Connection:**
   - Acts as a reverse proxy that forwards requests to the 9Router Master, offloading load balancing and AI key rotation.

---

## System Architecture

```mermaid
flowchart TD
    Client([Client: Cursor / NextChat / Python]) -->|Authorization: Bearer sk-portal-...| Gateway[FastAPI Gateway Engine]

    Gateway --> Auth[Validate User Key & Permissions]
    Auth --> Tier{Model Tier?}

    Tier -->|Free model| Stream[Non-blocking SSE Proxy Engine]
    Tier -->|Premium xHigh| Trial{Daily free trial left?}
    Trial -->|Yes| Stream
    Trial -->|No| Balance{Wallet balance > 0?}
    Balance -->|No| Err[402 Insufficient Balance]
    Balance -->|Yes| Stream

    Stream -->|Authorization: Bearer Master 9Router Key| Router9[9Router Core: api.thirx.com]

    Router9 --> ModelPool[AI Model Pool: DeepSeek, GLM, MiniMax]
    ModelPool -->|Stream Chunks| Router9
    Router9 -->|Stream Chunks| Gateway
    Gateway -->|Atomic Token Metering + Wallet Charge| DB[(SQLite WAL: portal.db)]
    Gateway -->|Stream back immediately| Client
```

---

## Supported Models Catalog

The system serves 3 free models plus 1 premium model through 9Router. The catalog is defined in a single place: `app/core/catalog.py`.

### Free — Unlimited

| Model ID | Provider | Highlights |
|---|---|---|
| `deepseek-v4-flash` | DeepSeek | Fast inference, accurate code generation, and general Q&A |
| `GLM-5.3-Flash` | ZHIPU AI | High-speed model supporting Thai and long documents |
| `MiniMax-M2.7` | MiniMax | High-quality language model with a wide context window |

### Premium — xHigh (billed from wallet, USD per 1M tokens)

| Model ID | Provider | Input | Output | Official | Savings |
|---|---|---|---|---|---|
| `deepseek-v4.1-flash` | DeepSeek | $0.015 | $0.06 | $0.15 / $0.60 | 90% cheaper |
| `claude-opus-4-6` | Anthropic | $0.50 | $2.50 | $5.00 / $25.00 | 90% cheaper |

---

## Directory Structure

```text
api-portal/
├── app/
│   ├── core/
│   │   ├── config.py          # Environment variable loading & Superadmin binding
│   │   ├── catalog.py         # Single source of truth: free + premium model catalog
│   │   └── database.py        # SQLite WAL mode database (high concurrency) + migrations
│   ├── routers/
│   │   ├── auth.py            # Discord OAuth2 authentication
│   │   ├── gateway.py         # [OI]-compatible /v1 endpoints (auth + balance gate)
│   │   └── pages.py           # Dashboard, Admin, Wallet & Analytics API routes
│   ├── services/
│   │   ├── proxy_service.py   # Streaming proxy, upstream communication & metering
│   │   ├── user_service.py    # Accounts, wallet, token metering & analytics
│   │   └── payment_service.py # PromptPay QR generation & SlipOK slip verification
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
# Free models are unlimited; the wallet only pays for premium (xHigh) usage.
CONCURRENCY_LIMIT=10
PREMIUM_TRIAL_TOKENS_PER_DAY=1000000
USD_TO_THB=35
MIN_TOPUP_THB=10
TOPUP_PACKAGES_THB=10,35,70,175,350,700
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

## Wallet & Billing

- **Currency:** balances are stored and displayed in **USD**; top-ups are paid in **THB**.
- **Rate:** `1 USD = ฿35` by default (`USD_TO_THB`, editable in the Admin panel).
- **Free models:** unlimited, never deducted from the wallet.
- **Premium (xHigh) models:** each user gets a **per-model daily free trial** (`PREMIUM_TRIAL_TOKENS_PER_DAY`, default **1,000,000 tokens/day/model**, reset at 00:00 ICT). A model can override this in `app/core/catalog.py` via `trial_tokens_per_day` (set `0` for no trial). Tokens beyond the trial are charged per token at the rates in `app/core/catalog.py` (input/output) and deducted from the wallet after each successful request. Each premium model card shows its own live trial progress bar.
- **Top-up flow:** choose/enter a THB amount (min `MIN_TOPUP_THB`) → the modal previews the USD credit → pay via PromptPay QR → upload the slip → SlipOK verifies the exact THB amount → wallet is credited in USD and a ledger entry is written.
- **Insufficient balance:** calling a premium model once the daily trial is used up *and* the balance is zero returns `402 insufficient_balance`.

### Legacy VIP migration

On the first startup after upgrading, any previous VIP payments recorded in `payment_transactions` are automatically converted into wallet credit (`amount_THB / USD_TO_THB`) and logged as a `migration` ledger entry. This runs once (guarded by the `wallet_migrated_v1` setting).

### Atomic settlement & auditability

- **Atomic settlement:** the trial counter, wallet debit, ledger entry and request-log snapshot for a request are written inside a single `BEGIN IMMEDIATE` transaction (`record_usage` in `app/services/user_service.py`). Concurrent requests for the same user can therefore neither double-grant the free trial nor lose a charge (no lost updates).
- **Per-request audit log:** every request (including blocked `402`s) is recorded in `request_logs` with `tokens_in`/`tokens_out`, `trial_tokens`, `paid_tokens`, `cost_usd`, `is_premium`, `balance_after` and `usage_source` (`upstream`, `estimated`, or `rejected`). View it at **Admin → Request Log** (`/admin/requests`) with filters by user (**type-ahead search** by username or ID — no scrolling a huge dropdown), model, date range, status and tier, plus **CSV export** and a **14-day free-trial history** per user/model.
- **Wallet reconciliation:** **Admin → Wallet Reconciliation** compares each stored balance against the sum of that user's ledger entries (`expected = Σ credits + Σ debits`). A non-zero `Balance Diff` means a write bypassed the ledger and must be investigated.

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
