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

## Run locally with Docker

1. Install Docker Desktop with Compose support. Linux containers and the `libseccomp` runtime library are required for code execution.
2. Create an untracked `.env` file. Set `KITE_SECRET_KEY` and `RUNNER_SHARED_SECRET` to distinct random values of at least 32 characters; keep both values in the deployment secret manager outside local development.
3. Keep `DATABASE_URL=sqlite:////data/kite.db` for a local SQLite database. Set `RUNNER_SHARED_SECRET` in `.env`; no blank value is accepted by the execution service.
4. From this directory run `docker compose up --build`.
5. Open `http://localhost:8000`. Health checks are available at `/health`.

The API and web application share one origin. For a Supabase production deployment, set `SUPABASE_URL` to the project URL, `SUPABASE_ANON_KEY` to its public anon key, `PUBLIC_APP_URL` to the HTTPS origin serving Kite, and `DATABASE_URL` to the Supabase PostgreSQL connection string (with SSL enabled). In Supabase Auth, enable Google, enter the OAuth client ID and secret from Google Cloud, and allow the Supabase callback URL shown in its Google provider setup in Google Cloud. Set the Supabase Site URL and redirect allowlist to the Kite HTTPS origin (and any intentional local development origin). Keep database and service credentials in the deployment secret manager, not in the ZIP or source tree. Back up the database and use a managed TLS-terminating reverse proxy in front of the API.

### Running the API without Docker

Use Python 3.12+, create and activate a virtual environment, then run:

```powershell
python -m pip install -r requirements.txt
```

Keep the same `.env` and `KITE_SECRET_KEY` across server restarts; `.env` is ignored by Git and changing the key invalidates locally issued sign-in tokens.

In this mode authentication, learning and settings endpoints run, but code execution and Stop intentionally report runner-unavailable errors unless a separately isolated runner is configured. The Playground does not run its prototype JavaScript interpreter as a Python fallback. Do not execute learner code inside the API process.

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

The additive schema is in [`migrations/20261116_subscription_payments.sql`](./migrations/20261116_subscription_payments.sql). It creates plan, registration, transaction, attempt, status-history, and provider-configuration tables with foreign keys, uniqueness constraints, indexes, status checks, and deny-by-default RLS. The application also creates these tables through SQLAlchemy and applies the same no-direct-client-access RLS/revoke policy on PostgreSQL startup. Apply the migration only after checking the target Supabase schema and taking a backup; this repository environment did not have access to the production Supabase database, so its schema was not queried or changed. Keep `DATABASE_URL` server-side. Do not grant the Supabase `anon` or `authenticated` roles direct access to payment or phone tables.

The provider boundary lives in [`backend/payments.py`](./backend/payments.py): adapters implement initiation, transaction verification, and signed-webhook verification and return the shared payment states `NOT_INITIATED`, `PENDING`, `PROCESSING`, `SUCCESSFUL`, `FAILED`, `CANCELLED`, or `EXPIRED`. Register the adapter in the backend provider registry and add an enabled `payment_provider_configurations` row; credentials belong in the host's secret manager/environment, never in that table or browser code. The provider initiation response cannot activate a subscription. Only a verified response/webhook with the exact reference, amount, and currency can create an active subscription; duplicate events are idempotent. Verification can also reconcile delayed outcomes. Failed attempts are recorded and may be retried with a new idempotency key and transaction reference. Provider-specific authentication, payload parsing, signature verification, callback URL, retry rules, and status mapping remain the adapter's responsibility.

Endpoints: `POST /api/subscription/registrations` creates an idempotent registration; `GET /api/subscription/registrations/{id}` reads its status; `POST .../{id}/claim` associates a guest registration with the signed-in account; `POST .../{id}/payments` initiates through an enabled adapter; `POST .../{id}/verify` reconciles with the provider; `POST .../{id}/cancel` cancels only before payment processing; and `POST /api/payment-webhooks/{provider_id}` delegates signature checks to the configured adapter. Provider configuration and transaction administration are deliberately not exposed to ordinary users. The provider-agnostic registry is empty by default; the existing optional PawaPay checkout remains behind its separate configuration gate. Tests inject mock adapters only.

## Password reset and auth providers

For production, configure Supabase Auth with `SUPABASE_URL`, `SUPABASE_ANON_KEY`, and `PUBLIC_APP_URL`. Enable email confirmations and configure the Supabase email/SMTP provider and redirect allowlist. Signup then remains pending until Supabase confirms the email; the client supports resending the verification email and returning to registration to change the address. Account records are linked only after Supabase returns a verified email, and an existing Kite identity cannot be silently replaced by a different provider identity.

Local password authentication is disabled by default. To use it for local development, set `KITE_ALLOW_LOCAL_AUTH=true`; it is automatically disabled on Vercel and Render. Without Supabase Auth or this local-development opt-in, registration, login, and password recovery return a configuration error, and previously issued local sessions are rejected. The local fallback does not verify email addresses and must not be used as a production identity provider.

Password recovery uses Supabase Auth when Supabase is configured. Local recovery sends email only when `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, and (if required by the SMTP server) `SMTP_USER`/`SMTP_PASSWORD` are configured. Local auth attempts are rate-limited per process and are not shared across multiple workers or restarts, so production deployments must also enable Supabase Auth limits and appropriate edge/proxy rate limiting.

On PostgreSQL startup, Kite enables row-level security for its application tables and revokes direct table privileges from `PUBLIC` and the Supabase `anon`/`authenticated` roles when present. The FastAPI backend continues to access the tables through its server-side `DATABASE_URL`; never expose that URL or a Supabase service-role key to browser code.

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

## Important release boundary

This source is a deployable application foundation, not a Play-Console-ready release. The service-account key, Play products, OAuth provider, SMTP account, public API URL, TLS/domain, Supabase project and Capacitor billing plugin are not supplied by the prototype and must be configured by the project owner. The Google Play verification endpoint is implemented; the native purchase bridge and production credentials must be connected and validated before charging users. Docker execution was not testable on the current Windows environment because Docker is unavailable, so the isolation changes require validation on a Linux Docker host before they can be considered production-ready.

The current Playground prototype has an editor and output console, not dedicated editor/instructions/test-case tabs; challenge instructions and test-case evaluation remain in the existing Challenge screen. The repository has no generated Android project or native App Back-button handler, so Android hardware/gesture Back behavior and actual isolated execution/cancellation remain unverified until the Android shell and Linux Docker runner are deployed.
