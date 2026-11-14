# Learn Python with Kite

Kite is built around the supplied HTML prototype. `web/index.html` is the prototype with a small API bridge appended; `prototype-reference.html` preserves the visual reference with the same neutralized metric and notification placeholders as the live prototype. The screen design, mascot, animations, routes and base interactions come from that prototype. The subscription section is added to the existing Settings screen.

## What is included

- The original Kite screens and navigation, served from the prototype.
- A FastAPI backend with Supabase Auth integration (plus a local-only password-auth fallback), password reset, profiles, preferences, lessons, progress, XP, streaks, quiz/challenge endpoints, leaderboard and notifications.
- A 12-module Python learning path. The six existing lesson entries retain their IDs and content; six new advanced, Premium-gated modules add 48 lessons, 12 quizzes, and 60 server-evaluated coding challenges. The 25 topic quizzes are organized by module, use the same entitlement checks as lessons, connect to the existing coding challenges, and provide explanations and revision links. Signed-in attempts and best scores are stored per account; guest attempt history is stored on the current device.
- SQLite persistence for local development; SQLAlchemy can use Supabase PostgreSQL through `DATABASE_URL`.
- Premium lesson entitlement checks and Google Play purchase verification through the Android Publisher API. Purchase tokens are encrypted at rest.
- An independent Python runner service, reachable only on a private internal Docker network. The trusted service runs with narrowly scoped container capabilities; every learner program runs under a separate unprivileged UID with syscall, CPU, memory, process, filesystem and output restrictions. The runner has no network route.
- The existing Playground editor keeps syntax highlighting and multiline editing, updates line numbers, supports indentation and Run/Save keyboard shortcuts, and provides Run, Stop, Reset, Copy, Save, Clear Console and context-aware Exit controls. Unsaved code/output is retained for the browser session; explicitly saved code is stored on the current device and is not synchronized to the account.
- A Capacitor Android shell configuration.

The client does not use browser storage as the account/progress database for signed-in learners. The short-lived bearer token is held in `sessionStorage`; the server remains the source of truth for account, subscription and authenticated learning data. Guest lesson activity and quiz attempts are device-local and are not synchronized to an account automatically. The prototype itself continues to use browser storage for its original cosmetic theme and demo-only values; server-backed settings are also synchronized through the API.

## Run locally with Docker

1. Install Docker Desktop with Compose support. Linux containers and the `libseccomp` runtime library are required for code execution.
2. Copy `.env.example` to `.env`. Set `KITE_SECRET_KEY` and `RUNNER_SHARED_SECRET` to distinct random values of at least 32 characters.
3. Keep `DATABASE_URL=sqlite:////data/kite.db` for a local SQLite database. Set `RUNNER_SHARED_SECRET` in `.env`; no blank value is accepted by the execution service.
4. From this directory run `docker compose up --build`.
5. Open `http://localhost:8000`. Health checks are available at `/health`.

The API and web application share one origin. For a Supabase production deployment, set `SUPABASE_URL` to the project URL, `SUPABASE_ANON_KEY` to its public anon key, `PUBLIC_APP_URL` to the HTTPS origin serving Kite, and `DATABASE_URL` to the Supabase PostgreSQL connection string (with SSL enabled). In Supabase Auth, enable Google, enter the OAuth client ID and secret from Google Cloud, and allow the Supabase callback URL shown in its Google provider setup in Google Cloud. Set the Supabase Site URL and redirect allowlist to the Kite HTTPS origin (and any intentional local development origin). Keep database and service credentials in the deployment secret manager, not in the ZIP or source tree. Back up the database and use a managed TLS-terminating reverse proxy in front of the API.

### Running the API without Docker

Use Python 3.12+, create and activate a virtual environment, then run:

```powershell
python -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Edit .env: replace KITE_SECRET_KEY with a unique secret and set DATABASE_URL=sqlite:///./kite.db
$secretLine = Get-Content .env | Where-Object { $_ -match '^KITE_SECRET_KEY=' } | Select-Object -First 1
$databaseLine = Get-Content .env | Where-Object { $_ -match '^DATABASE_URL=' } | Select-Object -First 1
if (-not $secretLine -or -not $databaseLine) { throw "Set KITE_SECRET_KEY and DATABASE_URL in .env." }
$env:KITE_SECRET_KEY = $secretLine -replace '^KITE_SECRET_KEY=', ''
$env:DATABASE_URL = $databaseLine -replace '^DATABASE_URL=', ''
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

Keep the same `.env` and `KITE_SECRET_KEY` across server restarts; `.env` is ignored by Git and changing the key invalidates locally issued sign-in tokens.

In this mode authentication, learning and settings endpoints run, but code execution and Stop intentionally report runner-unavailable errors unless a separately isolated runner is configured. Do not execute learner code inside the API process.

## Google Play subscriptions

1. Create monthly and annual auto-renewing subscription products in Play Console. By default, use product IDs `kite_premium_monthly` and `kite_premium_annual`, or set `KITE_PLAY_MONTHLY_PRODUCT_ID` and `KITE_PLAY_ANNUAL_PRODUCT_ID` to the exact IDs you create.
   Set the Zambia storefront prices to K19.99 per month and K99.99 per year. The app displays those amounts, with the annual plan highlighted as Best Value and saving K139.89 annually (58.3% versus twelve monthly payments).
2. Set `GOOGLE_PLAY_PACKAGE_NAME` to the Android application ID/package name; enable the Google Play Android Developer API in the service account's Google Cloud project; link that project in Play Console; grant the service account access to the app with subscription/order read permissions; mount its JSON key as a deployment secret and set `GOOGLE_APPLICATION_CREDENTIALS` to its container path. Do not commit the key.
3. Configure the Android app to use the deployed API origin. In a packaged Capacitor build set `window.KITE_API_BASE` to that HTTPS origin before `web/assets/app.js` loads.
4. The Capacitor Google Play Billing bridge is not currently included. Implement or select a Capacitor 7-compatible bridge that exposes `window.Capacitor.Plugins.KiteBilling.purchase({productId})` and returns the verified Play purchase token as `purchaseToken`; it must also expose restoration and acknowledge/complete-purchase behavior. The backend verifies every token with Google; the client cannot grant its own entitlement.
5. Before release, test purchases, pending transactions, restore purchases, grace periods, account hold, expiration, refunds, and cancellation in Play license testing. The Settings screen links to Google Play subscription management and refreshes the authoritative state from the Play API.

Google Play credentials, Play Console products, signing keys, OAuth settings, and production Supabase credentials are deployment-specific and are not included. The `KITE_MONTHLY_PRICE` and `KITE_ANNUAL_PRICE` values control displayed plan text only; Google Play Console controls actual checkout amounts. Keep the storefront product prices and these display values in sync, and do not enable billing until the product prices and native billing bridge are configured and verified. Google Billing purchases and restoration have not been tested because this repository has no native billing bridge or Android project.

## Password reset and auth providers

For production, configure Supabase Auth with `SUPABASE_URL`, `SUPABASE_ANON_KEY`, and `PUBLIC_APP_URL`. Enable email confirmations and configure the Supabase email/SMTP provider and redirect allowlist. Signup then remains pending until Supabase confirms the email; the client supports resending the verification email and returning to registration to change the address. Account records are linked only after Supabase returns a verified email, and an existing Kite identity cannot be silently replaced by a different provider identity.

Password recovery uses Supabase Auth when Supabase is configured. The local-only fallback sends recovery email only when `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, and (if required by the SMTP server) `SMTP_USER`/`SMTP_PASSWORD` are configured. The local fallback is for development and does not provide Google sign-in or email verification; do not use it as a production identity provider. Local auth attempts are rate-limited per process and are not shared across multiple workers or restarts, so production deployments must also enable Supabase Auth limits and appropriate edge/proxy rate limiting.

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

The API tests cover email registration/login and logout revocation, one-time password-reset tokens, provider verification and identity linking, rate limiting, profile/email/password/settings changes, persisted progress and XP idempotency, module/lesson catalogs, quiz scoring and best-score retries, challenge scoring, notification actions, leaderboard periods, Python Pro/Top 10/Bug Hunter achievement tracking, premium access, mocked Google purchase verification/expiry, and ownership-checked Playground cancellation. Runner API tests cover authentication, input bounds, cancellation signaling and fail-closed behavior outside Linux. Browser verification covers editor indentation, syntax highlighting, line-number updates, local save/restore, console clearing and contextual navigation. For release, test real execution and process cleanup in the Linux Docker container and the Play Billing lifecycle against Play test products.

## Important release boundary

This source is a deployable application foundation, not a Play-Console-ready release. The service-account key, Play products, OAuth provider, SMTP account, public API URL, TLS/domain, Supabase project and Capacitor billing plugin are not supplied by the prototype and must be configured by the project owner. The Google Play verification endpoint is implemented; the native purchase bridge and production credentials must be connected and validated before charging users. Docker execution was not testable on the current Windows environment because Docker is unavailable, so the isolation changes require validation on a Linux Docker host before they can be considered production-ready.

The current Playground prototype has an editor and output console, not dedicated editor/instructions/test-case tabs; challenge instructions and test-case evaluation remain in the existing Challenge screen. The repository has no generated Android project or native App Back-button handler, so Android hardware/gesture Back behavior and actual isolated execution/cancellation remain unverified until the Android shell and Linux Docker runner are deployed.
