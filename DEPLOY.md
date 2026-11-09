# QBill — deploy checklist (Render, existing account of QGrab)
1. Neon: project "qbill" (free, no expiry) -> pooled DATABASE_URL -> protected file (never in chat/memory).
2. Local: run full test suite against the real Neon DATABASE_URL (Postgres path proof).
3. GitHub: new repo "qbill" under electronicsalghaffar78-stack; push via github tool (WARN Qudoos about the approval card first — standing rule).
4. Render: New Web Service from the repo (Docker, deploy/Dockerfile), name qbill, FREE plan for testing; env: DATABASE_URL (from step 1), SECRET_KEY (auto), OWNER_EMAIL, RESEND_API_KEY (set inside the dashboard flow from the Resend account — value never printed).
5. Verify live: /healthz instant; open site; owner login with a REAL OTP to the electronics inbox (needs the Resend key live + the domain caveat: onboarding@resend.dev can only mail the account's own email until a domain exists — owner testing works; customer OTP needs Brevo/Mailjet route or a domain decision).
6. Keep-warm workflow like QGrab (health ping 10 min) for the free testing period.
7. BEFORE FIRST SALE (binding condition): upgrade this service to the $7/month paid plan (funded by the first fees). No customer activation before the paid plan is active.
8. Then: migrate the real shop data from the Muse QBill copy; Qudoos phone test (install PWA, offline bill, sync).
