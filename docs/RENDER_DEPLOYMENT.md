# Kite Backend — Render Web Service Deployment Guide

**Target Service:** Render Web Services (Free Instance Tier)
**Application:** Learn Python With Kite — FastAPI REST Backend
**Frontend Origin:** `https://learnpythonwithkite.vercel.app` (Hosted on Vercel)
**Database:** Supabase Hosted PostgreSQL 17.11 (`izdtxorekfpasdkgasvs`, `eu-west-1`)
**Repository:** `https://github.com/Hanzo-Hasashi/Learn-Python-With-Kite.git` (`main` branch)

---

## 1. Hosting Architecture

Kite uses a decoupled, modern cloud architecture designed for zero recurring infrastructure costs on free tiers:

```
[ Learner Browser ]
       │
       ▼
[ Vercel CDN ] ─── (Frontend: Static HTML/CSS/JS at https://learnpythonwithkite.vercel.app)
       │
       │ API Requests (CORS / Same-Origin Proxy)
       ▼
[ Render Web Service ] ─── (FastAPI on Python 3.12, Uvicorn ASGI on 0.0.0.0:$PORT)
       │
       │ TLS (sslmode=require) via Transaction Pooler (:5432)
       ▼
[ Supabase PostgreSQL 17.11 ] ─── (Restricted kite_runtime role, 21 application tables)
```

* **Frontend:** Hosted statically on Vercel with automated global CDN edge caching.
* **Backend:** Hosted on Render as a Web Service running FastAPI with an asynchronous Uvicorn ASGI server.
* **Database:** Hosted on Supabase (PostgreSQL 17.11, AWS `eu-west-1` Ireland). Connects using the restricted `kite_runtime` database role with TLS forced (`sslmode=require`).
* **Security & Authorization:** Application-level authorization is enforced by FastAPI (`current_user`, `optional_user`, `playground_user`). Direct Supabase client access (PostgREST / Data API) has all table and sequence grants revoked.

---

## 2. Why Native Python Deployment Was Chosen

Render supports both **Native Python Web Services** and **Docker-based Web Services**:

1. **Native Python Web Service (`runtime: python` - Selected Primary Method):**
   * **Resource Efficiency:** Native Python runs directly on Render's managed Linux runtime without container virtualization overhead. On Render's Free tier (0.1 CPU, 512 MB RAM limit), saving memory is essential to prevent Out-Of-Memory (OOM) crashes.
   * **Build Performance:** Dependencies are installed via `pip install -r requirements.txt` and cached natively in Render's build cache, resulting in 2-3× faster builds compared to building multi-layer Docker images.
   * **Simplicity:** No Docker daemon storage quotas or base-image security patching to manage.
2. **Docker Alternative (`Dockerfile`):**
   * The repository retains a verified, hardened `Dockerfile` (`python:3.12-slim`, non-root user `10001:10001`, dynamic `${PORT:-8000}`). If Docker-based deployment is preferred in the future, it is fully compatible with Render by switching the runtime to `docker`.

---

## 3. Required Render Service Settings

| Setting | Value | Notes |
| :--- | :--- | :--- |
| **Service Type** | Web Service | Selected during service creation in Render Dashboard. |
| **Name** | `kite-backend` | Assigned production URL: `https://kite-backend-958r.onrender.com`. |
| **Region** | Frankfurt (EU Central) | Lowest latency to Supabase `eu-west-1` (Ireland). |
| **Branch** | `main` | Production branch. |
| **Root Directory** | *(leave blank)* | Builds and runs from the repository root. |
| **Runtime** | `Python 3` | Native Python runtime. |
| **Plan Type** | Free | 0.1 CPU, 512 MB RAM, 750 free instance hours/month. |
| **Build Command** | `pip install -r requirements.txt` | Installs verified dependencies. |
| **Start Command** | `uvicorn backend.app:app --host 0.0.0.0 --port $PORT` | Production ASGI start command. |
| **Health Check Path** | `/health` | Render verifies HTTP 200 before routing traffic. |
| **Auto-Deploy** | `No` (recommended initially) | Allows controlled manual deployments from dashboard. |

---

## 4. Repository and Root Directory Configuration

* **Root Directory:** Leave blank (defaults to `.`).
* The build command `pip install -r requirements.txt` executes from the repository root.
* The start command `uvicorn backend.app:app --host 0.0.0.0 --port $PORT` executes from the repository root, allowing Python to locate the `backend` package and load `backend/app.py`.
* Static assets in `web/` and migrations in `alembic/` remain in their established locations relative to repository root.

---

## 5. Build Command

```bash
pip install -r requirements.txt
```

* Dependency versions are pinned within compatible semantic ranges (`fastapi>=0.115,<1`, `uvicorn>=0.30,<1`, `sqlalchemy>=2.0,<3`, `psycopg[binary]>=3.2,<4`, `cryptography>=43,<46`).
* Python version is pinned to `3.12.8` via the `PYTHON_VERSION` environment variable and `.python-version`.

---

## 6. Start Command

```bash
uvicorn backend.app:app --host 0.0.0.0 --port $PORT
```

* **ASGI Server:** Uvicorn.
* **Host Binding:** `0.0.0.0` (required for Render's reverse proxy to route external traffic into the container).
* **Port:** `$PORT` (Render dynamically injects this environment variable; defaults to 10000 on Render).
* **Worker Count:** Single process (1 worker). Do **NOT** run multi-worker Gunicorn on Render Free; multiple Python processes with FastAPI and SQLAlchemy models would exceed the 512 MB memory limit and trigger OOM killer restarts.

---

## 7. Health-Check Endpoints

Kite provides two distinct health endpoints:

### A. Liveness Probe: `GET /health` (Render Health Check Path)
* **URL:** `https://<render-service-name>.onrender.com/health`
* **Response:** `{"status": "ok"}` with HTTP 200.
* **Execution:** Ultra-lightweight and fast (<2 ms). Does not query the database, writes zero data, requires no authentication, and is exempt from origin/proxy checks.
* **Purpose:** This is the exact endpoint configured in Render's **Health Check Path**. Render pings this endpoint during new deployments and will abort the deploy if it does not return HTTP 200 within 5 seconds.

### B. Readiness Probe: `GET /health/ready` (Deep Inspection)
* **URL:** `https://<render-service-name>.onrender.com/health/ready`
* **Response:** `{"status": "ready", "database": "connected"}` with HTTP 200 (or HTTP 503 if disconnected).
* **Execution:** Performs a lightweight `SELECT 1` query using the database connection pool.
* **Purpose:** Used for manual verification or external synthetic monitoring when deep database reachability confirmation is required.

---

## 8. Environment Variables and Configuration

Configure these in the Render Dashboard under:
**Kite Service -> Environment -> Environment Variables / Secret Files**.

### Mandatory Production Variables

| Variable | Example / Recommended Value | Description |
| :--- | :--- | :--- |
| `KITE_ENV` | `production` | Enforces production security checks, HTTPS cookies, and PostgreSQL. |
| `PYTHON_VERSION` | `3.12.8` | Pins Render's native Python runtime version. |
| `LOG_LEVEL` | `INFO` | Standard production logging level. |
| `KITE_SECRET_KEY` | *(64-character random hex string)* | Signs session tokens and CSRF tokens. Generate with `python -c "import secrets; print(secrets.token_hex(32))"`. |
| `KITE_ALLOW_LOCAL_AUTH` | `false` | Disables insecure fallback authentication. |
| `DATABASE_URL` | `postgresql+psycopg://kite_runtime.izdtxorekfpasdkgasvs:[PASSWORD]@aws-0-eu-west-1.pooler.supabase.com:5432/postgres?sslmode=require` | Restricted runtime user connection string. Must include `sslmode=require`. |
| `SUPABASE_URL` | `https://izdtxorekfpasdkgasvs.supabase.co` | Supabase project origin. |
| `SUPABASE_ANON_KEY` | *(publishable anon key)* | Supabase client anon key (from Project Settings -> API). |
| `PUBLIC_APP_URL` | `https://learnpythonwithkite.vercel.app` | Confirmed production frontend HTTPS origin. |
| `KITE_CORS_ORIGINS` | `https://learnpythonwithkite.vercel.app` | Exact allowed CORS origin. Wildcard `*` and `localhost` are rejected in production. |
| `KITE_COOKIE_SECURE` | `true` | Enforces `Secure` flag on all session cookies. |
| `KITE_COOKIE_SAMESITE` | `lax` | Cookie SameSite policy. |

### Architectural Integration Mode (Choose Option A or Option B)

#### Option A: Vercel Same-Origin Serverless Proxy Mode (Implemented & Recommended)
Browsers enforce strict cross-site cookie boundaries between `vercel.app` and `onrender.com` (both are on the Public Suffix List). Vercel Proxy Mode routes `/api/*` requests through Vercel's serverless proxy function (`web/api/proxy.js`) to Render, injecting the private `x-kite-proxy-secret` header server-side:
* **Proxy Handler:** Implemented in `web/api/proxy.js` (and mirrored in `api/proxy.js`).
* **Vercel Rewrite:** Configured in `web/vercel.json` routing `/api` and `/api/:match*` to `/api/proxy`.
* **Vercel Environment Variables (Configure in Vercel Dashboard):**
  * `KITE_PROXY_SECRET`: Shared secret string of at least 32 characters (must match Render).
  * `KITE_BACKEND_URL`: `https://kite-backend-958r.onrender.com` (Render service HTTPS origin).
* **Render Environment Variables (Configure in Render Dashboard):**
  * `KITE_VERCEL_PROXY`: `true`
  * `PUBLIC_API_URL`: `https://learnpythonwithkite.vercel.app`
  * `KITE_PROXY_SECRET`: Identical shared secret string of at least 32 characters.
* **Cookie Security:** Cookies (`kite_access`, `kite_session`) remain strictly host-only on `learnpythonwithkite.vercel.app` with `HttpOnly; Secure; SameSite=Lax`. The browser never sees or handles `KITE_PROXY_SECRET`.

#### Option B: Direct Cross-Origin Mode with Custom Domain
If using a custom registrable domain (e.g. `learnpythonwithkite.com`):
* `KITE_VERCEL_PROXY`: `false`
* `PUBLIC_API_URL`: `https://api.learnpythonwithkite.com`
* `KITE_COOKIE_SITE`: `learnpythonwithkite.com`

### Mandatory Gated Payment Flags (Must Remain FALSE)
* `KITE_LENCO_COLLECTION_ENABLED`: `false`
* `KITE_LENCO_ZMW_COLLECTION_CONFIRMED`: `false`
* `KITE_PAWAPAY_ENABLED`: `false`

---

## 9. Supabase Connection Requirements

* **Role:** Must connect exclusively as `kite_runtime` (user `kite_runtime.izdtxorekfpasdkgasvs`).
* **Privileges:** `kite_runtime` has strict least-privilege permissions (no DDL, no superuser, no bypass RLS, read-only on reference data, denied access to `alembic_version`).
* **Connection Pooling:** Use Supabase's AWS connection pooler hostname:
  `aws-0-eu-west-1.pooler.supabase.com` on port `5432` (or transaction pooler port `6543`).
* **SSL Requirement:** The connection string must include `?sslmode=require`. The application will refuse to start if SSL mode is missing or insecure.
* **Startup Safety:** Ordinary application startup (`startup()`) does **not** run DDL migrations and does **not** seed reference data. It verifies catalog schema matching using system catalogs.

---

## 10. Step-by-Step GitHub Deployment Instructions

1. **Sign in to Render:** Go to [dashboard.render.com](https://dashboard.render.com).
2. **Create New Web Service:**
   * Click **New +** -> **Web Service**.
   * Select **Build and deploy from a Git repository**.
   * Connect your GitHub account and select `Hanzo-Hasashi/Learn-Python-With-Kite`.
3. **Configure Settings:**
   * **Name:** `kite-backend`
   * **Region:** `Frankfurt (EU Central)`
   * **Branch:** `main`
   * **Runtime:** `Python 3`
   * **Build Command:** `pip install -r requirements.txt`
   * **Start Command:** `uvicorn backend.app:app --host 0.0.0.0 --port $PORT`
   * **Plan:** `Free`
4. **Configure Advanced Settings:**
   * **Health Check Path:** `/health`
   * **Auto-Deploy:** `No` (recommended for first deployment so environment variables can be configured before the first build triggers).
5. **Add Environment Variables:**
   * Enter all mandatory variables listed in Section 8 (or paste from `.env.render.example`).
6. **Trigger Deployment:**
   * Click **Create Web Service** (or **Manual Deploy** -> **Deploy latest commit**).
   * Monitor build logs. Deployment should complete in approximately 1 to 2 minutes.
   * Render will show status `Live` once `/health` returns HTTP 200.
7. **Note Your Backend URL:**
   * Copy the assigned Render URL (e.g. `https://kite-backend.onrender.com`).

---

## 11. Automated Keep-Alive via GitHub Actions & cron-job.org

Render Free Web Services automatically spin down ("sleep") after 15 minutes of inbound HTTP inactivity. To keep the service responsive during active hours:

1. **GitHub Actions Scheduled Keep-Alive (Included in Repository):**
   * Workflow file: `.github/workflows/keep-alive.yml`
   * **Schedule:** Every 10 minutes (`*/10 * * * *`)
   * **Target Endpoint:** `https://kite-backend-958r.onrender.com/health`
   * Automatically executes on GitHub hosted runners to ping the liveness endpoint, preventing idle sleep.

2. **Optional External Ping via cron-job.org:**
   * **URL:** `https://kite-backend-958r.onrender.com/health`
   * **Execution Schedule:** Every 10-12 minutes (`*/10 * * * *`).
   * **Request Method:** `GET`.
   * **Failure Notification:** Enable email alerts if the endpoint fails 3 consecutive times.
3. **Critical Limitations of External Pings:**
   * **Monthly Free Hours Limit:** Render Free accounts receive 750 free instance hours per calendar month across all free services. A single service running continuously 24/7 uses 744 hours in a 31-day month ($31 \times 24 = 744$). If you run more than one free service or have multiple rebuilds, your 750 hours will be exhausted before the month ends, causing Render to suspend the service until the 1st of the next month.
   * **Platform Spin-Downs:** Render reserves the right to spin down free instances regardless of synthetic pings during maintenance cycles.
   * **Conclusion:** Use cron-job.org to prevent cold-starts during learner waking hours (e.g., schedule it between 06:00 and 23:00 UTC), rather than expecting guaranteed 24/7 commercial uptime from a free plan.

---

## 12. Render Free-Tier Sleeping and Restart Limitations

* **Cold Starts:** If the service has spun down due to 15 minutes of inactivity, the next incoming HTTP request will experience a 30 to 50-second cold-start delay while Render provisions the container and boots Python.
* **Single Instance:** Free services run on exactly 1 instance. Zero-downtime rolling deploys are not available on the free tier (the old container stops before the new container starts).
* **Ephemeral Memory:** In-memory rate limiting dictionaries (`auth_attempts`) reset when the service restarts. Persistent abuse tracking is backed by database session checks.

---

## 13. Persistent Storage Findings

* **Stateless Backend:** The Kite backend is 100% stateless with respect to the local disk.
* **No File Uploads:** Kite does not accept local user file uploads or store media assets on disk.
* **Supabase Persistence:** All learner accounts, progress, subscriptions, quiz results, streak data, and audit activity persist safely in Supabase PostgreSQL.
* **Ephemeral Disk Impact:** Render's ephemeral filesystem has **zero impact** on Kite's business data. Restarts, redeployments, or instance migrations will not lose any user data.

---

## 14. Background-Job Compatibility Findings

An exhaustive audit of the backend confirms:
* **No Internal Schedulers:** The backend does **not** run APScheduler, Celery, Celery Beat, or continuous asyncio background loops.
* **On-Demand Processing:** All subscription checks and payment reconciliations are triggered strictly on-demand by incoming HTTP requests (user actions or payment provider webhooks).
* **Subscription Expiry:** Subscriptions expire naturally when `expires_at <= utcnow()`. There is no background worker that must run to revoke expired access.
* **Webhook Reliability:** Payment webhooks (`/api/payment-webhooks/{provider_id}`) will wake up a sleeping Render service, process the payload, update the database, and respond to the payment gateway.

---

## 15. Deployment Verification Checklist

After deploying to Render, verify the service in sequence:

- [ ] **Liveness Probe:** `curl -i https://<render-service-name>.onrender.com/health` returns `HTTP 200` with `{"status":"ok"}`.
- [ ] **Readiness Probe:** `curl -i https://<render-service-name>.onrender.com/health/ready` returns `HTTP 200` with `{"status":"ready","database":"connected"}`.
- [ ] **CORS Preflight:**
  ```bash
  curl -i -X OPTIONS https://<render-service-name>.onrender.com/api/me \
    -H "Origin: https://learnpythonwithkite.vercel.app" \
    -H "Access-Control-Request-Method: GET" \
    -H "Access-Control-Request-Headers: authorization"
  ```
  Returns `HTTP 200` with `Access-Control-Allow-Origin: https://learnpythonwithkite.vercel.app`.
- [ ] **Unlisted Origin Rejection:**
  ```bash
  curl -i -X OPTIONS https://<render-service-name>.onrender.com/api/me \
    -H "Origin: https://unauthorized-domain.com" \
    -H "Access-Control-Request-Method: GET"
  ```
  Does **not** return `Access-Control-Allow-Origin: https://unauthorized-domain.com`.
- [ ] **Lesson Catalog Read:** `curl -i https://<render-service-name>.onrender.com/api/lessons` returns `HTTP 200` with the 101 lessons.
- [ ] **Authentication Flow:** Register a test learner account and verify login returns valid tokens.

---

## 16. Rollback Instructions

If a newly deployed commit introduces errors:
1. Open the **Kite Web Service** in the Render Dashboard.
2. Navigate to the **Events** tab.
3. Locate the previous successful deployment event.
4. Click the three dots (`...`) next to the successful deployment and select **Rollback to this deploy**.
5. Render will immediately redeploy the previous container artifact.
6. Alternatively, revert the commit on GitHub `main` and trigger a manual redeploy.

---

## 17. Requirements Before Accepting Annual Subscriptions

Before enabling and accepting live mobile-money subscriptions from paying users:

1. **Lenco Commercial Contract Confirmation:** Complete merchant onboarding with Lenco Zambia and verify live API credentials. Keep `KITE_LENCO_COLLECTION_ENABLED=false` until payload specifications and settlement terms are officially verified.
2. **Dedicated Backend Instance:** Upgrade Render from the Free tier to a paid instance (e.g. Starter at $7/month) to eliminate the 15-minute spin-down sleep, ensure 24/7 availability for webhooks, and remove the 750 free-hour monthly cap.
3. **Automated Database Backups:** Verify logical database dump automation or upgrade to a Supabase plan with automated Point-in-Time Recovery (PITR).
4. **Isolated Code Runner:** If interactive Python code execution in challenges is advertised as a premium feature, deploy the isolated `runner` service with its dedicated resource quotas.
