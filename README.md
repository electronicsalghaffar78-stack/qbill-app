# QBill Web — standalone website (QGrab pattern)

Qudoos ka hukm (2026-11-09): QBill ki ALAG website, usi mojooda Render account me
jahan QGrab hai. Public link, online+offline app, kabhi freeze na ho,
email OTP ke 2–3 API raste (automatic failover).

## Design locks (Render free rules, verified 2026-11-09)
- 750 free instance hours/month pure workspace me SHARE hote hain (QGrab + QBill).
  QBill sleep-friendly rahegi: 15 min khali = sleep (ghante nahi lagte),
  kholne par ~1 min me wake.
- Data Render ke free Postgres me NAHI — wo 30 din me expire ho jata hai.
  Bahar ka free Postgres (Neon/Supabase) lagega, DATABASE_URL se.
- Render free par SMTP ports band hain — OTP sirf HTTPS API routes se:
  Resend (tayyar) + Brevo + aik aur, automatic failover ke sath.
- Filesystem ephemeral hai — site ke andar koi data store nahi hoga.

## Stack
Python/Flask + gunicorn (anti-freeze settings) + Docker — QGrab wala proven pattern.
Offline-first PWA frontend: local queue + auto-sync (online par ON, offline par OFF).

## Phase 1 foundation (backend) — qbill_app package
- `app.py` stays the thin gunicorn entry (`app:app`); the app lives in
  `qbill_app/` (config, db, security, mailer, auth, admin, crud, heavy).
- Config from env: SECRET_KEY, DATABASE_URL (external Postgres; unset =
  local SQLite fallback for DEV/TEST ONLY, loud startup warning),
  OWNER_EMAIL (default electronicsalghaffar78@gmail.com).
- DB: idempotent schema — site_accounts, otp_codes, sessions, mail_config,
  businesses, parties, items, invoices, invoice_lines, payments, expenses,
  auto_backups. Money is integer paisa everywhere (never float).
- Auth (Muse v13 semantics): email OTP — 6-digit, HMAC-hashed, 10-min
  expiry, one active code per email (resend kills the old), distinct
  wrong/used/expired errors; the code never appears in any response or log.
  Session = one device (30 days, token stored hashed). OTP verify creates
  the session LOCKED; the QBill app password unlocks it (set first time,
  verify on reopen, /lock on close; /logout destroys the session so the
  next sign-in needs OTP + password again).
- Mailer: automatic failover Resend -> Brevo -> Mailjet (HTTPS APIs only;
  keys from env or the server-side mail_config table). No route configured
  = code delivered nowhere (v13 safe behavior). Dev recorder route exists
  ONLY under env QBILL_DEV_MAIL=1 (tests; in-memory, no HTTP endpoint).
- Owner gate: OWNER_EMAIL account is owner; mail-config endpoints are
  owner-only and keys are WRITE-ONLY (never returned, never logged; an
  empty key field on save keeps the previously saved key).
- CRUD smoke surface: parties, items, invoices (server-assigned numbers,
  exact totals, stock decrement), payments list. All behind an unlocked
  session. Heavy-work concurrency gate (max 3) wired to a placeholder
  endpoint for the coming PDF work.
- Tests: `.venv/bin/python tests/run_tests.py` — 33 checks, all passing
  (health, OTP lifecycle, owner gate, write-only key, password gate,
  paisa-exact invoice + payment + stock).

## Status
- 2026-11-09: Phase 1 STEP 2 built + tested (55/55): business profile,
  expenses, standalone payments (settle invoices), dashboard + P&L,
  party ledger, invoice PDF (reportlab, heavy-gated), invoice type
  (sale/purchase, S-/P- numbering), client_ref idempotency on all
  creates, auto-backups (snapshot/restore + daily helper), and the
  single-page PWA frontend (login/lock, dashboard, parties+ledger,
  items, new sale + PDF download, payments, expenses, settings with
  owner email delivery) with offline outbox, always-visible sync pill,
  and a service worker caching only the app shell.
- Backups also reach the login email (owner requirement, 2026-11-09):
  every snapshot (manual run, daily helper, offline device upload) is
  emailed to the account's login email as a JSON attachment via the
  mailer failover; an email failure never fails a backup (the row's
  `emailed` flag records the outcome). Offline, the app builds the
  day's snapshot on the device by itself and uploads it via
  POST /api/backups/upload (client_ref dedupe) when internet returns.
- 2026-11-09: Phase 1 FOUNDATION (backend) built + tested (33 checks).
- 2026-11-09: project scaffold (health endpoint + anti-freeze server config).
- Baqi: deploy (GitHub/Render + external Postgres; suite re-run against
  it), offline-queue cross-references (an invoice queued offline can
  only use parties/items already on the server), Phase 2 features
  (Estimates/Orders/Challans/Returns, Day Book, 12 bill themes, Online
  Store, Team & Roles, Barcode).

Blueprint: ~/workspace/goals/qbill-business-billing-app/hidden_files/website-blueprint.md
