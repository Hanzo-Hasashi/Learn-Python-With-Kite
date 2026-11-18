# Learn Python with Kite

Kite is built around the supplied HTML prototype. `web/index.html` is the prototype with a small API bridge appended; `prototype-reference.html` preserves the visual reference with the same neutralized metric and notification placeholders as the live prototype. The screen design, mascot, animations, routes and base interactions come from that prototype. The subscription section is added to the existing Settings screen.

## What is included

- The original Kite screens and navigation, served from the prototype.
- A FastAPI backend with Supabase Auth integration (plus a local-only password-auth fallback), password reset, profiles, preferences, lessons, progress, XP, streaks, quiz/challenge endpoints, leaderboard and notifications. Guests can browse the leaderboard and their device-local profile/progress, and can submit free quizzes and challenges; guest results are evaluated but are not saved to an account or awarded XP. Personal leaderboard rank and account changes remain sign-in features.
- A 12-module Python learning path. The six existing lesson entries retain their IDs and content; six new advanced, Premium-gated modules add 48 lessons, 12 quizzes, and 60 server-evaluated coding challenges. The 25 topic quizzes are organized by module, use the same entitlement checks as lessons, connect to the existing coding challenges, and provide explanations and revision links. Every lesson has a context-specific assessment in its Quiz tab, using the existing assessment runner; the Home Assessment Centre catalog remains unchanged. Signed-in attempts and best scores are stored per account; guest attempt history is stored on the current device.
- SQLite persistence for local development; SQLAlchemy can use Supabase PostgreSQL through `DATABASE_URL`.
- Premium lesson entitlement checks and Google Play purchase verification through the Android Publisher API. Purchase tokens are encrypted at rest.
- A provider-neutral subscription registration journey with Zambia phone normalization, encrypted number storage, pending payment tracking, retryable attempts, verification, webhook, and status-history records. No provider is enabled by default, and registration alone never activates Premium.
- An opt-in PawaPay mobile-money checkout for prepaid Zambia Premium access. It reuses the existing subscription entitlement checks and records payment attempts separately; it is disabled by default.
- An independent Python runner service, reachable only on a private internal Docker network. The trusted service runs with narrowly scoped container capabilities; every learner program runs under a separate unprivileged UID with syscall, CPU, memory, process, filesystem and output restrictions. The runner has no network route.
- The existing Playground editor keeps syntax highlighting and multiline editing, updates line numbers, supports indentation and Run/Save keyboard shortcuts, and provides Run, Stop, Reset, Copy, Save, Clear Console and context-aware Exit controls. Guest and signed-in users can submit code without an authentication redirect; guest cancellation uses a short-lived per-run capability token. Unsaved code/output is retained for the browser session; explicitly saved code is stored on the current device and is not synchronized to the account.
- A Capacitor Android shell configuration.

Signed-in sessions use HTTP-only, same-site cookies and a per-user CSRF header; access and refresh tokens are not exposed to browser JavaScript or stored in browser storage. Sessions last up to eight hours by default, or 30 days when the learner selects “Keep me signed in on this device.” The server remains the source of truth for account, subscription and authenticated learning data. Guest lesson activity and quiz attempts are device-local and are not synchronized to an account automatically. The prototype itself continues to use browser storage for its original cosmetic theme and demo-only values; server-backed settings are also synchronized through the API.

## Quick start

For Docker Compose, create a local `.env` file in the project root with at least the following values:

```powershell
KITE_ENV=development
KITE_SECRET_KEY=replace-with-a-random-32-character-string
RUNNER_SHARED_SECRET=replace-with-a-different-random-32-character-string
DATABASE_URL=sqlite:////data/kite.db
```

You can start from `.env.example`, then replace both secret placeholders with different random values. Docker Compose reads `.env` automatically. A plain `uvicorn` process does not load `.env` by itself; set the environment variables in the shell as shown below. Local password authentication is disabled unless `KITE_ALLOW_LOCAL_AUTH` is explicitly enabled.

### Docker (recommended)

```powershell
docker compose build
docker compose run --rm --no-deps app alembic upgrade head
docker compose up
```

Migrations are an explicit step and do not run from FastAPI startup. Repeat the migration command after pulling a schema revision, before restarting the app.

Then open `http://localhost:8000` in the browser.

### Local Python development server

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:KITE_SECRET_KEY = "replace-with-a-random-value-of-at-least-32-characters"
$env:KITE_ENV = "development"
$env:DATABASE_URL = "sqlite:///./kite.db"
# Optional: enable password authentication for local development only.
$env:KITE_ALLOW_LOCAL_AUTH = "true"
alembic upgrade head
uvicorn backend.app:app --reload --host 0.0.0.0 --port 8000
```

Then open `http://localhost:8000`.

If you run without Docker, the code-runner and `Stop` action will report runner-unavailable errors unless a separate runner service is configured.

## Run locally with Docker

1. Install Docker Desktop with Compose support. Linux containers and the `libseccomp` runtime library are required for code execution.
2. Create an untracked `.env` file. Set `KITE_SECRET_KEY` and `RUNNER_SHARED_SECRET` to distinct random values of at least 32 characters; keep both values in the deployment secret manager outside local development.
3. Keep `DATABASE_URL=sqlite:////data/kite.db` for a local SQLite database. Set `RUNNER_SHARED_SECRET` in `.env`; no blank value is accepted by the execution service.
4. From this directory run `docker compose up --build`.
5. Open `http://localhost:8000`. Health checks are available at `/health`.

The API and web application share one origin. For a Supabase production deployment, set `DATABASE_URL` to the Supabase PostgreSQL connection string with SSL enabled, and set `KITE_SECRET_KEY` to a stable, secret random value of at least 32 characters. The key is required to encrypt registered phone numbers; rotating it makes previously stored numbers unreadable and invalidates existing sessions. `SUPABASE_URL` and `SUPABASE_ANON_KEY` must either both be set or both be empty. They may remain empty while Supabase Auth is intentionally unavailable.

For Vercel serverless functions, use Supabase's shared transaction pooler connection string (port 6543) with the host and username copied from the Supabase Connect dialog. When Vercel is detected, Kite disables Psycopg prepared statements and limits its SQLAlchemy pool to one connection per function instance. For a persistent backend, use the direct connection when the host supports IPv6 (or the IPv4 add-on); otherwise use the shared session pooler. Keep database and service credentials in the deployment secret manager, not in the ZIP or source tree. Back up the database before applying migrations.

When authentication is configured, set `PUBLIC_APP_URL` to the Kite HTTPS origin. In Supabase Auth, enable Google, enter the OAuth client ID and secret from Google Cloud, allow the Supabase callback URL shown in its Google provider setup, and set the Supabase Site URL and redirect allowlist to the Kite HTTPS origin (plus any intentional local development origin).

If the static frontend and API are deployed on different origins, set `window.KITE_API_BASE` in `web/assets/config.js` to the API's HTTPS origin and set `KITE_CORS_ORIGINS` on the API to a comma-separated list of exact frontend origins (for example, `https://learn.example.com`). The API must allow the `X-Kite-Registration-Key` header for guest subscription status and management requests. Do not use wildcard origins with credentialed authentication. Production cookie authentication additionally requires the shared custom-domain configuration described below; allowing an origin through CORS alone does not make cross-site cookies reliable.

### Running the API without Docker

Use Python 3.12+, create and activate a virtual environment, then run:

```powershell
python -m pip install -r requirements.txt
```

Keep the same `.env` and `KITE_SECRET_KEY` across server restarts; `.env` is ignored by Git and changing the key invalidates locally issued sign-in tokens.

In this mode authentication, learning and settings endpoints run, but code execution and Stop intentionally report runner-unavailable errors unless a separately isolated runner is configured. The Playground does not run its prototype JavaScript interpreter as a Python fallback. Do not execute learner code inside the API process.

## Render + Supabase backend preparation

The frontend remains on Vercel; this section prepares the existing FastAPI backend for a separate Render web service and Supabase Postgres/Auth. No Render service, Supabase project, or live configuration is created by these instructions. Render explicitly describes its Free instances as unsuitable for production: they spin down after 15 minutes without inbound traffic, take about a minute to wake, and have an ephemeral filesystem. Use a Free web service only for preview or staging; never rely on its filesystem or SQLite for persistent data. Keep durable data in Supabase Postgres.

### Render web service

Create a Web Service from this repository using the repository root as its root directory and `main` as the branch. Select Python and a region also available for the Supabase project; Frankfurt is one possible shared region, but latency from Zambia has not been measured. The root `.python-version` selects the latest available Python 3.12 patch, matching the project's `>=3.12` requirement and Docker runtime.

Use these commands and settings:

| Render setting | Value |
| --- | --- |
| Build command | `pip install -r requirements.txt` |
| Start command | `uvicorn backend.app:app --host 0.0.0.0 --port $PORT` |
| Health check path | `/health` |

Render supplies `PORT`; do not hardcode a port. `/health` is a liveness response, not an end-to-end database or authentication check. Run reviewed migrations from a trusted one-off environment before starting the service; the web process performs read-only migration/schema checks and refuses to start if the database is not current.

### Required Render environment variables

Set these in the Render dashboard's environment settings, not in Git or the frontend:

| Variable | Required value |
| --- | --- |
| `KITE_ENV` | `production` (fails closed if a production deployment omits its PostgreSQL URL) |
| `DATABASE_URL` | Supabase Postgres connection string for the `kite_runtime` role created below. A persistent backend can use the direct connection when Render has IPv6 reachability; otherwise use Supabase's shared **Session Pooler** URL (port 5432). Preserve the copied host, adapt the username to the `kite_runtime` role using Supabase's documented pooler format, percent-encode reserved characters in the database password, and include `sslmode=require` (or stronger certificate verification). Do not use the transaction pooler intended for short-lived serverless connections. |
| `KITE_SECRET_KEY` | A stable, private random value of at least 32 characters. Keep it unchanged: rotation invalidates sessions and makes existing encrypted phone numbers unreadable. |
| `SUPABASE_URL` | The HTTPS Project URL from the Supabase project settings. |
| `SUPABASE_ANON_KEY` | The project's publishable key (or legacy anon key); this is the variable name the backend currently reads. Never put a service-role/secret key here. |
| `PUBLIC_APP_URL` | The frontend's custom HTTPS origin, e.g. `https://learn.example.com` (no path). |
| `PUBLIC_API_URL` | The API's custom HTTPS origin, e.g. `https://api.example.com` (no path). |
| `KITE_COOKIE_SITE` | The shared registrable custom domain, e.g. `example.com`; both public origins must be that domain or one of its subdomains. |
| `KITE_CORS_ORIGINS` | The exact frontend custom origin, e.g. `https://learn.example.com`; add only other exact origins actually used. Never use `*` with credentialed requests. |
| `KITE_COOKIE_SECURE` | `true` |
| `KITE_COOKIE_SAMESITE` | `lax`; frontend and API must be under the same registrable custom domain. |

`KITE_ALLOW_LOCAL_AUTH` must remain unset or `false`. `LOG_LEVEL=INFO` is optional. Render supplies its own `RENDER` and `PORT` values; do not set these manually. Keep `MIGRATION_DATABASE_URL` out of the web service environment; it is a separate, short-lived, privileged connection for trusted migration commands only. Keep payment provider credentials, Google Play service-account credentials, and runner secrets unset unless those separate integrations have been deliberately configured and verified. `KITE_PAWAPAY_ENABLED` must remain `false` until merchant onboarding and payment tests are complete. Backend `SMTP_*` settings do not configure Supabase Auth email.

Lenco is not implemented in this repository. Do not configure or advertise Lenco payments until a provider adapter and signed-webhook verification are implemented and validated against Lenco's current integration documentation and sandbox.

### Supabase setup

1. Create a Supabase project and set a strong database password. Copy the connection string from **Connect**; do not construct the pooler hostname or username manually. For Render's persistent process, prefer the direct connection only after confirming IPv6 reachability; otherwise choose Session Pooler. The connection password belongs only in Render's `DATABASE_URL`.
2. Before applying the migration, connect as the database owner and run `migrations/bootstrap_runtime_role.psql` with `psql -v ON_ERROR_STOP=1 -f migrations/bootstrap_runtime_role.psql $env:MIGRATION_DATABASE_URL`. The script prompts for a unique password for `kite_runtime`; store that password only in the restricted runtime `DATABASE_URL`. It removes public-schema CREATE inherited by all users, grants the runtime role only schema usage, and does not grant application-table access until the security migration.
3. In a trusted migration environment, set `MIGRATION_DATABASE_URL` to the owner connection string and run `alembic upgrade head` for a fresh database. The fresh baseline creates all application tables; the next revision enables RLS, restricts client-role table/sequence access, and grants the runtime role only required DML and sequence access. Use the same migration owner for later revisions. Clear `MIGRATION_DATABASE_URL` from the process environment after migration.
4. For an existing Kite database, take and verify a backup first. Set `MIGRATION_DATABASE_URL`, run `python -m backend.migration_tools preflight`, and proceed only if it reports an exact baseline match or only the specifically listed additive compatibility columns/indexes. Then run `alembic stamp d9f9f9ebbced`, `alembic upgrade head`, and `alembic check`. The stamp records the existing schema baseline; it does not apply the initial table-creation migration. Any other difference is a stop condition and must be reviewed separately. Never run the legacy `migrations/20261116_subscription_payments.sql`; its schema is now managed by Alembic.

Example trusted-migration commands in PowerShell:

```powershell
$env:MIGRATION_DATABASE_URL = "<owner connection URL>"
# Fresh, empty database:
alembic upgrade head
alembic check

# Existing database only, after backup and successful preflight:
python -m backend.migration_tools preflight
alembic stamp d9f9f9ebbced
alembic upgrade head
alembic check

Remove-Item Env:\MIGRATION_DATABASE_URL
```

Run either the fresh-database commands or the existing-database commands, not both. Do not copy the migration-owner URL into Render's service environment.
5. In **Authentication → URL Configuration**, set the Site URL to the frontend custom origin (for example, `https://learn.example.com`). Add the exact production redirect URLs used by the app, such as `https://learn.example.com/` and `https://learn.example.com/#reset`. Add local redirect URLs only to a development project or when intentionally testing locally.
6. Enable email authentication. Supabase's default SMTP is restricted to project team members and has a low rate limit; configure a custom SMTP provider in Supabase Auth before sending email to real learners. Render Free also blocks outbound SMTP ports, so do not depend on the backend's local SMTP fallback.
7. For Google sign-in, configure the Google provider in Supabase Auth. In Google Cloud, add the callback URI displayed by the Supabase provider setup (the project-specific Supabase `/auth/v1/callback` URL); do not guess the project reference or callback value.
8. The frontend is a separate Vercel static site. Set `window.KITE_API_BASE` in `web/assets/config.js` to the Render API custom origin (for example, `https://api.example.com`) and redeploy the existing Vercel project. Keep the corresponding frontend origin in Render's `KITE_CORS_ORIGINS`.

For production, keep Vercel and Render but map custom hostnames under one registrable domain, such as `learn.example.com` to Vercel and `api.example.com` to Render. Set `window.KITE_API_BASE` in `web/assets/config.js` to the API custom origin, retain the exact frontend origin in `KITE_CORS_ORIGINS`, and use `KITE_COOKIE_SAMESITE=lax`. Do not deploy the current `vercel.app`/`onrender.com` cross-site cookie configuration: third-party-cookie restrictions can break authentication. The actual domain and DNS are operator-provided and are not configured here. Verify sign-in, refresh, sign-out, CSRF, password recovery, and CORS in the target browsers before release; browser compatibility has not been tested in this repository.

### Database and runtime limitations

Alembic migrations are versioned and forward-only; automatic destructive downgrades are intentionally unsupported. FastAPI startup performs no table creation, schema repair, or DDL. It verifies the current migration revision and model/schema compatibility, checks the restricted runtime-role privileges, and then retains the existing idempotent lesson and plan synchronization. Use `alembic check` after applying migrations to catch model/migration drift. The runtime role is deliberately `BYPASSRLS` because this API uses its own authenticated data-access checks and does not set per-user PostgreSQL RLS context; never expose its connection string to the browser or Supabase client.

The backend's `/health` route is a liveness response and does not test database or authentication flows. PostgreSQL-specific migrations, Supabase role grants, and cookie behavior have not been verified against a live Supabase project or deployed Render service. Production startup requires Vercel and Render custom hostnames under the configured shared domain and `SameSite=Lax`; the default `vercel.app` and `onrender.com` origins are intentionally rejected. Real-browser authentication testing remains a deployment gate. The Python runner is a separate isolated Docker service; no hosted runner configuration is included here.

Official references: [Render Python version](https://render.com/docs/python-version), [Render web services](https://render.com/docs/web-services), [Render Free limitations](https://render.com/docs/free), [Supabase Postgres connection methods](https://supabase.com/docs/guides/database/connecting-to-postgres), [Supabase Auth redirect URLs](https://supabase.com/docs/guides/auth/redirect-urls), [Supabase custom SMTP](https://supabase.com/docs/guides/auth/auth-smtp), and [Supabase Google sign-in](https://supabase.com/docs/guides/auth/social-sign-in/auth-google).

## Google Play subscriptions

1. Create monthly and annual auto-renewing subscription products in Play Console. By default, use product IDs `kite_premium_monthly` and `kite_premium_annual`, or set `KITE_PLAY_MONTHLY_PRODUCT_ID` and `KITE_PLAY_ANNUAL_PRODUCT_ID` to the exact IDs you create.
   Set the Zambia storefront prices to K19.99 per month and K99.99 per year. The app displays those amounts, with the annual plan highlighted as Best Value and saving K139.89 annually (58.3% versus twelve monthly payments).
2. Set `GOOGLE_PLAY_PACKAGE_NAME` to the Android application ID/package name; enable the Google Play Android Developer API in the service account's Google Cloud project; link that project in Play Console; grant the service account access to the app with subscription/order read permissions; mount its JSON key as a deployment secret and set `GOOGLE_APPLICATION_CREDENTIALS` to its container path. Do not commit the key.
3. Configure the Android app to use the deployed API origin. In a packaged Capacitor build set `window.KITE_API_BASE` to that HTTPS origin before `web/assets/app.js` loads.
4. The Capacitor Google Play Billing bridge is not currently included. Implement or select a Capacitor 7-compatible bridge that exposes `window.Capacitor.Plugins.KiteBilling.purchase({productId})` and returns the verified Play purchase token as `purchaseToken`; it must also expose restoration and acknowledge/complete-purchase behavior. The backend verifies every token with Google; the client cannot grant its own entitlement.
5. Before release, test purchases, pending transactions, restore purchases, grace periods, account hold, expiration, refunds, and cancellation in Play license testing. The Settings screen links to Google Play subscription management and refreshes the authoritative state from the Play API.

Google Play credentials, Play Console products, signing keys, OAuth settings, and production Supabase credentials are deployment-specific and are not included. The `KITE_MONTHLY_PRICE` and `KITE_ANNUAL_PRICE` values control displayed plan text only; Google Play Console controls actual checkout amounts. Keep the storefront product prices and these display values in sync, and do not enable billing until the product prices and native billing bridge are configured and verified. Google Billing purchases and restoration have not been tested because this repository has no native billing bridge or Android project.

## Zambia mobile-money checkout

The optional legacy PawaPay checkout supports the documented Zambia providers `AIRTEL_OAPI_ZMB` (Airtel Money), `MTN_MOMO_ZMB` (MTN MoMo), and `ZAMTEL_ZMB` (Zamtel Kwacha), in ZMW. The backend checks the merchant account's live active configuration, provider availability, amount precision, and transaction limits before starting each deposit. Provider status is then checked server-to-server; the browser cannot grant Premium. That legacy checkout sends the phone number to PawaPay but does not store it in Kite. The separate provider-neutral registration flow stores the normalized number encrypted at rest and shares it with a provider only when a payment is initiated. Kite never requests or stores a mobile-money PIN.

To configure the integration:

1. Create a PawaPay sandbox merchant account, obtain a sandbox API token, and verify that the account's active configuration enables ZMB deposits for each provider you intend to offer. PawaPay provider availability is merchant-account-specific.
2. Set `KITE_PAWAPAY_API_TOKEN`, `KITE_PAWAPAY_ENV=sandbox`, and `KITE_PAWAPAY_ENABLED=true`. Set `KITE_PAWAPAY_MONTHLY_AMOUNT` and `KITE_PAWAPAY_ANNUAL_AMOUNT` to the exact numeric ZMW amounts you intend to charge; keep them aligned with `KITE_MONTHLY_PRICE` and `KITE_ANNUAL_PRICE`. Kite rejects prices with more than two decimal places, and the backend checks each provider's current configured limits.
3. Test accepted, completed, failed, duplicate, unavailable-provider, and delayed/reconciliation outcomes with PawaPay's sandbox test numbers. Kite polls PawaPay's authenticated deposit-status API when the user checks or returns to the checkout; no unauthenticated payment callback is trusted or required. Configure PawaPay not to send callbacks if the account requires an explicit callback preference.
4. Before enabling production, complete PawaPay merchant onboarding and production-provider activation, use the production API token, set `KITE_PAWAPAY_ENV=production`, and review the current prices, provider fees, settlement arrangement, customer disclosures, applicable Zambian requirements, and refund/support process. Keep `KITE_PAWAPAY_ENABLED=false` until operational and legal review is complete.

Mobile-money purchases are **prepaid**, not automatically renewing: a confirmed monthly payment grants 30 days and a confirmed annual payment grants 365 days. Users must make a new payment to renew. Additional mobile-money purchases extend an unexpired mobile-money term; a user with an active Google Play subscription is directed to manage it in Google Play instead. Kite grants access only after a provider status response confirms the expected deposit ID, completed status, Zambian country, ZMW amount, and selected provider. Unknown or mismatched outcomes remain unentitled for reconciliation. The app does not currently initiate mobile-money refunds; payment disputes or refund requests require the configured support process and remain subject to applicable law.

Provider capability and implementation status:

| Provider | Official PawaPay Zambia listing | Kite integration | Sandbox / production status |
| --- | --- | --- | --- |
| Airtel Money (`AIRTEL_OAPI_ZMB`) | ZMB / ZMW; two decimal places | Implemented behind the disabled-by-default PawaPay configuration gate; account active-configuration checks apply | No merchant credentials are available here, so neither a live sandbox transaction nor production checkout has been exercised |
| MTN MoMo (`MTN_MOMO_ZMB`) | ZMB / ZMW; two decimal places | Implemented behind the disabled-by-default PawaPay configuration gate; account active-configuration checks apply | No merchant credentials are available here, so neither a live sandbox transaction nor production checkout has been exercised |
| Zamtel Kwacha (`ZAMTEL_ZMB`) | ZMB / ZMW; two decimal places | Implemented behind the disabled-by-default PawaPay configuration gate; account active-configuration checks apply | No merchant credentials are available here, so neither a live sandbox transaction nor production checkout has been exercised |

These listings are based on PawaPay's [official Zambia provider documentation](https://docs.pawapay.io/v2/docs/providers.md), [active configuration API](https://docs.pawapay.io/v2/api-reference/toolkit/active-configuration.md), [deposit guide](https://docs.pawapay.io/v2/docs/deposits.md), [deposit status API](https://docs.pawapay.io/v2/api-reference/deposits/check-deposit-status.md), [sandbox guidance](https://docs.pawapay.io/v2/docs/how_to_start.md), and [go-live checklist](https://docs.pawapay.io/v2/docs/going_live.md), accessed 2026-11-15. Documentation coverage does not guarantee that any provider is enabled on Kite's merchant account or that the service is approved for production.

## Subscription registration and provider integration

The Settings subscription journey is usable even when no mobile-money processor is connected. Users can select the monthly or annual plan, register a Zambian mobile number, review the masked number and transaction reference, refresh status, and cancel a registration that has not entered provider processing. A saved registration remains `PENDING_PAYMENT`; its payment starts as `NOT_INITIATED`. Selecting a plan or saving a number never activates Premium. Premium is granted only after the backend accepts a signed/verified provider result whose transaction reference, amount, and currency match the stored intent.

Zambian mobile numbers are normalized to E.164 (`+260` plus nine national digits; mobile ranges starting with 7 or 9) on both client and server. The backend encrypts the full number with AES-GCM using a key derived from `KITE_SECRET_KEY`; only the last four digits are returned to clients. `KITE_SECRET_KEY` must be a stable, secret value of at least 32 characters and must be backed up securely: changing it makes existing encrypted phone numbers unreadable. Phone lookup and idempotency digests are keyed, and registration access keys are high-entropy capabilities stored only as hashes. Guest registrations can be viewed using the `X-Kite-Registration-Key` header and linked to a signed-in account before payment initiation. The `phone_ciphertext` is never returned by the API.

The historical [`migrations/20261116_subscription_payments.sql`](./migrations/20261116_subscription_payments.sql) is retained for reference and must not be applied. All application tables, including payment tables, are now managed by the Alembic revision chain described in the deployment section. The PostgreSQL security revision enables RLS and revokes direct client-role access; it does not create per-user RLS policies because requests use the server-side API authorization layer. Keep `DATABASE_URL` server-side. Do not grant the Supabase `anon` or `authenticated` roles direct access to payment or phone tables.

The provider boundary lives in [`backend/payments.py`](./backend/payments.py): adapters implement initiation, transaction verification, and signed-webhook verification and return the shared payment states `NOT_INITIATED`, `PENDING`, `PROCESSING`, `SUCCESSFUL`, `FAILED`, `CANCELLED`, or `EXPIRED`. Register the adapter in the backend provider registry and add an enabled `payment_provider_configurations` row; credentials belong in the host's secret manager/environment, never in that table or browser code. The provider initiation response cannot activate a subscription. Only a verified response/webhook with the exact reference, amount, and currency can create an active subscription; duplicate events are idempotent. Verification can also reconcile delayed outcomes. Failed attempts are recorded and may be retried with a new idempotency key and transaction reference. Provider-specific authentication, payload parsing, signature verification, callback URL, retry rules, and status mapping remain the adapter's responsibility.

Endpoints: `POST /api/subscription/registrations` creates an idempotent registration; `GET /api/subscription/registrations/{id}` reads its status; `POST .../{id}/claim` associates a guest registration with the signed-in account; `POST .../{id}/payments` initiates through an enabled adapter; `POST .../{id}/verify` reconciles with the provider; `POST .../{id}/cancel` cancels only before payment processing; and `POST /api/payment-webhooks/{provider_id}` delegates signature checks to the configured adapter. Provider configuration and transaction administration are deliberately not exposed to ordinary users. The provider-agnostic registry is empty by default; the existing optional PawaPay checkout remains behind its separate configuration gate. Tests inject mock adapters only.

## Password reset and auth providers

For production, configure Supabase Auth with `SUPABASE_URL`, `SUPABASE_ANON_KEY`, and `PUBLIC_APP_URL`. Enable email confirmations and configure the Supabase email/SMTP provider and redirect allowlist. Signup then remains pending until Supabase confirms the email; the client supports resending the verification email and returning to registration to change the address. Account records are linked only after Supabase returns a verified email, and an existing Kite identity cannot be silently replaced by a different provider identity.

Local password authentication is disabled by default. To use it for local development, set `KITE_ALLOW_LOCAL_AUTH=true`; it is automatically disabled on Vercel and Render. Without Supabase Auth or this local-development opt-in, registration, login, and password recovery return a configuration error, and previously issued local sessions are rejected. The local fallback does not verify email addresses and must not be used as a production identity provider.

Password recovery uses Supabase Auth when Supabase is configured. Local recovery sends email only when `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, and (if required by the SMTP server) `SMTP_USER`/`SMTP_PASSWORD` are configured. Local auth attempts are rate-limited per process and are not shared across multiple workers or restarts, so production deployments must also enable Supabase Auth limits and appropriate edge/proxy rate limiting.

The versioned PostgreSQL security migration enables row-level security and revokes direct table privileges from `PUBLIC` and the Supabase `anon`/`authenticated` roles when present. FastAPI startup verifies the migration revision, schema, and restricted runtime-role privileges; it does not perform schema DDL. The backend accesses tables through its server-side `DATABASE_URL`; never expose that URL or a Supabase service-role key to browser code.

The `kite_runtime` role has `BYPASSRLS` and there are no per-user PostgreSQL RLS policies. RLS is therefore not the tenant-isolation boundary for API requests: FastAPI authorization and user-scoped queries are. Every handler reading or changing user-owned data must scope access to the authenticated user or an explicitly validated capability. Keep runtime database credentials private, and test cross-user access whenever adding or changing an ownership-sensitive endpoint.

Google sign-in uses Supabase Auth with PKCE. In Supabase, enable the Google provider, enter the OAuth client ID and secret from Google Cloud, and allow the Supabase callback and Kite HTTPS origin. Without the required provider configuration, Kite reports an error instead of creating a false session. This environment has no configured Supabase project, Google OAuth credentials, or SMTP delivery, so real Google sign-in, email verification delivery, and provider-issued recovery links cannot be exercised here.

## Android packaging

Install Node.js and the Android Studio SDK/JDK for your target Capacitor version, then run:

```powershell
npm install
npm run android:add
npm run android:sync
npm run android:open
```

Set the Android application ID to the Play Console package name before publishing. Use a release signing key managed outside this repository. Configure API HTTPS, privacy policy, data-safety declarations, target SDK, billing bridge, app icons and store listing in Android Studio/Play Console. The Android wrapper is a packaging shell; it still depends on the deployed Kite API and isolated runner.

## Tests

Install `requirements.txt`, then run:

```powershell
python -m unittest discover -s tests -v
```

The API tests cover email registration/login and logout revocation, one-time password-reset tokens, provider verification and identity linking, rate limiting, profile/email/password/settings changes, persisted progress and XP idempotency, module/lesson catalogs, quiz scoring and best-score retries, challenge scoring, notification actions, authenticated and guest leaderboard access, Python Pro/Top 10/Bug Hunter achievement tracking, premium access, mocked Google purchase verification/expiry, and ownership-checked Playground cancellation. Runner API tests cover authentication, input bounds, cancellation signaling and fail-closed behavior outside Linux. Browser verification covers editor indentation, syntax highlighting, line-number updates, local save/restore, console clearing and contextual navigation. For release, test real execution and process cleanup in the Linux Docker container and the Play Billing lifecycle against Play test products.

### Isolated PostgreSQL integration tests

PostgreSQL integration tests use a separate Compose project and a dedicated `kite_integration_test` database. They do not use the root `compose.yaml`, its application/runner services, the `.env` file, or any Supabase/Render configuration. The test container is pinned to PostgreSQL 17.12, exposes port 55432 on `127.0.0.1` only, and stores data in a separate named volume. The local `tests\postgres-test.env` file contains disposable credentials, is ignored by Git, and must never be reused outside this test database.

From the repository root in Windows PowerShell, create the local environment file from the safe template, replace both password placeholders with different URL-safe random test-only values of at least 24 characters, then start with a clean test-only volume:

```powershell
Copy-Item tests\postgres-test.env.example tests\postgres-test.env
# Edit tests\postgres-test.env with unique test-only values before continuing.
docker compose --env-file tests\postgres-test.env -f compose.postgres-test.yaml down --volumes --remove-orphans
docker compose --env-file tests\postgres-test.env -f compose.postgres-test.yaml up --detach --wait postgres-test
```

Run the PostgreSQL-only integration suite (Python dependencies from `requirements.txt` must already be installed):

```powershell
.\tests\run_postgres_integration.ps1
```

The runner reads only `tests\postgres-test.env`, sets explicit owner/runtime URLs and the required safety marker, removes hosted/Supabase environment variables, and checks that the database is ready. The Python suite independently refuses to run unless both URLs point to `127.0.0.1:55432/kite_integration_test` with the expected test roles and marker. Before each destructive schema reset, it verifies the connected database and owner identity. It exercises fresh/repeated migrations, legacy-schema adoption with data preservation, migration-history access, role/table/sequence privileges, forbidden DDL, FastAPI startup as `kite_runtime`, and cross-user progress, quiz, notification, subscription/payment, and execution access. These tests run against PostgreSQL; SQLite tests are not substituted.

Inspect the latest test results and service state:

```powershell
docker compose --env-file tests\postgres-test.env -f compose.postgres-test.yaml ps
docker compose --env-file tests\postgres-test.env -f compose.postgres-test.yaml logs --no-color postgres-test
```

Stop and remove only this test stack and its disposable database volume:

```powershell
docker compose --env-file tests\postgres-test.env -f compose.postgres-test.yaml down --volumes --remove-orphans
```

The PostgreSQL suite has not been run until Docker Desktop is available and the explicit runner completes successfully. A passing SQLite suite does not count as PostgreSQL verification.

## Important release boundary

This source is a deployable application foundation, not a Play-Console-ready release. The service-account key, Play products, OAuth provider, SMTP account, public API URL, TLS/domain, Supabase project and Capacitor billing plugin are not supplied by the prototype and must be configured by the project owner. The Google Play verification endpoint is implemented; the native purchase bridge and production credentials must be connected and validated before charging users. Docker execution was not testable on the current Windows environment because Docker is unavailable, so the isolation changes require validation on a Linux Docker host before they can be considered production-ready.

The current Playground prototype has an editor and output console, not dedicated editor/instructions/test-case tabs; challenge instructions and test-case evaluation remain in the existing Challenge screen. The repository has no generated Android project or native App Back-button handler, so Android hardware/gesture Back behavior and actual isolated execution/cancellation remain unverified until the Android shell and Linux Docker runner are deployed.

## Frontend-only Vercel deployment

This repository's root Vercel setting points to the FastAPI entrypoint in `pyproject.toml`. To deploy only the static frontend, create a Vercel project with **Root Directory** set to `web` and **Framework Preset** set to `Other`. No install or build command is required; if Vercel asks for an output directory, use `.`. The application uses hash-based routes, so nested-route rewrites are not required. Do not select the repository root for this frontend-only project, or Vercel may build the Python backend instead.

The static frontend has no Vite build and does not read Vercel environment variables. There are no frontend secrets to configure. `web/assets/config.js` currently sets `window.KITE_API_BASE` to an empty string, which uses same-origin API requests. Once the FastAPI service is hosted separately, set that value to its HTTPS origin and configure the backend's exact CORS origins, including the Vercel production and preview origins that should be allowed. Never put server credentials in `config.js` or another browser asset.

Until a backend is deployed and connected, account, curriculum, lesson, quiz, challenge, subscription, saved-progress, leaderboard, and other API-backed operations are unavailable; the frontend reports those unavailable states rather than inventing results. Local theme, guest notification, and other explicitly device-local behavior can still run in the browser. Confirm those boundaries on a Vercel Preview deployment before promoting a production domain.

### Progressive Web App

The static frontend is installable over HTTPS using `web/manifest.webmanifest`. `web/sw.js` caches only the public application shell and allowlisted static assets; all `/api` requests remain network-only. Offline use can reopen the cached shell, but authentication, learning data, subscriptions, and other backend-dependent features still require the API and are never replaced with cached or fabricated responses. Supported browsers expose an **Install Kite** option in the More menu; iPhone and iPad users receive Add to Home Screen guidance. `web/vercel.json` sets the manifest and service-worker response headers required for the existing `web`-root deployment.
