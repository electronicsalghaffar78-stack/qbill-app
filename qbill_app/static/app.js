/* QBill app — single-page PWA.
   Offline design: creates made while offline (or when a request fails)
   go into a localStorage outbox with a unique client_ref each; the server
   dedupes by client_ref, so replaying can never create duplicates. Reads
   fall back to the last successful response (kept in localStorage).
   Daily backup: once per calendar day the app asks the server for a
   snapshot; offline it builds the snapshot LOCALLY on the device by
   itself and uploads it when the internet returns (server then emails
   it, like every snapshot). */
"use strict";

/* ------------------------------------------------------------ storage -- */
function lsGet(key, fallback) {
  try {
    var raw = localStorage.getItem(key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch (e) { return fallback; }
}
function lsSet(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) {}
}
function lsDel(key) { try { localStorage.removeItem(key); } catch (e) {} }
function ssGet(key, fallback) {
  try {
    var raw = sessionStorage.getItem(key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch (e) { return fallback; }
}
function ssSet(key, value) {
  try { sessionStorage.setItem(key, JSON.stringify(value)); } catch (e) {}
}
function ssDel(key) { try { sessionStorage.removeItem(key); } catch (e) {} }

/* The session token lives in localStorage for "Remember me" logins and
   OTP sign-ins (it survives a browser restart), or in sessionStorage
   for a login WITHOUT "Remember me" (gone when the browser closes).
   Lookup checks localStorage first, then sessionStorage. The
   qbill_token_temp flag marks a sessionStorage token so the pagehide
   handler can lock it: closing the page then asks the password again
   when this tab reopens. */
var TOKEN_KEY = "qbill_token";
var token = function () {
  var t = lsGet(TOKEN_KEY, null);
  return t !== null ? t : ssGet(TOKEN_KEY, null);
};
function setToken(t, remember) {
  if (!t) {
    lsDel(TOKEN_KEY); ssDel(TOKEN_KEY); lsDel("qbill_token_temp");
    return;
  }
  if (remember === false) {
    ssSet(TOKEN_KEY, t); lsDel(TOKEN_KEY); lsSet("qbill_token_temp", true);
  } else {
    lsSet(TOKEN_KEY, t); ssDel(TOKEN_KEY); lsDel("qbill_token_temp");
  }
}
function outbox() { return lsGet("qbill_outbox", []); }
function setOutbox(q) { lsSet("qbill_outbox", q); updateSyncPill(); }
function localBackups() { return lsGet("qbill_local_backups", []); }
function setLocalBackups(b) { lsSet("qbill_local_backups", b); updateSyncPill(); }
function cacheGet(key) { return lsGet("qbill_cache_" + key, null); }
function cacheSet(key, value) { lsSet("qbill_cache_" + key, value); }

function newClientRef() {
  return "cr-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
}

/* -------------------------------------------------------------- money -- */
var QB = { settings: null, privacy: false };
function decimalPlaces() {
  var d = QB.settings && QB.settings.decimal_places;
  return (d === 0 || d === 1 || d === 2) ? d : 2;
}
function fmtRs(paisa) {
  var neg = paisa < 0; paisa = Math.abs(paisa || 0);
  var dp = decimalPlaces();
  var rupees = Math.floor(paisa / 100), ps = paisa % 100;
  var s = rupees.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  if (dp === 0) return (neg ? "-" : "") + "Rs " + s;
  var frac = (ps < 10 ? "0" : "") + ps;
  if (dp === 1) frac = frac.slice(0, 1);
  return (neg ? "-" : "") + "Rs " + s + "." + frac;
}
function rsToPaisa(value) {
  var n = parseFloat(value);
  if (isNaN(n)) return 0;
  return Math.round(n * 100);
}

/* ---------------------------------------------------------------- dom -- */
function $(id) { return document.getElementById(id); }
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}
function setMsg(id, text, isErr) {
  var el = $(id); if (!el) return;
  el.textContent = text || "";
  el.className = "msg" + (isErr ? " err" : "");
}

/* ---------------------------------------------------------------- api -- */
function authHeaders() {
  var h = { "Content-Type": "application/json" };
  var t = token();
  if (t) h["Authorization"] = "Bearer " + t;
  return h;
}

function handleAuthFailure(status, body) {
  // Only a dead/expired session ("Sign-in required.") ends the local
  // session. A 401 that means "wrong password" (verify-password,
  // change-password) must surface its message and keep the session.
  if (status === 401 && body && body.error === "Sign-in required.") { setToken(null); showAuth(); return true; }
  if (status === 403 && body && body.code === "locked") { showLock(); return true; }
  // The owner has not approved this account (or blocked it mid-use):
  // re-read /me and land on the waiting screen.
  if (status === 403 && body && body.code === "pending") { refreshMe(); return true; }
  if (status === 403 && body && body.code === "forbidden_role") { toast("Your role does not allow this action."); return false; }
  return false;
}

/* GET with offline fallback to the last successful response. */
function apiGet(path, cacheKey) {
  return fetch(path, { headers: authHeaders() }).then(function (resp) {
    return resp.json().catch(function () { return {}; }).then(function (body) {
      if (resp.ok) {
        if (cacheKey) cacheSet(cacheKey, body);
        return { body: body, offline: false };
      }
      if (handleAuthFailure(resp.status, body)) throw new Error("auth");
      throw new Error(body.error || ("Request failed (" + resp.status + ")"));
    });
  }).catch(function (err) {
    if (err.message === "auth") throw err;
    var cached = cacheKey ? cacheGet(cacheKey) : null;
    if (cached) return { body: cached, offline: true };
    throw err;
  });
}

/* POST for creates. Offline (or network failure) -> outbox queue. */
var CREATE_PATHS = {
  party: "/api/parties", item: "/api/items", invoice: "/api/invoices",
  payment: "/api/payments", expense: "/api/expenses"
};
function apiCreate(kind, payload) {
  if (!payload.client_ref) payload.client_ref = newClientRef();
  if (!navigator.onLine) {
    queueCreate(kind, payload);
    return Promise.resolve({ queued: true });
  }
  return fetch(CREATE_PATHS[kind], {
    method: "POST", headers: authHeaders(), body: JSON.stringify(payload)
  }).then(function (resp) {
    return resp.json().catch(function () { return {}; }).then(function (body) {
      if (resp.ok) return { queued: false, body: body, status: resp.status };
      if (handleAuthFailure(resp.status, body)) throw new Error("auth");
      var err = new Error(body.error || ("Could not save (" + resp.status + ")"));
      err.httpStatus = resp.status; // server answered: a real rejection
      throw err;
    });
  }).catch(function (err) {
    if (err.message === "auth" || err.httpStatus) throw err;
    queueCreate(kind, payload); // no answer at all -> save on this device
    return { queued: true };
  });
}
function queueCreate(kind, payload) {
  var q = outbox();
  q.push({ kind: kind, payload: payload });
  setOutbox(q);
}

function apiPost(path, payload) {
  return fetch(path, {
    method: "POST", headers: authHeaders(), body: JSON.stringify(payload || {})
  }).then(function (resp) {
    return resp.json().catch(function () { return {}; }).then(function (body) {
      if (resp.ok) return body;
      if (handleAuthFailure(resp.status, body)) throw new Error("auth");
      throw new Error(body.error || ("Request failed (" + resp.status + ")"));
    });
  });
}
function apiPut(path, payload) {
  return fetch(path, {
    method: "PUT", headers: authHeaders(), body: JSON.stringify(payload || {})
  }).then(function (resp) {
    return resp.json().catch(function () { return {}; }).then(function (body) {
      if (resp.ok) return body;
      if (handleAuthFailure(resp.status, body)) throw new Error("auth");
      throw new Error(body.error || ("Request failed (" + resp.status + ")"));
    });
  });
}
function apiDelete(path) {
  return fetch(path, { method: "DELETE", headers: authHeaders() }).then(function (resp) {
    return resp.json().catch(function () { return {}; }).then(function (body) {
      if (resp.ok) return body;
      if (handleAuthFailure(resp.status, body)) throw new Error("auth");
      throw new Error(body.error || ("Request failed (" + resp.status + ")"));
    });
  });
}
function downloadAuthed(path, filename) {
  return fetch(path, { headers: authHeaders() })
    .then(function (resp) { if (!resp.ok) throw new Error("Download failed (" + resp.status + ")"); return resp.blob(); })
    .then(function (blob) {
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      a.click();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 4000);
    });
}

/* ------------------------------------------------------------ syncing -- */
var syncing = false;
function updateSyncPill() {
  var pending = outbox().length + localBackups().length;
  $("offlineNote").classList.toggle("hidden", navigator.onLine);
  var text, cls;
  if (!navigator.onLine) {
    text = "Offline · Auto-sync OFF — kaam chalta rahega";
    cls = "pill pill-off";
  } else if (pending > 0) {
    text = "Online · Auto-sync ON · " + pending + " pending";
    cls = "pill pill-pending";
  } else {
    text = "Online · Auto-sync ON";
    cls = "pill pill-on";
  }
  ["syncPill", "syncPillSide"].forEach(function (id) {
    var pill = $(id);
    if (!pill) return;
    pill.textContent = text;
    pill.className = cls;
  });
}

/* Replay the outbox, then upload any local (offline-made) backups. */
function syncNow() {
  if (syncing || !navigator.onLine || !token()) return;
  syncing = true;
  updateSyncPill();
  var q = outbox();
  var chain = Promise.resolve();
  q.forEach(function (entry) {
    chain = chain.then(function () {
      return fetch(CREATE_PATHS[entry.kind], {
        method: "POST", headers: authHeaders(), body: JSON.stringify(entry.payload)
      }).then(function (resp) {
        if (resp.ok || (resp.status >= 400 && resp.status < 500)) {
          // Saved (or permanently rejected): drop from the queue either
          // way so one bad entry can never block the rest forever.
          var rest = outbox().filter(function (e) {
            return e.payload.client_ref !== entry.payload.client_ref;
          });
          setOutbox(rest);
        }
        if (resp.status === 401) { setToken(null); showAuth(); }
      }).catch(function () { /* still offline — keep queued */ });
    });
  });
  chain.then(function () { return uploadLocalBackups(); })
    .then(function () {
      syncing = false;
      updateSyncPill();
      if (currentScreen) loadScreen(currentScreen, true);
    });
}

function uploadLocalBackups() {
  var pending = localBackups();
  if (!pending.length) return Promise.resolve();
  var chain = Promise.resolve();
  pending.forEach(function (snap) {
    chain = chain.then(function () {
      return apiPost("/api/backups/upload", {
        payload: snap.payload, created_at: snap.created_at, client_ref: snap.client_ref
      }).then(function () {
        setLocalBackups(localBackups().filter(function (b) {
          return b.client_ref !== snap.client_ref;
        }));
      }).catch(function () { /* keep for the next connection */ });
    });
  });
  return chain;
}

/* ------------------------------------------- daily backup (auto) ----- */
/* Runs by itself: online -> the server snapshots + emails it; offline ->
   the snapshot is built on THIS device and queued for upload. Either way
   the shopkeeper does nothing. Once per calendar day. */
function maybeDailyBackup() {
  if (!token()) return;
  if (QB.settings && QB.settings.auto_backup === false) return;
  var today = new Date().toISOString().slice(0, 10);
  if (lsGet("qbill_backup_day", "") === today) return;
  if (navigator.onLine) {
    apiPost("/api/backups/run", {}).then(function () {
      lsSet("qbill_backup_day", today);
      updateSyncPill();
    }).catch(function () { buildLocalBackup(today); });
  } else {
    buildLocalBackup(today);
  }
}
function buildLocalBackup(today) {
  var payload = {
    version: 1,
    created_at: new Date().toISOString(),
    source: "device",
    business: (cacheGet("business") || {}).business || {},
    parties: (cacheGet("parties") || {}).parties || [],
    items: (cacheGet("items") || {}).items || [],
    invoices: (cacheGet("invoices") || {}).invoices || [],
    payments: (cacheGet("payments") || {}).payments || [],
    expenses: (cacheGet("expenses") || {}).expenses || [],
    totals: clientTotals()
  };
  var snaps = localBackups();
  snaps.push({ client_ref: newClientRef(), created_at: payload.created_at, payload: payload });
  setLocalBackups(snaps);
  lsSet("qbill_backup_day", today);
}
function clientTotals() {
  var parties = (cacheGet("parties") || {}).parties || [];
  var items = (cacheGet("items") || {}).items || [];
  var invoices = (cacheGet("invoices") || {}).invoices || [];
  var payments = (cacheGet("payments") || {}).payments || [];
  var expenses = (cacheGet("expenses") || {}).expenses || [];
  var today = new Date().toISOString().slice(0, 10), month = today.slice(0, 7);
  var t = { todaySalesPaisa: 0, monthSalesPaisa: 0, totalReceivablePaisa: 0, cashInHandPaisa: 0, stockValuePaisa: 0 };
  invoices.forEach(function (i) {
    if (i.type !== "sale") return;
    if ((i.createdAt || "").slice(0, 10) === today) t.todaySalesPaisa += i.totalPaisa;
    if ((i.createdAt || "").slice(0, 7) === month) t.monthSalesPaisa += i.totalPaisa;
  });
  parties.forEach(function (p) { if (p.balancePaisa > 0) t.totalReceivablePaisa += p.balancePaisa; });
  payments.forEach(function (p) { t.cashInHandPaisa += p.direction === "in" ? p.amountPaisa : -p.amountPaisa; });
  expenses.forEach(function (e) { t.cashInHandPaisa -= e.amountPaisa; });
  items.forEach(function (i) { t.stockValuePaisa += i.stockQty * i.purchasePricePaisa; });
  return t;
}

/* --------------------------------------------------------------- auth -- */
/* All sign-in happens in ONE popup (authModal) over a blurred backdrop.
   The front door is the CLASSIC login (stepLogin): email + password +
   "Remember me", no email code needed day to day. Around it:
   - "Create an account" (stepOne): ONE single form stacked top to
     bottom — email, the emailed code, a created password, "Log in";
     completing it opens the app directly.
   - "Forgot your password?" (stepForgot): email -> code -> stepReset,
     a NEW password for the SAME account (needs an OTP-fresh session).
   Owner's rule: an emailed code appears ONLY in the create-account
   form and the forgot route — never as a third login route (the old
   stepEmail panel stays in the markup, unlinked).
   The app behind the popup is only shown once the session is unlocked. */
var me = null;
/* Set when the user came through a "Forgot your password?" route: after
   the fresh email code verifies, route to the create-a-new-password
   step instead of the normal welcome-back step. */
var forgotIntent = false;
/* Which flow the code step belongs to ("signup" | "forgot" |
   "codesignin"), the email the code was sent to, and the panel the
   code step's back link returns to. */
var codeContext = "codesignin";
var otpEmail = "";
var codeFromStep = "stepLogin";
var AUTH_STEPS = ["stepOne", "stepLogin", "stepEmail", "stepSignup", "stepForgot",
  "stepCode", "stepCreate", "stepWelcome", "stepReset"];
var ADD_BTNS = ["btnAddSale", "btnAddPurchase", "btnAddMore"];
function hideAddButtons() {
  ADD_BTNS.forEach(function (id) { $(id).classList.add("hidden"); });
  $("addMoreMenu").classList.add("hidden");
}
function showAuthStep(step) {
  $("appView").classList.add("hidden");
  $("pendingView").classList.add("hidden");
  $("btnLock").classList.add("hidden");
  hideAddButtons();
  // A session exists on the password steps, so Log out stays available.
  $("btnLogout").classList.toggle("hidden",
    step !== "stepCreate" && step !== "stepWelcome" && step !== "stepReset");
  if (step === "stepWelcome") {
    // Just signed in with an email code (OTP-fresh session): the user
    // may replace a forgotten password right away. A plain locked
    // session instead offers the sign-in-again "Forgot" route.
    var fresh = !!(me && me.otpFresh);
    $("btnWelcomeReset").classList.toggle("hidden", !fresh);
    $("btnForgotPw").classList.toggle("hidden", fresh);
  }
  $("authModal").classList.remove("hidden");
  AUTH_STEPS.forEach(function (s) { $(s).classList.toggle("hidden", s !== step); });
  setMsg("authMsg", "");
}
function showAuth() {
  // No session on this device: the classic login is the front door.
  me = null;
  showAuthStep("stepLogin");
}
function showLock() {
  // Session exists but is locked: ask for the password, never a dead end.
  showAuthStep("stepWelcome");
}
function showPending() {
  // Signed in, but the owner has not approved this account yet: show
  // the activation (plans) screen — neither the app nor the popup.
  // A duplicate-IP signup first sees WHY it was auto-rejected; a
  // normal new account sees the plain activation text. Both then
  // see the same plans and owner contact block.
  var rejected = !!(me && me.rejectReason === "ip_match");
  $("pendingIpNote").classList.toggle("hidden", !rejected);
  $("pendingText").classList.toggle("hidden", rejected);
  $("authModal").classList.add("hidden");
  $("appView").classList.add("hidden");
  $("btnLock").classList.add("hidden");
  hideAddButtons();
  $("btnLogout").classList.remove("hidden");
  $("pendingView").classList.remove("hidden");
}
function showApp() {
  $("authModal").classList.add("hidden");
  $("pendingView").classList.add("hidden");
  $("appView").classList.remove("hidden");
  $("btnLock").classList.remove("hidden");
  $("btnLogout").classList.remove("hidden");
  ADD_BTNS.forEach(function (id) { $(id).classList.remove("hidden"); });
  if (me && me.email) $("sideBizEmail").textContent = me.email;
  var biz = (cacheGet("business") || {}).business || {};
  if (biz.name) applyBusinessIdentity(biz);
  $("setCatEmail").classList.toggle("hidden", !(me && me.isOwner));
  $("setCatAccounts").classList.toggle("hidden", !(me && me.isOwner));
  setSettingsCat(settingsCat);
  loadSettingsDoc();
  showScreen(currentScreen || "dashboard");
  maybeDailyBackup();
  syncNow();
}

function refreshMe() {
  return fetch("/api/auth/me", { headers: authHeaders() }).then(function (resp) {
    if (resp.status === 401) { setToken(null); showAuth(); return null; }
    return resp.json();
  }).then(function (body) {
    if (!body) return;
    me = body;
    if (!body.isOwner && body.approved === false) {
      // Waiting for the owner's approval — regardless of locked state
      // or whether a password exists yet.
      forgotIntent = false;
      showPending();
    } else if (!body.hasAppPassword) {
      forgotIntent = false; // a first password is created, never "reset"
      showAuthStep("stepCreate"); // first time: create a password, NOT a lock screen
    } else if (!body.unlocked) {
      if (forgotIntent) {
        showAuthStep("stepReset"); // forgot it: the fresh code proved the email
      } else {
        showAuthStep("stepWelcome");
      }
    } else {
      forgotIntent = false;
      showApp();
    }
  }).catch(function () {
    // Offline at startup: if we hold a token, open the app on cached data.
    if (token()) { me = me || { isOwner: false }; showApp(); }
    else showAuth();
  });
}

/* Show/Hide toggles on the password fields. */
document.querySelectorAll(".pw-toggle").forEach(function (btn) {
  btn.addEventListener("click", function (ev) {
    ev.preventDefault();
    var input = $(btn.getAttribute("data-toggle"));
    if (!input) return;
    var show = input.type === "password";
    input.type = show ? "text" : "password";
    btn.textContent = show ? "Hide" : "Show";
    input.focus();
  });
});

/* Ask the server for an email code and move to the code step, keeping
   track of which flow asked (signup / forgot / codesignin) so the code
   step's wording and the routing after the code fit the flow. */
function requestCode(email, context, fromStep) {
  codeContext = context;
  otpEmail = email;
  codeFromStep = fromStep;
  setMsg("authMsg", "Sending code…");
  return apiPost("/api/auth/request-otp", { email: email }).then(function () {
    updateCodeText();
    showAuthStep("stepCode");
    setMsg("authMsg", "Code sent. Check your email inbox.");
  }).catch(function (e) { setMsg("authMsg", e.message, true); });
}
function updateCodeText() {
  $("codeSentTo").textContent = otpEmail;
  if (codeContext === "signup") {
    $("codeHeading").textContent = "Verify your email";
    $("codeSubLead").textContent = "Enter the code we sent to";
    $("codeSubTail").textContent = " to verify it is yours.";
  } else if (codeContext === "forgot") {
    $("codeHeading").textContent = "Enter your login code";
    $("codeSubLead").textContent = "We sent a 6-digit code to";
    $("codeSubTail").textContent = ". After the code, you can create a new password.";
  } else {
    $("codeHeading").textContent = "Enter your login code";
    $("codeSubLead").textContent = "We sent a 6-digit code to";
    $("codeSubTail").textContent = ". Please enter it below.";
  }
}
function sendCode() {
  // stepEmail's Continue: sign in with an emailed code instead.
  requestCode($("loginEmail").value.trim(), "codesignin", "stepEmail");
}
$("btnSendCode").addEventListener("click", sendCode);
$("btnResendCode").addEventListener("click", function () {
  var email = otpEmail || $("loginEmail").value.trim();
  setMsg("authMsg", "Sending a new code…");
  apiPost("/api/auth/request-otp", { email: email }).then(function () {
    setMsg("authMsg", "A new code was sent. Check your email inbox.");
  }).catch(function (e) { setMsg("authMsg", e.message, true); });
});
$("btnBackToEmail").addEventListener("click", function () {
  // Back to whichever panel asked for the code (signup/forgot/email).
  showAuthStep(codeFromStep);
});
/* After a code verifies, route by the flow that asked for it. */
function routeAfterVerify(body) {
  if (codeContext === "signup" && body.hasAppPassword) {
    // This email already has an account: the code sign-in is dropped
    // and the user logs in with the password, like everyone else.
    forgotIntent = false;
    apiPost("/api/auth/logout", {}).catch(function () {}).then(function () {
      setToken(null);
      me = null;
      $("liEmail").value = otpEmail;
      showAuthStep("stepLogin");
      setMsg("authMsg", "This email already has an account. Please log in.");
    });
    return;
  }
  // forgot: refreshMe lands on the reset step (or the create step when
  // the account never had a password). signup/codesignin: refreshMe
  // routes create (no password) / welcome (locked) / app (unlocked).
  forgotIntent = codeContext === "forgot";
  refreshMe();
}
$("btnVerifyCode").addEventListener("click", function () {
  var email = otpEmail || $("loginEmail").value.trim();
  var code = $("loginCode").value.trim();
  setMsg("authMsg", "Checking your code…");
  apiPost("/api/auth/verify-otp", { email: email, code: code }).then(function (body) {
    setToken(body.token); // code sign-ins stay remembered on this device
    routeAfterVerify(body);
  }).catch(function (e) { setMsg("authMsg", e.message, true); });
});

/* ------------------------------------------------------- ONE form ---- */
/* The create-an-account form (owner's design): ONE form stacked top
   to bottom — email, the emailed code, a created password, "Log in".
   Completing it chains verify-otp -> set-password and opens the app.
   An email that already HAS a password never gets a new one here:
   the just-created code session is logged out and the user is sent
   to the classic login to use the password they already have. */
$("btnOneSend").addEventListener("click", function () {
  var email = $("oneEmail").value.trim();
  if (!email) { setMsg("authMsg", "Please enter your email first.", true); return; }
  setMsg("authMsg", "Sending code…");
  apiPost("/api/auth/request-otp", { email: email }).then(function () {
    $("btnOneSend").textContent = "Resend code";
    setMsg("authMsg", "Code sent — check your email. The code is valid for 10 minutes.");
  }).catch(function (e) { setMsg("authMsg", e.message, true); });
});
$("btnOneLogin").addEventListener("click", function () {
  var email = $("oneEmail").value.trim();
  var code = $("oneCode").value.trim();
  var pw = $("onePassword").value;
  var at = email.indexOf("@");
  // Check on this device FIRST, with clear messages — a bad entry
  // must never silently fail against the server.
  if (!email || at < 1 || email.lastIndexOf(".") < at + 2) {
    setMsg("authMsg", "Please enter a valid email address.", true); return;
  }
  if (!/^\d{6}$/.test(code)) {
    setMsg("authMsg", "Please enter the 6-digit code sent to your email.", true); return;
  }
  if (pw.length < 4) {
    setMsg("authMsg", "Password must be at least 4 characters.", true); return;
  }
  setMsg("authMsg", "Opening QBill…");
  // A dedicated fetch (not apiPost): a failed verify must show its
  // message here, and its (non-)token must never be stored or treated
  // as a dead session by handleAuthFailure.
  fetch("/api/auth/verify-otp", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: email, code: code })
  }).then(function (resp) {
    return resp.json().catch(function () { return {}; }).then(function (body) {
      if (!resp.ok) throw new Error(body.error || ("Could not check the code (" + resp.status + ")"));
      return body;
    });
  }).then(function (body) {
    if (body.hasAppPassword) {
      // This email already has an account and a password: end the
      // code session just created and route to the classic login.
      // set-password is NEVER called on this path.
      return fetch("/api/auth/logout", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": "Bearer " + body.token },
        body: "{}"
      }).catch(function () {}).then(function () {
        setToken(null);
        me = null;
        $("liEmail").value = email;
        showAuthStep("stepLogin");
        setMsg("authMsg", "This email already has an account and a password. Please log in with your password.");
      });
    }
    setToken(body.token); // code sign-ins stay remembered on this device
    return apiPost("/api/auth/set-password", { password: pw }).then(function () {
      $("onePassword").value = "";
      $("oneCode").value = "";
      forgotIntent = false;
      refreshMe(); // set-password unlocked this session: the app opens
    });
  }).catch(function (e) { setMsg("authMsg", e.message, true); });
});
$("btnOneHavePw").addEventListener("click", function () { showAuthStep("stepLogin"); });
$("btnOneForgot").addEventListener("click", function () { showAuthStep("stepForgot"); });

/* ------------------------------------------------ classic login form -- */
$("loginForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  var remember = $("liRemember").checked;
  setMsg("authMsg", "Logging in…");
  // A dedicated fetch (not apiPost): a 401 here is the normal "wrong
  // email or password" answer whose message the user must see — not a
  // dead session for handleAuthFailure to clean up.
  fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      email: $("liEmail").value.trim(),
      password: $("liPassword").value,
      remember: remember
    })
  }).then(function (resp) {
    return resp.json().catch(function () { return {}; }).then(function (body) {
      if (!resp.ok) throw new Error(body.error || ("Log in failed (" + resp.status + ")"));
      setToken(body.token, remember);
      $("liPassword").value = "";
      forgotIntent = false;
      refreshMe(); // the login session starts unlocked: the app opens
    });
  }).catch(function (e) { setMsg("authMsg", e.message, true); });
});
$("btnLoginForgot").addEventListener("click", function () { showAuthStep("stepForgot"); });
$("btnLoginSignup").addEventListener("click", function () { showAuthStep("stepOne"); });
$("btnEmailBack").addEventListener("click", function () { showAuthStep("stepLogin"); });
$("btnSignupBack").addEventListener("click", function () { showAuthStep("stepLogin"); });
$("btnForgotBack").addEventListener("click", function () { showAuthStep("stepLogin"); });
$("btnSignupContinue").addEventListener("click", function () {
  forgotIntent = false;
  requestCode($("suEmail").value.trim(), "signup", "stepSignup");
});
$("btnForgotContinue").addEventListener("click", function () {
  var email = $("fgEmail").value.trim();
  forgotIntent = true;
  if (token()) {
    // A locked session may still be signed in on this device: end it
    // first — a password reset must start from the email code alone.
    apiPost("/api/auth/logout", {}).catch(function () {}).then(function () {
      setToken(null);
      me = null;
      requestCode(email, "forgot", "stepForgot");
    });
  } else {
    requestCode(email, "forgot", "stepForgot");
  }
});
$("btnSetPassword").addEventListener("click", function () {
  var pw = $("pwNew").value;
  var confirm = $("pwConfirm").value;
  // Check on this device FIRST, with a big clear message — a short or
  // mistyped password must never silently fail against the server.
  if (pw.length < 4) {
    setMsg("authMsg", "Password must be at least 4 characters.", true);
    return;
  }
  if (pw !== confirm) {
    setMsg("authMsg", "The two passwords do not match. Please type the same password in both boxes.", true);
    return;
  }
  setMsg("authMsg", "Saving your password…");
  apiPost("/api/auth/set-password", { password: pw }).then(function () {
    $("pwNew").value = "";
    $("pwConfirm").value = "";
    forgotIntent = false;
    refreshMe(); // the server unlocked this session: the app opens
  }).catch(function (e) { setMsg("authMsg", e.message, true); });
});
$("btnUnlock").addEventListener("click", function () {
  setMsg("authMsg", "Opening QBill…");
  apiPost("/api/auth/verify-password", { password: $("lockPassword").value }).then(function () {
    $("lockPassword").value = "";
    forgotIntent = false; // a normal login finished: intent is spent
    refreshMe();
  }).catch(function (e) { setMsg("authMsg", e.message, true); });
});
$("btnForgotPw").addEventListener("click", function () {
  // The forgot route starts at its own email-first panel; its Continue
  // ends any current session before asking for the code.
  if (me && me.email) $("fgEmail").value = me.email;
  showAuthStep("stepForgot");
});
$("btnWelcomeOther").addEventListener("click", function () {
  // Sign this device out completely and go back to the classic login.
  apiPost("/api/auth/logout", {}).catch(function () {}).then(function () {
    setToken(null);
    forgotIntent = false;
    showAuth();
  });
});
$("btnWelcomeReset").addEventListener("click", function () {
  // This session just proved the email with a fresh code, so a new
  // password may be created right away (the server checks otp_fresh).
  showAuthStep("stepReset");
});
$("btnResetPassword").addEventListener("click", function () {
  var pw = $("pwResetNew").value;
  var confirm = $("pwResetConfirm").value;
  // Same on-device checks as the create step, before any API call.
  if (pw.length < 4) {
    setMsg("authMsg", "Password must be at least 4 characters.", true);
    return;
  }
  if (pw !== confirm) {
    setMsg("authMsg", "The two passwords do not match. Please type the same password in both boxes.", true);
    return;
  }
  setMsg("authMsg", "Saving your new password…");
  apiPost("/api/auth/reset-password", { password: pw }).then(function () {
    $("pwResetNew").value = "";
    $("pwResetConfirm").value = "";
    forgotIntent = false;
    refreshMe(); // reset unlocked this session: the app opens
  }).catch(function (e) { setMsg("authMsg", e.message, true); });
});
$("btnLock").addEventListener("click", function () {
  apiPost("/api/auth/lock", {}).then(function () { refreshMe(); })
    .catch(function () { showLock(); });
});
function doLogout() {
  apiPost("/api/auth/logout", {}).catch(function () {}).then(function () {
    setToken(null);
    forgotIntent = false;
    showAuth();
  });
}
$("btnLogout").addEventListener("click", doLogout);
$("btnPendingLogout").addEventListener("click", doLogout);

/* ------------------------------------------------------------ screens -- */
var currentScreen = "dashboard";
var navKey = "dashboard";      /* sidebar item currently highlighted */
var docType = "sale";          /* document type on the shared form */
var bookKind = "sale";         /* book view: "sale" | "purchase" */
var docsType = "estimate";     /* document list view type */
var payFilter = "all";         /* payments list filter */
var settingsCat = "general";   /* settings pane category */

var DOC_META = {
  sale:            { title: "Sale Invoice",              bookTitle: "Sale Invoices",      party: "Customer", cashName: "Cash Sale",     add: "＋ Add Sale",     settle: "Receive Payment", noPayment: false, returnsDoc: false },
  purchase:        { title: "Purchase Bill",             bookTitle: "Purchase Bills",     party: "Supplier", cashName: "Cash Purchase", add: "＋ Add Purchase", settle: "Pay Payment",    noPayment: false, returnsDoc: false },
  estimate:        { title: "Estimate/Quotation",        bookTitle: "Estimate/Quotation", party: "Customer", cashName: "Cash Sale",     add: "＋ Add Estimate",  settle: "", noPayment: true,  returnsDoc: false },
  sale_order:      { title: "Sale Order",                bookTitle: "Sale Orders",        party: "Customer", cashName: "Cash Sale",     add: "＋ Add Sale Order", settle: "", noPayment: true, returnsDoc: false },
  purchase_order:  { title: "Purchase Order",            bookTitle: "Purchase Orders",    party: "Supplier", cashName: "Cash Purchase", add: "＋ Add Order",     settle: "", noPayment: true,  returnsDoc: false },
  challan:         { title: "Delivery Challan",          bookTitle: "Delivery Challans",  party: "Customer", cashName: "Cash Sale",     add: "＋ Add Challan",   settle: "", noPayment: true,  returnsDoc: false },
  proforma:        { title: "Proforma Invoice",          bookTitle: "Proforma Invoices",  party: "Customer", cashName: "Cash Sale",     add: "＋ Add Proforma",  settle: "", noPayment: true,  returnsDoc: false },
  sale_return:     { title: "Sale Return / Credit Note", bookTitle: "Sale Returns",       party: "Customer", cashName: "Cash Sale",     add: "＋ Add Return",    settle: "", noPayment: false, returnsDoc: true },
  purchase_return: { title: "Purchase Return / Debit Note", bookTitle: "Purchase Returns", party: "Supplier", cashName: "Cash Purchase", add: "＋ Add Return",  settle: "", noPayment: false, returnsDoc: true },
};
var CONVERTIBLE = { estimate: 1, sale_order: 1, challan: 1, proforma: 1 };

var VIEW_TITLES = {
  dashboard: "Home", parties: "Parties", items: "Items", payments: "Payments",
  expenses: "Expenses", settings: "Settings", cashbank: "Cash & Bank",
  pnl: "Profit & Loss", daybook: "Day Book", stock: "Stock Summary",
  otherincome: "Other Income", reports: "Reports", salesgraph: "Sales Graph",
  godowns: "Manage Godowns", stocktransfer: "Stock Transfer",
  store: "Online Store", loyalty: "Loyalty Points", accounting: "Accounting",
  team: "Sync, Share & Backup", utilities: "Utilities",
  grow: "Grow Your Business", preview: "Invoice Preview",
  itemdetail: "Item Detail", ledger: "Ledger", quick: "Quick Billing",
};

function setActiveNav() {
  document.querySelectorAll(".navbtn").forEach(function (b) {
    b.classList.toggle("active", b.getAttribute("data-nav") === navKey);
  });
  /* Sidebar groups start minimized (Qudoos phone check 2026-11-10):
     a group holding the active screen gets a hint on its header —
     it is NOT auto-opened; groups open only on the user's click. */
  document.querySelectorAll(".nav-group").forEach(function (g) {
    var head = g.querySelector(".nav-group-head");
    if (head) head.classList.toggle("has-active", !!g.querySelector(".navbtn.active"));
  });
}
function showScreen(name) {
  currentScreen = name;
  document.querySelectorAll(".screen").forEach(function (s) { s.classList.add("hidden"); });
  var el = $("screen-" + name);
  if (el) el.classList.remove("hidden");
  var title = VIEW_TITLES[name];
  if (name === "sale") title = DOC_META[docType].title;
  if (name === "book") title = DOC_META[bookKind].bookTitle;
  if (name === "docs") title = DOC_META[docsType].bookTitle;
  $("viewTitle").textContent = title || name;
  setActiveNav();
  loadScreen(name);
  closeSidebarMobile();
}
function closeSidebarMobile() {
  $("sidebar").classList.remove("open");
  $("sideScrim").classList.add("hidden");
}
function go(nav) {
  switch (nav) {
    case "dashboard": navKey = "dashboard"; showScreen("dashboard"); break;
    case "parties": navKey = "parties"; showScreen("parties"); break;
    case "items": navKey = "items"; showScreen("items"); break;
    case "salebook": navKey = "salebook"; bookKind = "sale"; resetBookPeriod(); showScreen("book"); break;
    case "purchasebook": navKey = "purchasebook"; bookKind = "purchase"; resetBookPeriod(); showScreen("book"); break;
    case "estimates": navKey = "estimates"; docsType = "estimate"; showScreen("docs"); break;
    case "saleorders": navKey = "saleorders"; docsType = "sale_order"; showScreen("docs"); break;
    case "challans": navKey = "challans"; docsType = "challan"; showScreen("docs"); break;
    case "salereturns": navKey = "salereturns"; docsType = "sale_return"; showScreen("docs"); break;
    case "purchaseorders": navKey = "purchaseorders"; docsType = "purchase_order"; showScreen("docs"); break;
    case "purchasereturns": navKey = "purchasereturns"; docsType = "purchase_return"; showScreen("docs"); break;
    case "paymentin": navKey = "paymentin"; setPayFilter("in", "in"); showScreen("payments"); showPanel("paymentPanel"); break;
    case "paymentout": navKey = "paymentout"; setPayFilter("out", "out"); showScreen("payments"); showPanel("paymentPanel"); break;
    case "payments": setPayFilter("all", null); showScreen("payments"); break;
    case "quickbilling": navKey = "quickbilling"; showScreen("quick"); break;
    case "expenses": navKey = "expenses"; showScreen("expenses"); break;
    case "cashbank": navKey = "cashbank"; showScreen("cashbank"); break;
    case "pnl": navKey = "pnl"; showScreen("pnl"); break;
    case "daybook": navKey = "daybook"; showScreen("daybook"); break;
    case "stock": navKey = "stock"; showScreen("stock"); break;
    case "settings": navKey = "settings"; showScreen("settings"); break;
    case "proforma": navKey = "proforma"; docsType = "proforma"; showScreen("docs"); break;
    case "otherincome": navKey = "otherincome"; showScreen("otherincome"); break;
    case "reports": navKey = "reports"; openReports("all-transactions"); break;
    case "salesgraph": navKey = "salesgraph"; openReports("sales-graph"); break;
    case "godowns": navKey = "godowns"; showScreen("godowns"); break;
    case "stocktransfer": navKey = "stocktransfer"; showScreen("stocktransfer"); break;
    case "store": navKey = "store"; showScreen("store"); break;
    case "loyalty": navKey = "loyalty"; showScreen("loyalty"); break;
    case "accounting": navKey = "accounting"; showScreen("accounting"); break;
    case "team": navKey = "team"; showScreen("team"); break;
    case "utilities": navKey = "utilities"; showScreen("utilities"); break;
    case "grow": navKey = "grow"; showScreen("grow"); break;
    default: navKey = "dashboard"; showScreen("dashboard");
  }
}
document.querySelectorAll(".navbtn").forEach(function (b) {
  b.addEventListener("click", function () { go(b.getAttribute("data-nav")); });
});
document.querySelectorAll(".nav-group-head").forEach(function (head) {
  head.addEventListener("click", function () {
    var body = $("group-" + head.getAttribute("data-group"));
    if (body) body.classList.toggle("closed");
    head.classList.toggle("closed");
  });
});
function loadScreen(name, silent) {
  if (name === "dashboard") loadDashboard();
  else if (name === "parties") loadParties();
  else if (name === "items") loadItems();
  else if (name === "sale") loadSaleForm();
  else if (name === "book") loadBook();
  else if (name === "docs") loadDocs();
  else if (name === "payments") { loadPayments(); fillPartySelects(); }
  else if (name === "expenses") loadExpenses();
  else if (name === "cashbank") loadCashBank();
  else if (name === "pnl") loadPnl();
  else if (name === "daybook") loadDayBook();
  else if (name === "stock") loadStock();
  else if (name === "otherincome") loadOtherIncome();
  else if (name === "godowns") loadGodowns();
  else if (name === "stocktransfer") loadTransfers();
  else if (name === "store") loadStore();
  else if (name === "loyalty") loadLoyalty();
  else if (name === "accounting") loadAccounting();
  else if (name === "team") loadTeam();
  else if (name === "quick") loadQuick();
  else if (name === "utilities") loadUtilities();
  else if (name === "grow") loadGrow();
  else if (name === "settings") { loadProfile(); loadSettingsDoc(); loadBackups(); if (me && me.isOwner) { loadMailConfig(); loadAccounts(); } }
}

/* ---------------------------------------------------------- dashboard -- */
var dashData = null;
function loadDashboard() {
  apiGet("/api/dashboard", "dashboard").then(function (r) {
    var d = r.body.dashboard;
    dashData = d;
    $("dToday").textContent = fmtRs(d.todaySalesPaisa);
    $("dMonth").textContent = fmtRs(d.monthSalesPaisa);
    $("dReceivable").textContent = fmtRs(d.totalReceivablePaisa);
    $("dCash").textContent = fmtRs(d.cashInHandPaisa);
    $("dBank").textContent = fmtRs(d.bankBalancePaisa || 0);
    $("dStock").textContent = fmtRs(d.stockValuePaisa);
    var pct = d.salesVsLastMonthPct;
    var badge = $("dVsLast");
    if (pct == null) { badge.textContent = "—"; }
    else {
      badge.textContent = (pct >= 0 ? "▲ " : "▼ ") + Math.abs(pct) + "% vs last month";
      badge.style.background = pct >= 0 ? "#e6f4ea" : "#fdecec";
      badge.style.color = pct >= 0 ? "#137333" : "#b3261e";
    }
    $("dTopParty").textContent = d.topReceivableParty
      ? d.topReceivableParty.name + " · " + fmtRs(d.topReceivableParty.amountPaisa)
      : "—";
    $("dTopPayParty").textContent = d.topPayableParty
      ? d.topPayableParty.name + " · " + fmtRs(d.topPayableParty.amountPaisa)
      : "—";
    $("dPurchases").textContent = fmtRs(d.monthPurchasesPaisa || 0);
    $("dTopPurchaseParty").textContent = d.topPurchaseParty
      ? "Top: " + d.topPurchaseParty.name + " · " + fmtRs(d.topPurchaseParty.amountPaisa)
      : "No purchases this month";
    var chq = d.openCheques || {};
    $("dChqIn").textContent = (chq.receivedCount || 0) + " · " + fmtRs(chq.receivedPaisa || 0);
    $("dChqOut").textContent = (chq.paidCount || 0) + " · " + fmtRs(chq.paidPaisa || 0);
    renderExpChart(d.expensesSeries || []);
    var low = d.lowStockCount || 0;
    $("dLowRow").classList.toggle("hidden", low === 0);
    if (low) $("dLowStock").textContent = low + (low === 1 ? " item" : " items");
    var exp = d.expiringItems || [];
    $("dExpiringList").innerHTML = exp.length ? exp.slice(0, 8).map(function (e) {
      var right = '<div class="amt">' + e.expDate + "</div>";
      return '<div class="row">' + rowHtml(e.itemName, "Batch " + (e.batchNo || "—") + " · " + e.qty + " left", right, "") + "</div>";
    }).join("") : '<p class="empty">No stock is expiring soon.</p>';
    renderSaleGraph();
    applyWidgets();
    /* PROFIT / EXPENSES (THIS MONTH) come from the SAME dashboard
       payload as every other Home figure (round-3 fix): a separate
       /api/pnl fetch let live profit render beside a stale cached
       all-zero dashboard, so the screen disagreed with itself. */
    $("dProfit").textContent = fmtRs(d.monthProfitPaisa || 0);
    $("dExpenses").textContent = fmtRs(d.monthExpensesPaisa || 0);
  }).catch(function () {});
  apiGet("/api/parties", "parties").then(function (r) {
    var pay = 0;
    (r.body.parties || []).forEach(function (p) { if (p.balancePaisa < 0) pay += -p.balancePaisa; });
    $("dPayable").textContent = fmtRs(pay);
  }).catch(function () {});
  apiGet("/api/invoices", "invoices").then(function (r) {
    var open = function (t) {
      return (r.body.invoices || []).filter(function (i) {
        return i.type === t && !i.convertedToId;
      }).length;
    };
    $("dOpenOrders").textContent = open("sale_order");
    $("dOpenChallans").textContent = open("challan");
  }).catch(function () {});
}

/* Total Sale line graph — aggregates the daily series by period. */
function graphBuckets() {
  var series = (dashData && dashData.salesSeries) || [];
  var period = $("saleGraphPeriod").value;
  if (period === "daily") return series.map(function (p) { return { label: p.date, amount: p.amountPaisa }; });
  var fmtKey;
  if (period === "weekly") {
    fmtKey = function (dateStr) {
      var d = new Date(dateStr + "T00:00:00");
      d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
      return d.toISOString().slice(0, 10);
    };
  } else if (period === "monthly") fmtKey = function (s) { return s.slice(0, 7); };
  else fmtKey = function (s) { return s.slice(0, 4); };
  var map = {}, order = [];
  series.forEach(function (p) {
    var k = fmtKey(p.date);
    if (!(k in map)) { map[k] = 0; order.push(k); }
    map[k] += p.amountPaisa;
  });
  return order.map(function (k) { return { label: k, amount: map[k] }; });
}
function renderExpChart(series) {
  var svg = $("dExpChart");
  if (!svg) return;
  var max = 0;
  series.forEach(function (s) { if (s.amountPaisa > max) max = s.amountPaisa; });
  if (!max) { svg.innerHTML = ""; return; }
  var bars = series.map(function (s, i) {
    var h = Math.max(1, Math.round(32 * s.amountPaisa / max));
    return '<rect x="' + (i * 4) + '" y="' + (34 - h) + '" width="3" height="' + h + '" fill="#b3261e" opacity="0.75"><title>' + esc(s.date) + ": " + esc(fmtRs(s.amountPaisa)) + "</title></rect>";
  }).join("");
  svg.innerHTML = bars;
}
function renderSaleGraph() {
  var svg = $("saleGraph"), tip = $("saleGraphTip");
  if (!svg || !dashData) return;
  var pts = graphBuckets();
  var pct = dashData.salesVsLastMonthPct;
  $("saleGraphPct").textContent = pct == null ? "" :
    (pct >= 0 ? "▲ " : "▼ ") + Math.abs(pct) + "% vs last month";
  $("saleGraphNote").textContent = "Last month total: " +
    fmtRs(dashData.lastMonthSalesPaisa || 0);
  if (!pts.length) { svg.innerHTML = ""; return; }
  var W = 640, H = 180, padL = 8, padR = 8, padT = 14, padB = 22;
  var max = Math.max.apply(null, pts.map(function (p) { return p.amount; }).concat([1]));
  var step = pts.length > 1 ? (W - padL - padR) / (pts.length - 1) : 0;
  var coords = pts.map(function (p, i) {
    return {
      x: padL + step * i,
      y: padT + (H - padT - padB) * (1 - p.amount / max),
      label: p.label, amount: p.amount,
    };
  });
  var line = coords.map(function (c, i) {
    return (i === 0 ? "M" : "L") + c.x.toFixed(1) + "," + c.y.toFixed(1);
  }).join(" ");
  var area = line + " L" + coords[coords.length - 1].x.toFixed(1) + "," + (H - padB) +
    " L" + coords[0].x.toFixed(1) + "," + (H - padB) + " Z";
  var html = '<path d="' + area + '" fill="#f26522" opacity="0.12"/>' +
    '<path d="' + line + '" fill="none" stroke="#f26522" stroke-width="2.5" stroke-linejoin="round"/>';
  coords.forEach(function (c, i) {
    html += '<circle class="gdot" data-i="' + i + '" cx="' + c.x.toFixed(1) + '" cy="' + c.y.toFixed(1) +
      '" r="4" fill="#fff" stroke="#f26522" stroke-width="2"/>';
  });
  if (coords.length) {
    var f = coords[0], l = coords[coords.length - 1];
    html += '<text x="' + f.x + '" y="' + (H - 6) + '" font-size="10" fill="#667085">' + esc(f.label) + "</text>";
    html += '<text x="' + l.x + '" y="' + (H - 6) + '" font-size="10" fill="#667085" text-anchor="end">' + esc(l.label) + "</text>";
  }
  svg.innerHTML = html;
  svg.querySelectorAll(".gdot").forEach(function (dot) {
    dot.addEventListener("mouseenter", function () {
      var c = coords[+dot.getAttribute("data-i")];
      tip.classList.remove("hidden");
      tip.textContent = c.label + " · " + fmtRs(c.amount);
      tip.style.left = (c.x / W * 100) + "%";
      tip.style.top = (c.y / H * 100 - 16) + "%";
    });
    dot.addEventListener("mouseleave", function () { tip.classList.add("hidden"); });
  });
}
function applyWidgets() {
  var w = (QB.settings && QB.settings.widgets) || { cash_in_hand: true, bank_balance: true, low_stock: true, expiring_stock: true };
  document.querySelectorAll("[data-widget]").forEach(function (cb) {
    cb.checked = !!w[cb.getAttribute("data-widget")];
  });
  $("widgetBankRow").classList.toggle("hidden", !w.bank_balance);
  $("widgetCashCard").classList.toggle("hidden", !w.cash_in_hand);
  $("dLowRow").classList.toggle("hidden", !w.low_stock || !(dashData && dashData.lowStockCount));
  $("dExpiringCard").classList.toggle("hidden", !w.expiring_stock);
}

/* ------------------------------------------------------------ parties -- */
function rowHtml(main, sub, amt, cls) {
  return '<div><div class="main">' + esc(main) + '</div>' +
    (sub ? '<div class="sub">' + esc(sub) + "</div>" : "") + "</div>" +
    (amt != null ? '<div class="amt ' + (cls || "") + '">' + amt + "</div>" : "");
}
var editingPartyId = null;
var partyExtrasDefsCache = [];
function partyGroupsList(parties) {
  var seen = {}, out = [];
  (parties || []).forEach(function (p) {
    if (p.groupName && !seen[p.groupName]) { seen[p.groupName] = 1; out.push(p.groupName); }
  });
  return out;
}
function renderPartyFormExtras() {
  var s = QB.settings || {};
  partyExtrasDefsCache = (s.party_extra_fields || []).filter(function (f) { return f.enabled; });
  $("partyExtras").innerHTML = partyExtrasDefsCache.map(function (f, i) {
    return '<label>' + esc(f.label || ("Field " + (i + 1))) +
      ' <input type="text" data-party-extra="' + i + '"></label>';
  }).join("");
}
/* ------------------------------------------- list-first panels ----- */
/* Delta build #3: screens show their list + an Add button; add/edit
   forms live in panels that open only on Add / Edit and close on
   Save or Back. */
function showPanel(id) { var p = $(id); if (p) p.classList.remove("hidden"); }
function hidePanel(id) { var p = $(id); if (p) p.classList.add("hidden"); }
function deleteRecord(what, detail, path, done, exactMsg) {
  /* exactMsg (audit 2026-11-10): where Vyapar's own confirmation wording
     is verified by research, the dialog opens with that exact sentence;
     the record name + Recycle Bin note follow as detail. */
  var msg = exactMsg ? exactMsg + (detail ? "\n" + detail : "")
                     : "Delete " + what + "?" + (detail ? "\n" + detail : "");
  if (!confirm(msg)) return;
  apiDelete(path).then(function () {
    setMsg("appMsg", what + " deleted.");
    if (done) done();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
function miniBtn(label, fn) {
  var b = document.createElement("button");
  b.className = "btn btn-mini"; b.type = "button"; b.textContent = label;
  b.addEventListener("click", fn);
  return b;
}

/* Party Status (audit 2026-11-10, research §2): Vyapar parties carry an
   active/inactive status — a "Party Status:" toggle on the party detail
   (our Ledger screen), a status filter + bulk status update on the list,
   and inactive parties hidden from the bill/payment pickers. All of it
   is governed by Settings > PARTY > Manage Party Status. */
var partyStatusFilter = "active";
var lastPartyList = [];
function partyInactive(p) {
  return String((p && p.status) || "active").toLowerCase() === "inactive";
}
function setPartyStatus(p, status, done) {
  apiPut("/api/parties/" + p.id, { status: status }).then(function () {
    setMsg("appMsg", p.name + " is now " + (status === "inactive" ? "Inactive" : "Active") + ".");
    if (done) done();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
function loadParties() {
  apiGet("/api/parties", "parties").then(function (r) {
    var s = QB.settings || {};
    $("partyStatusChips").classList.toggle("hidden", s.party_manage_status === false);
    renderPartyFormExtras();
    var parties = r.body.parties || [];
    var groups = partyGroupsList(parties);
    $("partyGroupChips").innerHTML = s.party_grouping === false ? "" :
      groups.map(function (g) {
        return '<span class="rep-chip">' + esc(g) + "</span>";
      }).join("");
    var gs = $("partyGroup");
    gs.innerHTML = '<option value="">— No group —</option>' + groups.map(function (g) {
      return '<option>' + esc(g) + "</option>";
    }).join("");
    var dues = parties.filter(function (p) { return p.balancePaisa > 0 && !partyInactive(p); });
    $("reminderCard").classList.toggle("hidden", !(s.party_reminder_enabled && dues.length));
    $("reminderList").innerHTML = dues.slice(0, 10).map(function (p) {
      return '<div class="row"><div><div class="main">' + esc(p.name) + '</div><div class="sub">Owes ' +
        fmtRs(p.balancePaisa) + '</div></div><button class="btn btn-mini" data-remind="' + p.id + '">Remind</button></div>';
    }).join("");
    $("reminderList").querySelectorAll("[data-remind]").forEach(function (b) {
      b.addEventListener("click", function () {
        var p = parties.filter(function (x) { return String(x.id) === b.getAttribute("data-remind"); })[0];
        if (p) sendReminder(p);
      });
    });
    var q = searchText();
    var manageStatus = s.party_manage_status !== false;
    var list = parties.filter(function (p) {
      if (q && p.name.toLowerCase().indexOf(q) < 0) return false;
      if (manageStatus && partyStatusFilter === "active" && partyInactive(p)) return false;
      if (manageStatus && partyStatusFilter === "inactive" && !partyInactive(p)) return false;
      return true;
    });
    lastPartyList = list;
    $("partyList").innerHTML = list.length ? "" : '<p class="empty">No parties here.</p>';
    list.forEach(function (p) {
      var wrap = document.createElement("div");
      wrap.className = "row";
      var sub = [p.phone || "", p.groupName || "", partyInactive(p) ? "Inactive" : "",
        p.creditLimitType === "custom" ? "Limit " + fmtRs(p.creditLimitPaisa || 0) : ""]
        .filter(Boolean).join(" · ");
      wrap.innerHTML = rowHtml(p.name, sub, fmtRs(p.balancePaisa), p.balancePaisa > 0 ? "neg" : "");
      var actions = document.createElement("div");
      actions.className = "row-actions";
      var led = document.createElement("button");
      led.className = "btn btn-mini"; led.type = "button"; led.textContent = "Ledger";
      led.addEventListener("click", function () { openLedger(p); });
      actions.appendChild(led);
      var ed = document.createElement("button");
      ed.className = "btn btn-mini"; ed.type = "button"; ed.textContent = "Edit";
      ed.addEventListener("click", function () { startEditParty(p); });
      actions.appendChild(ed);
      actions.appendChild(miniBtn("Delete", function () {
        var warn = p.balancePaisa ? "It has an open balance of " + fmtRs(p.balancePaisa) + ". " : "";
        deleteRecord("party '" + p.name + "'",
          warn + "It moves to the Recycle Bin (Utilities) and leaves your lists and totals until you restore it. Its bills and payments stay saved.",
          "/api/parties/" + p.id, function () { loadParties(); loadDashboard(); });
      }));
      if (manageStatus) {
        actions.appendChild(miniBtn(partyInactive(p) ? "Activate" : "Deactivate", function () {
          setPartyStatus(p, partyInactive(p) ? "active" : "inactive",
            function () { loadParties(); fillPartySelects(); });
        }));
      }
      wrap.appendChild(actions);
      $("partyList").appendChild(wrap);
    });
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
document.querySelectorAll("[data-pstatus]").forEach(function (b) {
  b.addEventListener("click", function () {
    partyStatusFilter = b.getAttribute("data-pstatus");
    document.querySelectorAll("[data-pstatus]").forEach(function (x) {
      x.classList.toggle("active", x === b);
    });
    loadParties();
  });
});
function bulkPartyStatus(status) {
  var targets = lastPartyList.filter(function (p) {
    return partyInactive(p) !== (status === "inactive");
  });
  if (!targets.length) { setMsg("appMsg", "Nothing to change in the shown list.", true); return; }
  if (!confirm("Mark " + targets.length + " shown parties " +
      (status === "inactive" ? "Inactive" : "Active") + "?")) return;
  var chain = Promise.resolve();
  targets.forEach(function (p) {
    chain = chain.then(function () {
      return apiPut("/api/parties/" + p.id, { status: status });
    });
  });
  chain.then(function () {
    setMsg("appMsg", targets.length + " parties marked " +
      (status === "inactive" ? "Inactive" : "Active") + ".");
    loadParties(); fillPartySelects();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
$("btnBulkActivate").addEventListener("click", function () { bulkPartyStatus("active"); });
$("btnBulkDeactivate").addEventListener("click", function () { bulkPartyStatus("inactive"); });
function sendReminder(p) {
  var s = QB.settings || {};
  var tmpl = s.party_reminder_message ||
    "Assalam-o-Alaikum [Party Name], a gentle reminder: [Amount] is due at [Business Name]. Please pay at your earliest convenience. Thank you.";
  var msg = tmpl.split("[Party Name]").join(p.name)
    .split("[Amount]").join(fmtRs(p.balancePaisa))
    .split("[Business Name]").join((me && me.business && me.business.name) || "QBill");
  var phone = (p.phone || "").replace(/\D/g, "");
  if (phone && phone.charAt(0) === "0") phone = "92" + phone.slice(1);
  window.open("https://wa.me/" + phone + "?text=" + encodeURIComponent(msg), "_blank");
}
$("btnSendRemindersBulk").addEventListener("click", function () {
  var parties = ((cacheGet("parties") || {}).parties) || [];
  var dues = parties.filter(function (p) { return p.balancePaisa > 0 && p.phone; });
  if (!dues.length) { setMsg("appMsg", "No party with a due balance and a phone number.", true); return; }
  if (!confirm("Open WhatsApp for " + dues.length + " parties, one by one?")) return;
  dues.slice(0, 5).forEach(function (p, i) {
    setTimeout(function () { sendReminder(p); }, i * 900);
  });
  setMsg("appMsg", "Reminders opened for the first " + Math.min(dues.length, 5) +
    " parties (browsers block opening many tabs at once).");
});
function startEditParty(p) {
  showPanel("partyPanel");
  editingPartyId = p.id;
  $("partyFormTitle").textContent = "Edit party";
  $("partySubmitBtn").textContent = "Save changes";
  $("partyCancelEdit").classList.remove("hidden");
  $("partyDeleteBtn").classList.remove("hidden");
  $("partyName").value = p.name || "";
  $("partyPhone").value = p.phone || "";
  $("partyEmail").value = p.email || "";
  $("partyOpening").value = ((p.openingBalancePaisa || 0) / 100).toFixed(2);
  $("partyOpeningDate").value = p.openingDate || "";
  var ctype = p.creditLimitType || "none";
  document.querySelectorAll('[name="partyCreditType"]').forEach(function (rb) {
    rb.checked = rb.value === ctype;
  });
  $("partyCreditLimit").value = ((p.creditLimitPaisa || 0) / 100).toFixed(2);
  $("partyCreditLimitWrap").classList.toggle("hidden", ctype !== "custom");
  $("partyStatus").value = partyInactive(p) ? "inactive" : "active";
  $("partyLoyalty").value = p.loyaltyPoints || 0;
  $("partyBilling").value = p.billingAddress || "";
  $("partyShipping").value = p.shippingAddress || "";
  if (p.groupName) {
    var gs = $("partyGroup");
    if (![].some.call(gs.options, function (o) { return o.value === p.groupName; })) {
      var o = document.createElement("option"); o.value = p.groupName; o.textContent = p.groupName;
      gs.appendChild(o);
    }
    gs.value = p.groupName;
  }
  document.querySelectorAll("[data-party-extra]").forEach(function (inp) {
    var def = partyExtrasDefsCache[+inp.getAttribute("data-party-extra")];
    if (def) inp.value = (p.extras || {})[def.label] || "";
  });
  $("partyName").focus();
}
$("partyDeleteBtn").addEventListener("click", function () {
  if (!editingPartyId) return;
  var name = $("partyName").value.trim();
  deleteRecord("party '" + name + "'",
    "It moves to the Recycle Bin (Utilities) and leaves your lists and totals until you restore it. Its bills and payments stay saved.",
    "/api/parties/" + editingPartyId, function () {
      $("partyCancelEdit").click();
      hidePanel("partyPanel");
      loadParties(); loadDashboard();
    });
});
$("partyCancelEdit").addEventListener("click", function () {
  editingPartyId = null;
  $("partyDeleteBtn").classList.add("hidden");
  $("partyForm").reset(); $("partyOpening").value = 0;
  $("partyFormTitle").textContent = "Add party";
  $("partySubmitBtn").textContent = "Add party";
  $("partyCancelEdit").classList.add("hidden");
});
$("partyForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  var extras = {};
  document.querySelectorAll("[data-party-extra]").forEach(function (inp) {
    var def = partyExtrasDefsCache[+inp.getAttribute("data-party-extra")];
    if (def && inp.value.trim()) extras[def.label] = inp.value.trim();
  });
  var creditType = (document.querySelector('[name="partyCreditType"]:checked') || {}).value || "none";
  var payload = {
    name: $("partyName").value.trim(), phone: $("partyPhone").value.trim(),
    email: $("partyEmail").value.trim(),
    opening_balance_paisa: rsToPaisa($("partyOpening").value),
    opening_date: $("partyOpeningDate").value || "",
    credit_limit_type: creditType,
    credit_limit_paisa: creditType === "custom" ? rsToPaisa($("partyCreditLimit").value) : 0,
    group_name: ($("partyGroupNew").value.trim() || $("partyGroup").value || "").trim(),
    status: $("partyStatus").value,
    billing_address: $("partyBilling").value.trim(),
    shipping_address: $("partyShipping").value.trim(),
    loyalty_points: parseInt($("partyLoyalty").value || "0", 10) || 0,
    extras: extras
  };
  var done = function (msg) {
    editingPartyId = null;
    $("partyForm").reset(); $("partyOpening").value = 0;
    $("partyCreditLimitWrap").classList.add("hidden");
    $("partyFormTitle").textContent = "Add party";
    $("partySubmitBtn").textContent = "Add party";
    $("partyCancelEdit").classList.add("hidden");
    if (partySaveNewFlag) { partySaveNewFlag = false; msg += " Add the next party."; $("partyName").focus(); }
    else hidePanel("partyPanel");
    setMsg("appMsg", msg);
    loadParties();
  };
  if (editingPartyId) {
    apiPut("/api/parties/" + editingPartyId, payload).then(function () { done("Party updated."); })
      .catch(function (e) { setMsg("appMsg", e.message, true); });
  } else {
    apiCreate("party", payload).then(function (r) {
      done(r.queued ? "Saved on this device — will sync when online." : "Party added.");
    }).catch(function (e) { setMsg("appMsg", e.message, true); });
  }
});

/* Party form tabs (Address / Credit & Balance / Additional Fields). */
var partySaveNewFlag = false;
document.querySelectorAll("[data-partytab]").forEach(function (btn) {
  btn.addEventListener("click", function () {
    document.querySelectorAll("[data-partytab]").forEach(function (b) {
      b.classList.toggle("active", b === btn);
    });
    document.querySelectorAll("[data-partypanel]").forEach(function (p) {
      p.classList.toggle("hidden", p.getAttribute("data-partypanel") !== btn.getAttribute("data-partytab"));
    });
  });
});
document.querySelectorAll('[name="partyCreditType"]').forEach(function (rb) {
  rb.addEventListener("change", function () {
    $("partyCreditLimitWrap").classList.toggle("hidden", rb.value !== "custom" || !rb.checked);
  });
});
$("partySaveNew").addEventListener("click", function () {
  partySaveNewFlag = true;
  $("partyForm").requestSubmit();
});
$("btnAddParty").addEventListener("click", function () {
  if (editingPartyId) $("partyCancelEdit").click();
  showPanel("partyPanel");
  $("partyName").focus();
});
$("btnPartyBack").addEventListener("click", function () {
  if (editingPartyId) $("partyCancelEdit").click();
  hidePanel("partyPanel");
});
$("btnOpenTeam").addEventListener("click", function () { go("team"); });

/* ------------------------------------------------------------- ledger -- */
var currentLedgerParty = null;
function renderLedgerStatus() {
  var rowEl = $("ledgerStatusRow");
  if (!rowEl) return;
  var manage = !QB.settings || QB.settings.party_manage_status !== false;
  rowEl.classList.toggle("hidden", !manage || !currentLedgerParty);
  if (currentLedgerParty) {
    $("ledgerStatusToggle").textContent = partyInactive(currentLedgerParty) ? "Inactive" : "Active";
  }
}
$("ledgerStatusToggle").addEventListener("click", function () {
  if (!currentLedgerParty) return;
  var next = partyInactive(currentLedgerParty) ? "active" : "inactive";
  setPartyStatus(currentLedgerParty, next, function () {
    currentLedgerParty.status = next;
    renderLedgerStatus();
    fillPartySelects();
  });
});
function openLedger(party) {
  showScreen("parties");
  document.querySelectorAll(".screen").forEach(function (s) { s.classList.add("hidden"); });
  $("screen-ledger").classList.remove("hidden");
  currentScreen = "ledger";
  currentLedgerParty = party;
  renderLedgerStatus();
  $("ledgerTitle").textContent = "Ledger — " + party.name;
  apiGet("/api/parties/" + party.id + "/ledger", "ledger_" + party.id).then(function (r) {
    $("ledgerBalance").textContent = "Balance: " + fmtRs(r.body.balancePaisa);
    var list = r.body.entries || [];
    $("ledgerList").innerHTML = list.length ? "" : '<p class="empty">No entries yet.</p>';
    list.forEach(function (e) {
      var div = document.createElement("div");
      div.className = "row";
      var movement = e.debitPaisa ? "+" + fmtRs(e.debitPaisa) : "-" + fmtRs(e.creditPaisa);
      div.innerHTML = rowHtml(e.description, (e.date || "").slice(0, 10),
        movement + " · Bal " + fmtRs(e.balancePaisa));
      $("ledgerList").appendChild(div);
    });
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
$("btnLedgerBack").addEventListener("click", function () { showScreen("parties"); });

/* -------------------------------------------------------------- items -- */
/* -------------------------------------------------------------- items -- */
var itemsTab = "products";
var editingItemId = null;
function itemCategories() {
  try { return JSON.parse(localStorage.getItem("qbill_item_categories") || "[]"); } catch (e) { return []; }
}
function setItemCategories(list) { localStorage.setItem("qbill_item_categories", JSON.stringify(list)); }
var STANDARD_UNITS = [
  ["Bags", "Bag"], ["Bottles", "Btl"], ["Box", "Box"], ["Bundles", "Bdl"],
  ["Cans", "Can"], ["Cartons", "Ctn"], ["Dozens", "Dzn"], ["Grammes", "Gm"],
  ["Kilograms", "Kg"], ["Litre", "Ltr"], ["Meters", "Mtr"], ["Mililitre", "Ml"],
  ["Numbers", "Nos"], ["Packs", "Pac"], ["Pairs", "Prs"], ["Pieces", "Pcs"],
  ["Quintal", "Qtl"], ["Rolls", "Rol"], ["Square Feet", "Sqft"]
];
function itemUnits() {
  var stored = null;
  try { stored = JSON.parse(localStorage.getItem("qbill_item_units") || "null"); } catch (e) {}
  var list = stored && stored.length ? stored.slice() : [
    { name: "Pcs", code: "Pcs", secondary: "", conversion: 0 },
    { name: "Box", code: "Box", secondary: "", conversion: 0 },
    { name: "Kg", code: "Kg", secondary: "", conversion: 0 },
    { name: "Litre", code: "Ltr", secondary: "", conversion: 0 },
    { name: "Dozen", code: "Dzn", secondary: "Pcs", conversion: 12 }
  ];
  var have = {};
  list.forEach(function (u) { have[(u.name || "").toLowerCase()] = 1; });
  STANDARD_UNITS.forEach(function (pair) {
    if (!have[pair[0].toLowerCase()]) {
      list.push({ name: pair[0], code: pair[1], secondary: "", conversion: 0 });
    }
  });
  return list;
}
function setItemUnits(list) { localStorage.setItem("qbill_item_units", JSON.stringify(list)); }
function refreshItemUnitSelects() {
  var units = itemUnits();
  ["itemUnit", "itemSecondaryUnit"].forEach(function (id) {
    var sel = $(id);
    var keep = sel.value;
    sel.innerHTML = units.map(function (u) { return "<option>" + esc(u.name) + "</option>"; }).join("");
    if (keep) sel.value = keep;
  });
}
function refreshItemCategorySelect() {
  var items = ((cacheGet("items") || {}).items) || [];
  var cats = itemCategories();
  items.forEach(function (i) {
    if (i.category && cats.indexOf(i.category) < 0) cats.push(i.category);
  });
  var sel = $("itemCategory");
  var keep = sel.value;
  sel.innerHTML = '<option value="">— None —</option>' + cats.map(function (c) {
    return "<option>" + esc(c) + "</option>";
  }).join("");
  if (keep) sel.value = keep;
}
function renderCategoryPanel() {
  var items = ((cacheGet("items") || {}).items) || [];
  var cats = itemCategories();
  items.forEach(function (i) {
    if (i.category && cats.indexOf(i.category) < 0) cats.push(i.category);
  });
  $("categoryList").innerHTML = cats.length ? cats.map(function (c) {
    var n = items.filter(function (i) { return i.category === c; }).length;
    return '<div class="row"><div><div class="main">' + esc(c) + '</div><div class="sub">' + n +
      ' items</div></div><button class="btn btn-mini" type="button" data-cat-del="' + esc(c) + '">Remove</button></div>';
  }).join("") : '<p class="empty">No categories yet.</p>';
  $("categoryList").querySelectorAll("[data-cat-del]").forEach(function (b) {
    b.addEventListener("click", function () {
      var c = b.getAttribute("data-cat-del");
      var used = items.filter(function (i) { return i.category === c; }).length;
      if (used) { setMsg("appMsg", used + " item(s) use the category '" + c + "' - change those items first, then remove the category.", true); return; }
      if (!confirm("Remove category '" + c + "'?")) return;
      setItemCategories(itemCategories().filter(function (x) { return x !== c; }));
      renderCategoryPanel(); refreshItemCategorySelect();
    });
  });
}
function renderUnitPanel() {
  $("unitList").innerHTML = itemUnits().map(function (u) {
    return '<div class="row"><div><div class="main">' + esc(u.name) +
      (u.code ? " (" + esc(u.code) + ")" : "") + '</div><div class="sub">' +
      (u.secondary ? ("1 " + esc(u.name) + " = " + u.conversion + " " + esc(u.secondary)) : "Base unit") +
      '</div></div><button class="btn btn-mini" type="button" data-unit-del="' + esc(u.name) + '">Remove</button></div>';
  }).join("");
  $("unitList").querySelectorAll("[data-unit-del]").forEach(function (b) {
    b.addEventListener("click", function () {
      var name = b.getAttribute("data-unit-del");
      var items = ((cacheGet("items") || {}).items) || [];
      var used = items.filter(function (i) { return i.unit === name; }).length;
      if (used) { setMsg("appMsg", used + " item(s) use the unit '" + name + "' - change those items first, then remove the unit.", true); return; }
      if (!confirm("Remove unit '" + name + "' from the unit list?")) return;
      setItemUnits(itemUnits().filter(function (u) { return u.name !== name; }));
      renderUnitPanel(); refreshItemUnitSelects();
    });
  });
}
function loadItems() {
  refreshItemUnitSelects();
  apiGet("/api/items", "items").then(function (r) {
    refreshItemCategorySelect();
    var q = searchText();
    var all = (r.body.items || []);
    var list = all.filter(function (i) {
      if (itemsTab === "services" && i.itemType !== "service") return false;
      if (itemsTab === "products" && i.itemType === "service") return false;
      return !q || i.name.toLowerCase().indexOf(q) >= 0;
    });
    $("itemList").innerHTML = list.length ? "" : '<p class="empty">No items yet.</p>';
    $("itemList").classList.toggle("hidden", itemsTab === "category" || itemsTab === "units");
    $("itemsCategory").classList.toggle("hidden", itemsTab !== "category");
    $("itemsUnits").classList.toggle("hidden", itemsTab !== "units");
    list.forEach(function (i) {
      var div = document.createElement("div");
      div.className = "row";
      var low = (i.minStock || 0) > 0 ? i.stockQty < i.minStock : i.stockQty <= 0;
      div.innerHTML = rowHtml(i.name,
        "Sale " + fmtRs(i.salePricePaisa) + " · Purchase " + fmtRs(i.purchasePricePaisa) +
        (i.category ? " · " + i.category : "") + (i.itemCode ? " · " + i.itemCode : ""),
        "Stock: " + i.stockQty + (low ? " ⚠ low" : ""));
      var actions = document.createElement("div");
      actions.className = "row-actions";
      var det = document.createElement("button");
      det.className = "btn btn-mini"; det.type = "button"; det.textContent = "Detail";
      det.addEventListener("click", function () { openItemDetail(i); });
      actions.appendChild(det);
      var ed = document.createElement("button");
      ed.className = "btn btn-mini"; ed.type = "button"; ed.textContent = "Edit";
      ed.addEventListener("click", function () { startEditItem(i); });
      actions.appendChild(ed);
      actions.appendChild(miniBtn("Delete", function () {
        deleteRecord("item '" + i.name + "'",
          "'" + i.name + "' — Stock on hand: " + i.stockQty + ". It moves to the Recycle Bin (Utilities) and leaves your lists, reports and the sale form until you restore it. Bills that used it stay as they are.",
          "/api/items/" + i.id, function () { loadItems(); loadDashboard(); },
          "Do you really want to delete this item?");
      }));
      div.appendChild(actions);
      $("itemList").appendChild(div);
    });
    renderCategoryPanel();
    renderUnitPanel();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
document.querySelectorAll("[data-itemtab]").forEach(function (b) {
  b.addEventListener("click", function () {
    itemsTab = b.getAttribute("data-itemtab");
    document.querySelectorAll("[data-itemtab]").forEach(function (x) {
      x.classList.toggle("active", x === b);
    });
    loadItems();
  });
});
$("btnCatAdd").addEventListener("click", function () {
  var v = $("catNew").value.trim();
  if (!v) return;
  var cats = itemCategories();
  if (cats.indexOf(v) < 0) cats.push(v);
  setItemCategories(cats);
  $("catNew").value = "";
  hidePanel("catAddPanel");
  refreshItemCategorySelect(); renderCategoryPanel();
});
$("btnAddCategory").addEventListener("click", function () { showPanel("catAddPanel"); $("catNew").focus(); });
$("btnCatCancel").addEventListener("click", function () { hidePanel("catAddPanel"); });
$("btnUnitAdd").addEventListener("click", function () {
  var v = $("unitNew").value.trim();
  if (!v) return;
  var units = itemUnits();
  units.push({ name: v, secondary: $("unitNewSecondary").value.trim(),
               conversion: parseFloat($("unitNewConv").value || "0") || 0 });
  setItemUnits(units);
  $("unitNew").value = ""; $("unitNewSecondary").value = ""; $("unitNewConv").value = "";
  hidePanel("unitAddPanel");
  refreshItemUnitSelects(); renderUnitPanel();
});
$("btnAddUnit").addEventListener("click", function () { showPanel("unitAddPanel"); $("unitNew").focus(); });
$("btnUnitCancel").addEventListener("click", function () { hidePanel("unitAddPanel"); });
$("btnAssignCode").addEventListener("click", function () {
  $("itemCode").value = "ITM-" + Math.floor(1000 + Math.random() * 9000);
});
$("btnAddBatchRow").addEventListener("click", function () {
  var row = document.createElement("div");
  row.className = "grid2";
  row.innerHTML = '<input class="bt-no" placeholder="Batch No.">' +
    '<input class="bt-qty" type="number" step="1" min="0" placeholder="Qty">' +
    '<input class="bt-exp" placeholder="Exp (mm/yy)">' +
    '<input class="bt-mfg" placeholder="Mfg (mm/yy)">';
  $("batchRows").appendChild(row);
});
function startEditItem(i) {
  showPanel("itemPanel");
  editingItemId = i.id;
  $("itemDeleteBtn").classList.remove("hidden");
  $("itemFormTitle").textContent = "Edit item";
  $("itemSubmitBtn").textContent = "Save changes";
  $("itemCancelEdit").classList.remove("hidden");
  $("itemType").value = i.itemType || "product";
  $("itemName").value = i.name || "";
  $("itemCode").value = i.itemCode || "";
  $("itemSale").value = ((i.salePricePaisa || 0) / 100).toFixed(2);
  $("itemPurchase").value = ((i.purchasePricePaisa || 0) / 100).toFixed(2);
  $("itemWholesale").value = ((i.wholesalePricePaisa || 0) / 100).toFixed(2);
  $("itemMinWholesaleQty").value = i.minWholesaleQty || 0;
  $("itemMrp").value = ((i.mrpPaisa || 0) / 100).toFixed(2);
  $("itemTaxPct").value = (i.taxBp || 0) / 100;
  $("itemDiscountType").value = i.discountType || "percentage";
  $("itemDiscountPct").value = i.discountType === "amount"
    ? ((i.discountAmountPaisa || 0) / 100).toFixed(2)
    : (i.discountBp || 0) / 100;
  $("itemStock").value = i.stockQty || 0;
  $("itemMinStock").value = i.minStock || 0;
  $("itemLocation").value = i.location || "";
  $("itemDescription").value = i.description || "";
  if (i.category) $("itemCategory").value = i.category;
  if (i.unit) $("itemUnit").value = i.unit;
  if (i.secondaryUnit) $("itemSecondaryUnit").value = i.secondaryUnit;
  $("itemConversion").value = i.conversion || 0;
  $("itemName").focus();
}
$("itemDeleteBtn").addEventListener("click", function () {
  if (!editingItemId) return;
  var name = $("itemName").value.trim();
  deleteRecord("item '" + name + "'",
    "'" + name + "' moves to the Recycle Bin (Utilities) and leaves your lists, reports and the sale form until you restore it. Bills that used it stay as they are.",
    "/api/items/" + editingItemId, function () {
      $("itemCancelEdit").click();
      hidePanel("itemPanel");
      loadItems(); loadDashboard();
    }, "Do you really want to delete this item?");
});
$("itemCancelEdit").addEventListener("click", function () {
  editingItemId = null;
  $("itemDeleteBtn").classList.add("hidden");
  $("itemForm").reset();
  $("itemFormTitle").textContent = "Add item";
  $("itemSubmitBtn").textContent = "Add item";
  $("itemCancelEdit").classList.add("hidden");
});
$("itemForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  var batches = [];
  var batchSum = 0;
  document.querySelectorAll("#batchRows > div").forEach(function (row) {
    var no = row.querySelector(".bt-no").value.trim();
    var qty = parseInt(row.querySelector(".bt-qty").value || "0", 10) || 0;
    if (no && qty > 0) {
      batches.push({ batch_no: no, qty: qty,
        exp_date: row.querySelector(".bt-exp").value.trim(),
        mfg_date: row.querySelector(".bt-mfg").value.trim() });
      batchSum += qty;
    }
  });
  var stockQty = parseInt($("itemStock").value || "0", 10) || 0;
  if (!editingItemId && batches.length && batchSum > 0) stockQty = batchSum;
  var payload = {
    name: $("itemName").value.trim(),
    item_type: $("itemType").value,
    item_code: $("itemCode").value.trim(),
    category: ($("itemCategoryNew").value.trim() || $("itemCategory").value || "").trim(),
    sale_price_paisa: rsToPaisa($("itemSale").value),
    purchase_price_paisa: rsToPaisa($("itemPurchase").value),
    wholesale_price_paisa: rsToPaisa($("itemWholesale").value),
    min_wholesale_qty: parseInt($("itemMinWholesaleQty").value || "0", 10) || 0,
    mrp_paisa: rsToPaisa($("itemMrp").value),
    tax_bp: Math.round((parseFloat($("itemTaxPct").value || "0") || 0) * 100),
    discount_type: $("itemDiscountType").value,
    discount_bp: $("itemDiscountType").value === "amount" ? 0
      : Math.round((parseFloat($("itemDiscountPct").value || "0") || 0) * 100),
    discount_amount_paisa: $("itemDiscountType").value === "amount"
      ? rsToPaisa($("itemDiscountPct").value) : 0,
    stock_qty: stockQty,
    min_stock: parseInt($("itemMinStock").value || "0", 10) || 0,
    location: $("itemLocation").value.trim(),
    description: $("itemDescription").value.trim(),
    unit: $("itemUnit").value,
    secondary_unit: $("itemSecondaryUnit").value,
    conversion: parseFloat($("itemConversion").value || "0") || 0,
    batches: batches
  };
  var done = function (msg) {
    editingItemId = null;
    $("itemForm").reset();
    $("batchRows").innerHTML = "";
    $("itemFormTitle").textContent = "Add item";
    $("itemSubmitBtn").textContent = "Add item";
    $("itemCancelEdit").classList.add("hidden");
    if (itemSaveNewFlag) { itemSaveNewFlag = false; msg += " Add the next item."; $("itemName").focus(); }
    else hidePanel("itemPanel");
    setMsg("appMsg", msg);
    loadItems();
  };
  if (editingItemId) {
    delete payload.batches;
    apiPut("/api/items/" + editingItemId, payload).then(function () { done("Item updated."); })
      .catch(function (e) { setMsg("appMsg", e.message, true); });
  } else {
    apiCreate("item", payload).then(function (r) {
      done(r.queued ? "Saved on this device — will sync when online." : "Item added.");
    }).catch(function (e) { setMsg("appMsg", e.message, true); });
  }
});

var itemSaveNewFlag = false;
$("itemSaveNew").addEventListener("click", function () {
  itemSaveNewFlag = true;
  $("itemForm").requestSubmit();
});
$("btnAddItem").addEventListener("click", function () {
  if (editingItemId) $("itemCancelEdit").click();
  showPanel("itemPanel");
  $("itemName").focus();
});
$("btnItemFormBack").addEventListener("click", function () {
  if (editingItemId) $("itemCancelEdit").click();
  hidePanel("itemPanel");
});

/* ------------------------------------------------- document form ----- */
/* One shared form drives every document type. docType picks the
   labels, the cash/credit behaviour and the payload "type". Lines use
   the existing item/qty/rate grid; the cash toggle simply sets the
   paid amount to the bill total (full payment at save). */
var docCashMode = false;

function fillPartySelects() {
  apiGet("/api/parties", "parties").then(function (r) {
    var parties = r.body.parties || [];
    [["saleParty", true], ["payParty", false]].forEach(function (pair) {
      var sel = $(pair[0]); if (!sel) return;
      var keep = sel.value;
      var meta = DOC_META[docType];
      var cashLabel = pair[1] ? meta.cashName : "— None —";
      sel.innerHTML = "";
      var first = document.createElement("option");
      first.value = ""; first.textContent = cashLabel;
      sel.appendChild(first);
      parties.forEach(function (p) {
        if (partyInactive(p) && String(p.id) !== String(keep)) return;
        var o = document.createElement("option");
        o.value = p.id;
        o.textContent = p.name + (partyInactive(p) ? " (Inactive)" : "") +
          (p.balancePaisa ? "  ·  " + fmtRs(p.balancePaisa) : "") +
          (pair[1] && p.creditLimitType === "custom" ? "  ·  Limit " + fmtRs(p.creditLimitPaisa || 0) : "");
        o.setAttribute("data-phone", p.phone || "");
        o.setAttribute("data-balance", p.balancePaisa || 0);
        o.setAttribute("data-credit", p.creditLimitType === "custom" ? (p.creditLimitPaisa || 0) : "");
        sel.appendChild(o);
      });
      sel.value = keep;
    });
    ["allocParty", "expParty", "incParty", "chqParty"].forEach(function (id) {
      var sel = $(id); if (!sel) return;
      var keep = sel.value;
      sel.innerHTML = id === "allocParty" ? '<option value="">— Choose party —</option>' : '<option value="">— None —</option>';
      parties.forEach(function (p) {
        if (partyInactive(p) && String(p.id) !== String(keep)) return;
        var o = document.createElement("option");
        o.value = p.id; o.textContent = p.name + (partyInactive(p) ? " (Inactive)" : "");
        sel.appendChild(o);
      });
      sel.value = keep;
    });
    updateDocPartyInfo();
    updateLoyaltyInfo();
  }).catch(function () {});
}
/* Vyapar-style party picker (delta build #3): the button shows the
   chosen party; the menu lists "+ Add Party" first, then every party
   with its current balance right-aligned and a tick on the selected
   one. The hidden #saleParty select stays the single source of truth
   for the value, so every existing reader keeps working. */
function renderDocPartyMenu() {
  var sel = $("saleParty");
  var list = $("docPartyList");
  if (!sel || !list) return;
  var q = ($("docPartySearch").value || "").trim().toLowerCase();
  var meta = DOC_META[docType];
  list.innerHTML = "";
  var any = false;
  Array.prototype.forEach.call(sel.options, function (o) {
    if (!o.value) return;
    var name = (o.textContent || "").split("  ·  ")[0];
    if (q && name.toLowerCase().indexOf(q) < 0) return;
    any = true;
    var bal = parseInt(o.getAttribute("data-balance") || "0", 10);
    var b = document.createElement("button");
    b.type = "button";
    b.className = "party-row" + (sel.value === o.value ? " sel" : "");
    b.innerHTML = "<span>" + esc(name) + (sel.value === o.value ? " ✓" : "") +
      '</span><span class="p-bal">' + fmtRs(bal) + "</span>";
    b.addEventListener("click", function () {
      sel.value = o.value;
      sel.dispatchEvent(new Event("change"));
      hidePanel("docPartyMenu");
    });
    list.appendChild(b);
  });
  if (!any) {
    var cash = document.createElement("button");
    cash.type = "button";
    cash.className = "party-row" + (!sel.value ? " sel" : "");
    cash.innerHTML = "<span>" + esc(meta.cashName) + (!sel.value ? " ✓" : "") +
      '</span><span class="p-bal">' + fmtRs(0) + "</span>";
    cash.addEventListener("click", function () {
      sel.value = "";
      sel.dispatchEvent(new Event("change"));
      hidePanel("docPartyMenu");
    });
    list.appendChild(cash);
  }
  syncDocPartyBtn();
}
function syncDocPartyBtn() {
  var sel = $("saleParty");
  var btn = $("docPartyBtnText");
  if (!sel || !btn) return;
  var o = sel.options[sel.selectedIndex];
  btn.textContent = o && o.value
    ? (o.textContent || "").split("  ·  ")[0]
    : (o ? o.textContent : DOC_META[docType].cashName);
}
function updateDocPartyInfo() {
  var sel = $("saleParty");
  if (!sel) return;
  var o = sel.options[sel.selectedIndex];
  if (o && o.value) {
    $("docPhone").value = o.getAttribute("data-phone") || "";
    var bal = parseInt(o.getAttribute("data-balance") || "0", 10);
    var credit = parseInt(o.getAttribute("data-credit") || "0", 10);
    $("docPartyBalance").textContent = (bal === 0 ? "Party balance: nil" :
      (bal > 0 ? "You'll receive " + fmtRs(bal) + " from this party."
               : "You'll pay " + fmtRs(-bal) + " to this party.")) +
      (credit ? " Credit limit: " + fmtRs(credit) + "." : "");
  } else {
    $("docPhone").value = "";
    $("docPartyBalance").textContent = "";
  }
  renderDocPartyMenu();
  syncDocBilling();
}
/* Billing Address on the bill form (audit 2026-11-10, research §1):
   the box sits directly under Customer and appears once a party is
   chosen, prefilled with the party's saved Billing Address; a manual
   edit is never overwritten by a later party change. */
var docBillingAuto = "";
function syncDocBilling() {
  var wrap = $("docBillingWrap"), box = $("docBilling");
  if (!wrap || !box) return;
  var pid = $("saleParty").value;
  var party = null;
  ((cacheGet("parties") || {}).parties || []).forEach(function (x) {
    if (String(x.id) === String(pid)) party = x;
  });
  if (party) {
    var addr = party.billingAddress || "";
    if (!box.value.trim() || box.value === docBillingAuto) {
      box.value = addr;
      docBillingAuto = addr;
    }
  }
  wrap.classList.toggle("hidden", !(pid || box.value.trim()));
}
$("docBilling").addEventListener("input", function () {
  if ($("docBilling").value !== docBillingAuto) docBillingAuto = "\u0001manual";
});
$("saleParty").addEventListener("change", updateDocPartyInfo);
$("docPartyBtn").addEventListener("click", function () {
  var menu = $("docPartyMenu");
  var opening = menu.classList.contains("hidden");
  menu.classList.toggle("hidden");
  if (opening) { renderDocPartyMenu(); $("docPartySearch").focus(); }
});
$("docPartySearch").addEventListener("input", renderDocPartyMenu);
document.addEventListener("click", function (ev) {
  var picker = $("docPartyPicker");
  if (picker && !picker.contains(ev.target)) hidePanel("docPartyMenu");
  var sw = document.querySelector(".share-wrap");
  if (sw && !sw.contains(ev.target)) hidePanel("docShareMenu");
  document.querySelectorAll(".sl-picker-menu").forEach(function (m) {
    if (!m.contains(ev.target)) m.classList.add("hidden");
  });
});
$("btnDocAddParty").addEventListener("click", function () {
  hidePanel("docPartyMenu");
  $("docAddParty").classList.toggle("hidden");
});
$("btnDocPartySave").addEventListener("click", function () {
  var name = $("docNewPartyName").value.trim();
  if (!name) { setMsg("appMsg", "Enter the party name.", true); return; }
  apiCreate("party", {
    name: name, phone: $("docNewPartyPhone").value.trim(), opening_balance_paisa: 0
  }).then(function () {
    $("docNewPartyName").value = ""; $("docNewPartyPhone").value = "";
    $("docAddParty").classList.add("hidden");
    setMsg("appMsg", "Party added — pick it from the list above.");
    fillPartySelects();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});

var editingInvoiceId = null;
var lastSavedInvoice = null;

function itemById(id) {
  return (((cacheGet("items") || {}).items) || []).filter(function (i) { return i.id === id; })[0] || null;
}

/* The bill line's item id lives on a hidden input (.sl-item) — the
   single value source every reader uses; the visible cell is a
   type-to-search text input (Vyapar behaviour, Qudoos phone check
   2026-11-10). fillLineItemOptions is kept only as a guarded no-op
   for any legacy SELECT caller; hidden inputs need no options. */
function fillLineItemOptions(sel) {
  /* No-op: the item cell is a text input + hidden id input now;
     there are no per-line <select> options left to rebuild. */
  return;
}
/* Bill-line TAX is a dropdown (Vyapar): resting label "Select", then
   "None", then the rates the account actually uses — the Settings
   default rate plus every rate items already carry. Choosing a rate
   sets the line's tax %; the TAX AMOUNT column stays computed. */
function fmtTaxPct(v) {
  return String(Math.round(v * 100) / 100);
}
function lineTaxRates() {
  var s = QB.settings || {};
  var seen = {};
  if (s.default_tax_bp) seen[s.default_tax_bp / 100] = true;
  (((cacheGet("items") || {}).items) || []).forEach(function (i) {
    if (i.taxBp) seen[i.taxBp / 100] = true;
  });
  return Object.keys(seen).map(Number).sort(function (a, b) { return a - b; });
}
function ensureTaxOption(selEl, pct) {
  var v = String(pct);
  var found = false;
  [].forEach.call(selEl.options, function (o) { if (o.value === v) found = true; });
  if (!found) {
    var o = document.createElement("option");
    o.value = v; o.textContent = fmtTaxPct(pct) + "%";
    selEl.appendChild(o);
  }
}
function buildLineTax(selEl, initialPct) {
  selEl.innerHTML = "";
  var o0 = document.createElement("option");
  o0.value = ""; o0.textContent = "Select";
  selEl.appendChild(o0);
  var o1 = document.createElement("option");
  o1.value = "0"; o1.textContent = "None";
  selEl.appendChild(o1);
  lineTaxRates().forEach(function (pct) {
    var o = document.createElement("option");
    o.value = String(pct); o.textContent = fmtTaxPct(pct) + "%";
    selEl.appendChild(o);
  });
  if (initialPct != null && initialPct !== "" && Number(initialPct) !== 0) {
    ensureTaxOption(selEl, Number(initialPct));
  }
  selEl.value = (initialPct == null ? "" : String(initialPct));
}
function setLineTaxPct(w, pct) {
  var selEl = w.querySelector(".sl-tax");
  if (!selEl || selEl.tagName !== "SELECT") return;
  ensureTaxOption(selEl, pct);
  selEl.value = String(pct);
}
function addSaleLine(itemId, preset) {
  var s = QB.settings || {};
  var wrap = document.createElement("div");
  wrap.className = "sale-line";
  var html =
    '<span class="sl-num"></span>' +
    '<span class="sl-item-cell"><input class="sl-item" type="hidden">' +
    '<span class="party-picker sl-picker">' +
      '<input class="sl-item-name" type="text" placeholder="Type item name" autocomplete="off">' +
      '<span class="party-picker-menu sl-picker-menu hidden">' +
        '<button type="button" class="party-picker-add sl-add-item">+ Add Item</button>' +
        '<span class="sl-quickadd hidden">' +
          '<input class="sl-qa-name" type="text" placeholder="New item name">' +
          '<input class="sl-qa-sale" type="number" step="0.01" min="0" placeholder="Sale price (Rs)">' +
          '<input class="sl-qa-purchase" type="number" step="0.01" min="0" placeholder="Purchase price (Rs)">' +
          '<input class="sl-qa-stock" type="number" step="1" min="0" placeholder="Opening stock">' +
          '<button type="button" class="btn btn-mini btn-primary sl-qa-save">Add Item</button>' +
        '</span>' +
        '<span class="sl-picker-head"><span>ITEM</span><span>SALE PRICE</span><span>PURCHASE PRICE</span><span>STOCK</span><span>LOCATION</span></span>' +
        '<span class="sl-picker-list"></span>' +
      '</span>' +
    '</span></span>' +
    '<span class="sl-qty-cell"><input class="sl-qty" type="number" step="1" min="1" value="1">' +
      '<input class="sl-free" type="number" step="1" min="0" value="0" title="Free quantity" placeholder="+ Free"></span>' +
    '<span class="sl-unit-cell"><select class="sl-unit-sel"></select></span>' +
    '<span class="sl-rate-cell"><input class="sl-rate" type="number" step="0.01" min="0" value="0"></span>' +
    '<span class="sl-disc-cell"><input class="sl-disc" type="number" step="0.01" min="0" value="0"><span class="sl-disc-amt">Rs 0.00</span></span>' +
    '<span class="sl-tax-cell"><select class="sl-tax"></select><span class="sl-tax-amt">Rs 0.00</span></span>' +
    '<span class="sl-amt">Rs 0.00</span>' +
    '<button class="rm" type="button" title="Remove line">×</button>' +
    /* Per-line Description / Batch No. / Serial No. / Exp inputs were
       removed from the bill form on Qudoos's phone-check order
       (2026-11-10): the form no longer shows or collects them. The
       server still accepts the fields (defaulting empty) and the
       item-side batch features are untouched. */
    '<div class="sl-sub">' +
      '<span class="sl-lpp"></span>' +
    "</div>";
  wrap.innerHTML = html;
  var unitSel = wrap.querySelector(".sl-unit-sel");
  (itemUnits() || []).forEach(function (u) {
    var nm = typeof u === "string" ? u : (u && u.name);
    if (!nm) return;
    var oo = document.createElement("option");
    oo.value = nm; oo.textContent = nm;
    unitSel.appendChild(oo);
  });
  if (!unitSel.options.length) {
    var po = document.createElement("option");
    po.value = "Pcs"; po.textContent = "Pcs";
    unitSel.appendChild(po);
  }
  var sel = wrap.querySelector(".sl-item");
  buildLineTax(wrap.querySelector(".sl-tax"),
    (s.tax_enabled && s.default_tax_bp) ? (s.default_tax_bp / 100) : "");
  /* Type-to-search item cell (Qudoos phone check 2026-11-10): the
     shopkeeper types the item name himself; the suggestion dropdown
     below lists the MATCHING items with Vyapar's columns (ITEM /
     SALE PRICE / PURCHASE PRICE / STOCK / LOCATION) under a
     "+ Add Item" header row. Picking a row fills the line exactly
     as before; typing without picking leaves a free-text line (the
     typed name is saved as the line description — no forced
     selection, no special resting label). The menu is
     fixed-positioned because the bill grid clips overflow. */
  var nameInput = wrap.querySelector(".sl-item-name");
  var pickerMenu = wrap.querySelector(".sl-picker-menu");
  function buildItemMenu() {
    var list = wrap.querySelector(".sl-picker-list");
    list.innerHTML = "";
    var items = ((cacheGet("items") || {}).items || []);
    var q = nameInput.value.trim().toLowerCase();
    var matches = items.filter(function (i) {
      return !q || String(i.name || "").toLowerCase().indexOf(q) >= 0;
    });
    if (!matches.length) {
      list.innerHTML = '<p class="empty" style="padding:6px">' +
        (items.length ? "No matching items — use + Add Item above."
                      : "No items yet — use + Add Item above.") + "</p>";
      return;
    }
    matches.forEach(function (i) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "sl-item-row" + (String(sel.value) === String(i.id) ? " sel" : "");
      b.innerHTML = "<span>" + esc(i.name) + (String(sel.value) === String(i.id) ? " ✓" : "") + "</span>" +
        "<span>" + fmtRs(i.salePricePaisa) + "</span>" +
        "<span>" + fmtRs(i.purchasePricePaisa || 0) + "</span>" +
        "<span>" + (i.stockQty != null ? i.stockQty : 0) + "</span>" +
        "<span>" + esc(i.location || "—") + "</span>";
      b.addEventListener("click", function () {
        nameInput.value = i.name;
        sel.value = i.id;
        sel.dispatchEvent(new Event("change"));
        pickerMenu.classList.add("hidden");
      });
      list.appendChild(b);
    });
  }
  function showItemMenu() {
    document.querySelectorAll(".sl-picker-menu").forEach(function (m) {
      if (m !== pickerMenu) m.classList.add("hidden");
    });
    wrap.querySelector(".sl-add-item").classList.toggle("hidden",
      !!(QB.settings && QB.settings.block_new_from_txn));
    buildItemMenu();
    var r = nameInput.getBoundingClientRect();
    pickerMenu.style.top = (r.bottom + 6) + "px";
    pickerMenu.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 552)) + "px";
    pickerMenu.classList.remove("hidden");
  }
  nameInput.addEventListener("click", function (ev) {
    ev.stopPropagation();
    if (pickerMenu.classList.contains("hidden")) showItemMenu();
  });
  nameInput.addEventListener("focus", function () { showItemMenu(); });
  nameInput.addEventListener("input", function () {
    /* Editing the text away from the chosen item's name turns the
       line back into free text: clear the id silently (rate, unit
       and tax the shopkeeper already sees stay untouched). */
    var chosen = itemById(parseInt(sel.value, 10) || 0);
    if (chosen && nameInput.value !== chosen.name) sel.value = "";
    if (pickerMenu.classList.contains("hidden")) showItemMenu();
    else buildItemMenu();
  });
  nameInput.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") pickerMenu.classList.add("hidden");
  });
  pickerMenu.addEventListener("click", function (ev) { ev.stopPropagation(); });
  wrap.querySelector(".sl-add-item").addEventListener("click", function () {
    wrap.querySelector(".sl-quickadd").classList.toggle("hidden");
    wrap.querySelector(".sl-qa-name").focus();
  });
  wrap.querySelector(".sl-qa-save").addEventListener("click", function () {
    var name = wrap.querySelector(".sl-qa-name").value.trim();
    if (!name) { setMsg("appMsg", "Enter the new item's name.", true); return; }
    apiCreate("item", {
      name: name,
      sale_price_paisa: rsToPaisa(wrap.querySelector(".sl-qa-sale").value),
      purchase_price_paisa: rsToPaisa(wrap.querySelector(".sl-qa-purchase").value),
      stock_qty: parseInt(wrap.querySelector(".sl-qa-stock").value || "0", 10) || 0
    }).then(function (r) {
      var newId = r.body && r.body.item ? r.body.item.id : null;
      return apiGet("/api/items", "items").then(function () {
        if (newId) {
          nameInput.value = name;
          sel.value = newId;
          sel.dispatchEvent(new Event("change"));
        }
        wrap.querySelector(".sl-quickadd").classList.add("hidden");
        pickerMenu.classList.add("hidden");
        setMsg("appMsg", "Item added.");
      });
    }).catch(function (e) { setMsg("appMsg", e.message, true); });
  });
  wrap.querySelector(".sl-free").classList.toggle("hidden", !s.txn_free_item_qty);
  wrap.querySelector(".sl-disc-cell").classList.toggle("hidden",
    s.item_wise_discount === false || s.txn_wise_discount === false);
  wrap.querySelector(".sl-tax-cell").classList.toggle("hidden",
    !(s.tax_enabled && s.item_wise_tax !== false && s.txn_wise_tax !== false));
  function fillFromItem() {
    var it = itemById(parseInt(sel.value, 10) || 0);
    if (it) {
      nameInput.value = it.name;
      wrap.querySelector(".sl-rate").value = (it.salePricePaisa / 100).toFixed(2);
    }
    var unitName = it && it.unit ? it.unit : (s.default_unit || "Pcs");
    var us = wrap.querySelector(".sl-unit-sel");
    if (![].some.call(us.options, function (x) { return x.value === unitName; })) {
      var uo = document.createElement("option");
      uo.value = unitName; uo.textContent = unitName;
      us.appendChild(uo);
    }
    us.value = unitName;
    if (it) {
      var rateNow = rsToPaisa(wrap.querySelector(".sl-rate").value);
      if (it.discountType === "amount" && it.discountAmountPaisa && rateNow) {
        wrap.querySelector(".sl-disc").value = (it.discountAmountPaisa / rateNow * 100).toFixed(2);
      } else if (it.discountBp) {
        wrap.querySelector(".sl-disc").value = it.discountBp / 100;
      }
      if (it.taxBp != null && s.tax_enabled) setLineTaxPct(wrap, it.taxBp / 100);
      var pid = $("saleParty").value;
      if (s.party_wise_item_rate && pid) {
        apiGet("/api/parties/" + pid + "/item-rate/" + it.id, null).then(function (r) {
          if (r.body.ratePaisa != null) wrap.querySelector(".sl-rate").value = (r.body.ratePaisa / 100).toFixed(2);
          calcSaleTotal();
        }).catch(function () {});
      }
      /* Settings > TRANSACTION > "Show last 5 Purchase Price of Items"
         (audit 2026-11-10: the toggle was a dead switch — nothing
         honoured it). Shows the item's recent purchase rates here. */
      var lpp = wrap.querySelector(".sl-lpp");
      if (lpp) {
        lpp.textContent = "";
        if (s.show_last_purchase_price) {
          apiGet("/api/reports/item-detail?item_id=" + it.id, null).then(function (r2) {
            var rates = [];
            (r2.body.rows || []).forEach(function (row) {
              if (row.kind !== "purchase") return;
              var rate = row.ratePaisa ||
                (row.qty ? Math.round((row.amountPaisa || 0) / row.qty) : 0);
              if (rate) rates.push(rate);
            });
            rates = rates.slice(-5).reverse().filter(function (v, ix, a) {
              return ix === 0 || v !== a[ix - 1];
            });
            if (rates.length && String(sel.value) === String(it.id)) {
              lpp.textContent = "Last purchase prices: " + rates.map(fmtRs).join(" · ");
            }
          }).catch(function () {});
        }
      }
    }
    calcSaleTotal();
  }
  sel.addEventListener("change", fillFromItem);
  wrap.querySelector(".rm").addEventListener("click", function () { wrap.remove(); calcSaleTotal(); });
  ["sl-qty", "sl-free", "sl-rate", "sl-disc", "sl-tax"].forEach(function (cn) {
    wrap.querySelector("." + cn).addEventListener("input", calcSaleTotal);
  });
  wrap.querySelector(".sl-tax").addEventListener("change", calcSaleTotal);
  wrap.querySelector(".sl-rate").addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && QB.settings && QB.settings.txn_quick_entry) {
      ev.preventDefault(); addSaleLine();
    }
  });
  if (preset) {
    if (preset.qty != null) wrap.querySelector(".sl-qty").value = preset.qty;
    if (preset.freeQty != null) wrap.querySelector(".sl-free").value = preset.freeQty;
    if (preset.ratePaisa != null) wrap.querySelector(".sl-rate").value = (preset.ratePaisa / 100).toFixed(2);
    if (preset.discountPaisa && preset.qty && preset.ratePaisa) {
      var gross = preset.qty * preset.ratePaisa;
      wrap.querySelector(".sl-disc").value = gross ? (preset.discountPaisa / gross * 100).toFixed(2) : 0;
    }
    if (preset.taxPaisa && preset.qty && preset.ratePaisa) {
      var net2 = preset.qty * preset.ratePaisa - (preset.discountPaisa || 0);
      if (net2) setLineTaxPct(wrap, Math.round(preset.taxPaisa / net2 * 10000) / 100);
    }
    if (preset.description) nameInput.value = preset.description;
    /* preset.batchNo / serialNo / expDate are ignored: the bill form
       no longer collects per-line values for them (Qudoos
       phone-check order, 2026-11-10). preset.description is the
       line's name: for item lines the server stored the item name;
       for free-text lines it is the name the shopkeeper typed. */
  }
  if (itemId) { sel.value = itemId; fillFromItem(); }
  $("saleLines").appendChild(wrap);
  calcSaleTotal();
  return wrap;
}
/* One line's money, honouring Settings > TRANSACTION: with
   "Transaction wise Discount" OFF the line discount is the item's own
   default (Disc. On Sale Price); with "Transaction wise Tax" OFF the
   line tax is 0 — exactly what the server computes. */
function lineAmounts(w) {
  var s = QB.settings || {};
  var itemId = w.querySelector(".sl-item").value;
  var it = itemId ? itemById(parseInt(itemId, 10) || 0) : null;
  var qty = parseInt(w.querySelector(".sl-qty").value || "0", 10);
  var rate = rsToPaisa(w.querySelector(".sl-rate").value);
  var gross = qty * rate;
  var disc, tax;
  if (s.txn_wise_discount === false) {
    if (!it) disc = 0;
    else if (it.discountType === "amount") disc = (it.discountAmountPaisa || 0) * qty;
    else disc = Math.round(gross * (it.discountBp || 0) / 10000);
  } else {
    disc = Math.round(gross * (parseFloat(w.querySelector(".sl-disc").value || "0") || 0) / 100);
  }
  if (s.txn_wise_tax === false) tax = 0;
  else tax = Math.round((gross - disc) * (parseFloat(w.querySelector(".sl-tax").value || "0") || 0) / 100);
  return { qty: qty, rate: rate, gross: gross, disc: disc, tax: tax };
}
function saleLinesData() {
  var lines = [];
  document.querySelectorAll(".sale-line").forEach(function (w) {
    var itemId = w.querySelector(".sl-item").value;
    var a = lineAmounts(w);
    var qty = a.qty, rate = a.rate, disc = a.disc, tax = a.tax;
    var line = {
      qty: qty,
      free_qty: parseInt(w.querySelector(".sl-free").value || "0", 10),
      rate_paisa: rate,
      discount_paisa: disc,
      tax_paisa: tax
    };
    if (itemId) line.item_id = parseInt(itemId, 10);
    else {
      /* Free-text line: the typed item name IS the line's
         description (the server requires a description or an item).
         Batch/serial/exp are still not collected. */
      var typedName = (w.querySelector(".sl-item-name").value || "").trim();
      if (typedName) line.description = typedName;
    }
    if (line.qty > 0) lines.push(line);
  });
  return lines;
}
function docTotals() {
  var sub = 0, disc = 0, tax = 0;
  saleLinesData().forEach(function (l) {
    sub += l.qty * l.rate_paisa;
    disc += l.discount_paisa || 0;
    tax += l.tax_paisa || 0;
  });
  var total = Math.max(sub - disc + tax, 0);
  var mode = (QB.settings || {}).round_off_mode;
  var rounded = mode === "one" ? Math.ceil(total / 100) * 100 : Math.round(total / 100) * 100;
  return { sub: sub, disc: disc, tax: tax, total: total, rounded: rounded };
}
function calcSaleTotal() {
  var qtySum = 0;
  document.querySelectorAll(".sale-line").forEach(function (w, idx) {
    var a = lineAmounts(w);
    var num = w.querySelector(".sl-num");
    if (num) num.textContent = idx + 1;
    w.querySelector(".sl-amt").textContent = fmtRs(a.gross - a.disc + a.tax);
    var da = w.querySelector(".sl-disc-amt");
    if (da) da.textContent = fmtRs(a.disc);
    var ta = w.querySelector(".sl-tax-amt");
    if (ta) ta.textContent = fmtRs(a.tax);
    qtySum += a.qty;
  });
  var t = docTotals();
  $("docTotalQty").textContent = qtySum;
  $("docTotalDisc").textContent = fmtRs(t.disc);
  $("docTotalTax").textContent = fmtRs(t.tax);
  $("docTotalAmt").textContent = fmtRs(t.total);
  var shown = $("docRoundOff").checked ? t.rounded : t.total;
  var loyUse = parseInt($("docLoyaltyUse").value || "0", 10) || 0;
  if (docType === "sale" && QB.settings && QB.settings.loyalty && QB.settings.loyalty.enabled && loyUse > 0) {
    shown = Math.max(shown - loyUse * Math.round((QB.settings.loyalty.redeem_value_paisa || 100)), 0);
  }
  $("saleTotal").textContent = fmtRs(shown);
  if (docType === "sale" && QB.settings && QB.settings.show_profit_on_sale) {
    var cost = 0;
    document.querySelectorAll(".sale-line").forEach(function (w) {
      var it = itemById(parseInt(w.querySelector(".sl-item").value, 10) || 0);
      if (it) cost += (parseInt(w.querySelector(".sl-qty").value || "0", 10) || 0) * (it.purchasePricePaisa || 0);
    });
    $("docProfitHint").classList.remove("hidden");
    $("docProfitHint").textContent = "Profit on this bill (approx): " + fmtRs(t.total - cost);
  } else {
    $("docProfitHint").classList.add("hidden");
  }
  updateLoyaltyInfo();
  if (docCashMode) $("salePaid").value = (shown / 100).toFixed(2);
  var payRowsNow = docPaymentsData();
  if (payRowsNow.length) {
    var paySum = 0;
    payRowsNow.forEach(function (p) { paySum += p.amount_paisa; });
    $("docPayTotal").textContent = "Total payment: " + fmtRs(paySum) + " / " + fmtRs(shown);
  }
}
/* ---- Split payments on the bill form (delta #3) ---- */
var docBankAccounts = [];
function refreshDocBankAccounts() {
  apiGet("/api/bank-accounts", "bankaccounts").then(function (r) {
    docBankAccounts = r.body.bankAccounts || [];
  }).catch(function () {});
}
function payTypeOptions(sel) {
  var keep = sel.value;
  sel.innerHTML = '<option value="cash">Cash</option><option value="cheque">Cheque</option>' +
    docBankAccounts.map(function (a) {
      return '<option value="bank:' + a.id + '">' + esc(a.displayName) + "</option>";
    }).join("");
  if (keep) sel.value = keep;
}
function addDocPayRow() {
  var row = document.createElement("div");
  row.className = "pay-row";
  row.innerHTML = '<select class="pay-type"></select>' +
    '<input class="pay-amt" type="number" step="0.01" min="0" value="0" placeholder="Amount (Rs)">' +
    '<input class="pay-ref hidden" type="text" placeholder="Reference No.">' +
    '<button class="rm" type="button" title="Remove payment">×</button>';
  var sel = row.querySelector(".pay-type");
  payTypeOptions(sel);
  var syncRef = function () {
    row.querySelector(".pay-ref").classList.toggle("hidden", sel.value === "cash");
    row.querySelector(".pay-ref").placeholder = sel.value === "cheque" ? "Cheque No." : "Reference No.";
  };
  sel.addEventListener("change", syncRef);
  row.querySelector(".pay-amt").addEventListener("input", calcSaleTotal);
  row.querySelector(".rm").addEventListener("click", function () { row.remove(); calcSaleTotal(); });
  syncRef();
  $("docPayRows").appendChild(row);
  return row;
}
function docPaymentsData() {
  var rows = [];
  document.querySelectorAll("#docPayRows .pay-row").forEach(function (row) {
    var amt = rsToPaisa(row.querySelector(".pay-amt").value);
    if (amt <= 0) return;
    var v = row.querySelector(".pay-type").value;
    var ref = row.querySelector(".pay-ref").value.trim();
    if (v === "cash") rows.push({ mode: "cash", amount_paisa: amt });
    else if (v === "cheque") rows.push({ mode: "cheque", amount_paisa: amt, cheque_no: ref });
    else if (v.indexOf("bank:") === 0) rows.push({ mode: "bank", bank_account_id: parseInt(v.slice(5), 10), amount_paisa: amt, reference_no: ref });
  });
  return rows;
}
$("btnAddPayRow").addEventListener("click", function () { addDocPayRow(); });

function applyDocType() {
  var meta = DOC_META[docType];
  $("docTitle").textContent = DOC_TAB_SHORT[docType] || meta.title;
  $("docPartyLabel").textContent = meta.party;
  $("docPaidLabel").textContent = meta.returnsDoc ? "Refund (Rs)" : "Paid now (Rs)";
  $("docToggleWrap").classList.toggle("hidden", meta.noPayment);
  $("docPaidWrap").classList.toggle("hidden", meta.noPayment);
  $("docPayBlock").classList.toggle("hidden", !!meta.noPayment);
  refreshDocBankAccounts();
  $("docSaveBtn").textContent = editingInvoiceId ? "Save changes" : "Save";
  $("docDateInput").value = new Date().toISOString().slice(0, 10);
  $("docNumber").textContent = editingInvoiceId ? "Editing" : "Auto";
  syncDocPartyBtn();
  docCashMode = false;
  $("docCashToggle").classList.remove("on");
  $("docCashToggle").setAttribute("aria-checked", "false");
  $("salePaid").removeAttribute("readonly");
  var s = QB.settings || {};
  $("docBarcodeRow").classList.toggle("hidden", !s.barcode_scan);
  $("docDueWrap").classList.toggle("hidden", !s.due_dates_payment_terms);
  $("docTermsWrap").classList.toggle("hidden", !s.due_dates_payment_terms);
  $("docPoWrap").classList.toggle("hidden", !s.customers_po_details);
  $("docRepeatWrap").classList.toggle("hidden", !s.repeat_invoices);
  $("docLoyaltyBlock").classList.toggle("hidden", !(docType === "sale" && s.loyalty && s.loyalty.enabled));
  $("docShippingWrap").classList.toggle("hidden", s.party_shipping_address === false);
  $("btnDocAddParty").classList.toggle("hidden", !!s.block_new_from_txn);
  if (s.txn_cash_sale_default && !meta.noPayment && !editingInvoiceId) {
    // Settings > TRANSACTION > Cash Sale by default.
    docCashMode = true;
    $("docCashToggle").classList.add("on");
    $("docCashToggle").setAttribute("aria-checked", "true");
    $("salePaid").toggleAttribute("readonly", true);
  }
  fillPartySelects();
  loadTermsPicker();
  calcSaleTotal();
}
/* ---- Document form tabs (delta #16) ----
   Client-side only: each tab keeps its own unsaved form state in
   memory; switching preserves it, closing a dirty tab asks first.
   No API changes — a tab is saved through the normal form submit. */
var docTabs = [];
var activeDocTab = null;
var docTabSeq = 0;
var docSaveNewFlag = false;
var DOC_TAB_SHORT = { sale: "Sale", purchase: "Purchase", estimate: "Estimate",
  sale_order: "Sale Order", purchase_order: "Purchase Order", challan: "Challan",
  proforma: "Proforma", sale_return: "Sale Return", purchase_return: "Purchase Return" };
function captureDocState() {
  return {
    type: docType, editingId: editingInvoiceId,
    party: $("saleParty").value, phone: $("docPhone").value,
    paid: $("salePaid").value, roundOff: $("docRoundOff").checked,
    cashMode: docCashMode, termsId: docTermsSelected,
    moreOpen: !$("docMoreBody").classList.contains("hidden"),
    termsOpen: !$("docTermsBody").classList.contains("hidden"),
    due: $("docDueDate").value, terms: $("docPayTerms").value,
    billing: $("docBilling").value, shipping: $("docShipping").value,
    po: $("docPoNumber").value, transport: $("docTransport").value,
    vehicle: $("docVehicle").value, delDate: $("docDeliveryDate").value,
    delLoc: $("docDeliveryLoc").value, desc: $("docDescription").value,
    loyalty: $("docLoyaltyUse").value, repeat: $("docRepeat").value,
    lines: Array.prototype.map.call(document.querySelectorAll(".sale-line"), function (w) {
      return {
        itemId: w.querySelector(".sl-item").value,
        name: w.querySelector(".sl-item-name").value,
        qty: w.querySelector(".sl-qty").value, free: w.querySelector(".sl-free").value,
        rate: w.querySelector(".sl-rate").value, disc: w.querySelector(".sl-disc").value,
        tax: w.querySelector(".sl-tax").value
      };
    }),
    payRows: Array.prototype.map.call(document.querySelectorAll("#docPayRows .pay-row"), function (row) {
      return { type: row.querySelector(".pay-type").value,
               amt: row.querySelector(".pay-amt").value,
               ref: row.querySelector(".pay-ref").value };
    })
  };
}
function restoreDocState(st) {
  docType = st.type;
  editingInvoiceId = st.editingId;
  docTermsSelected = st.termsId;
  $("saleLines").innerHTML = "";
  st.lines.forEach(function (L) {
    var w = addSaleLine();
    w.querySelector(".sl-item").value = L.itemId;
    w.querySelector(".sl-item-name").value = L.name ||
      ((itemById(parseInt(L.itemId, 10) || 0) || {}).name || "");
    w.querySelector(".sl-qty").value = L.qty;
    w.querySelector(".sl-free").value = L.free;
    w.querySelector(".sl-rate").value = L.rate;
    w.querySelector(".sl-disc").value = L.disc;
    setLineTaxPct(w, parseFloat(L.tax) || 0);
  });
  if (!st.lines.length) addSaleLine();
  $("docPayRows").innerHTML = "";
  (st.payRows || []).forEach(function (p) {
    var row = addDocPayRow();
    row.querySelector(".pay-type").value = p.type;
    row.querySelector(".pay-amt").value = p.amt;
    row.querySelector(".pay-ref").value = p.ref;
    row.querySelector(".pay-ref").classList.toggle("hidden", p.type === "cash");
  });
  $("saleParty").value = st.party;
  $("docDueDate").value = st.due; $("docPayTerms").value = st.terms;
  $("docBilling").value = st.billing; $("docShipping").value = st.shipping;
  $("docPoNumber").value = st.po; $("docTransport").value = st.transport;
  $("docVehicle").value = st.vehicle; $("docDeliveryDate").value = st.delDate;
  $("docDeliveryLoc").value = st.delLoc; $("docDescription").value = st.desc;
  $("docLoyaltyUse").value = st.loyalty; $("docRepeat").value = st.repeat;
  $("docRoundOff").checked = !!st.roundOff;
  applyDocType();
  /* Collapsible sections: restore the tab's own open state, and
     never leave saved values hidden — a section holding any value
     starts expanded. */
  docSectionSet("more", !!st.moreOpen || docMoreHasContent());
  docSectionSet("terms", !!st.termsOpen || !!docTermsSelected);
  docCashMode = !!st.cashMode;
  $("docCashToggle").classList.toggle("on", docCashMode);
  $("docCashToggle").setAttribute("aria-checked", docCashMode ? "true" : "false");
  $("salePaid").toggleAttribute("readonly", docCashMode);
  $("saleParty").value = st.party;
  $("salePaid").value = st.paid;
  updateDocPartyInfo();
  $("viewTitle").textContent = DOC_META[docType].title;
  calcSaleTotal();
}
function docTabLabel(t) {
  if (t.label) return t.label;
  var same = docTabs.filter(function (x) { return x.type === t.type && !x.editingId; });
  return (DOC_TAB_SHORT[t.type] || "Bill") + " #" + (same.indexOf(t) + 1 || 1);
}
function renderDocTabs() {
  var bar = $("docTabs");
  if (!bar) return;
  bar.innerHTML = "";
  docTabs.forEach(function (t) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "doc-tab" + (t === activeDocTab ? " active" : "");
    b.innerHTML = esc(docTabLabel(t)) + ' <span class="dt-close" title="Close tab">×</span>';
    b.addEventListener("click", function (ev) {
      if (ev.target.classList.contains("dt-close")) closeDocTab(t);
      else activateDocTab(t);
    });
    bar.appendChild(b);
  });
  var add = document.createElement("button");
  add.type = "button";
  add.className = "doc-tab";
  add.textContent = "+";
  add.title = "New tab";
  add.addEventListener("click", function () { newDocTab(docType); });
  bar.appendChild(add);
}
function newDocTab(type) {
  if (activeDocTab) activeDocTab.state = captureDocState();
  var tab = { id: ++docTabSeq, type: type, editingId: null, label: null, state: null, blank: null, baseline: null };
  docTabs.push(tab);
  activeDocTab = tab;
  docType = type;
  editingInvoiceId = null;
  resetDocForm();
  tab.blank = captureDocState();
  renderDocTabs();
  return tab;
}
function activateDocTab(tab) {
  if (activeDocTab === tab) { renderDocTabs(); return; }
  if (activeDocTab) activeDocTab.state = captureDocState();
  activeDocTab = tab;
  apiGet("/api/items", "items").then(function () {
    if (activeDocTab !== tab) return;
    if (tab.state) restoreDocState(tab.state);
    else {
      docType = tab.type;
      editingInvoiceId = tab.editingId;
      resetDocForm();
      tab.blank = captureDocState();
    }
    renderDocTabs();
  }).catch(function () { renderDocTabs(); });
}
function closeDocTab(t) {
  var state = t === activeDocTab ? captureDocState() : t.state;
  var base = t.baseline || (t.blank ? JSON.stringify(t.blank) : null);
  if (state && base && JSON.stringify(state) !== base &&
      !confirm("This bill has unsaved changes. Close the tab and discard them?")) return;
  docTabs = docTabs.filter(function (x) { return x !== t; });
  if (t === activeDocTab) {
    activeDocTab = null;
    if (docTabs.length) activateDocTab(docTabs[docTabs.length - 1]);
    else newDocTab(docType);
  } else {
    renderDocTabs();
  }
}
function dropActiveDocTab() {
  if (!activeDocTab) return;
  docTabs = docTabs.filter(function (t) { return t !== activeDocTab; });
  activeDocTab = null;
}
function ensureDocTab() {
  if (activeDocTab) { renderDocTabs(); return; }
  var tab = { id: ++docTabSeq, type: docType, editingId: null, label: null, state: null, blank: null, baseline: null };
  docTabs.push(tab);
  activeDocTab = tab;
  tab.blank = captureDocState();
  renderDocTabs();
}
$("docSaveNewBtn").addEventListener("click", function () {
  docSaveNewFlag = true;
  $("saleForm").requestSubmit();
});
/* Reference-layout footer (delta build #3): ADD DESCRIPTION / IMAGE /
   DOCUMENT reveal their inputs only when clicked; Share works on the
   bill being edited (or the last one saved) via the existing PDF and
   WhatsApp actions. */
$("btnDocDesc").addEventListener("click", function () {
  $("docDescWrap").classList.toggle("hidden");
});
$("btnDocImage").addEventListener("click", function () {
  $("docAttachWrap").classList.toggle("hidden");
});
$("btnDocDoc").addEventListener("click", function () {
  $("docAttachWrap").classList.toggle("hidden");
});
/* "More Details" and "ADD TERMS AND CONDITIONS" collapsibles
   (Qudoos phone-check order, 2026-11-10): each is ONE tappable row;
   its fields show only after a tap. Default = collapsed. When a
   form opens holding values inside a section (editing a bill, or
   returning to a tab), that section starts EXPANDED so no saved
   value is ever hidden from the user. */
function docSectionSet(which, open) {
  var body = $(which === "more" ? "docMoreBody" : "docTermsBody");
  var btn = $(which === "more" ? "btnDocMore" : "btnDocTerms");
  if (!body || !btn) return;
  body.classList.toggle("hidden", !open);
  btn.setAttribute("aria-expanded", open ? "true" : "false");
  var caret = btn.querySelector(".dc-caret");
  if (caret) caret.textContent = open ? "▴" : "▾";
}
function docMoreHasContent() {
  var ids = ["docShipping", "docDueDate", "docPayTerms", "docPoNumber",
             "docRepeat", "docTransport", "docVehicle", "docDeliveryDate",
             "docDeliveryLoc"];
  return ids.some(function (id) {
    var el = $(id);
    return el && String(el.value || "").trim() !== "";
  });
}
$("btnDocMore").addEventListener("click", function () {
  docSectionSet("more", $("docMoreBody").classList.contains("hidden"));
});
$("btnDocTerms").addEventListener("click", function () {
  docSectionSet("terms", $("docTermsBody").classList.contains("hidden"));
});
$("btnDocShare").addEventListener("click", function () {
  $("docShareMenu").classList.toggle("hidden");
});
function docShareTarget(cb) {
  var id = editingInvoiceId || (lastSavedInvoice && lastSavedInvoice.id);
  if (!id) {
    setMsg("appMsg", "Save this bill first — Share gives the PDF and WhatsApp options for a saved bill.", true);
    return;
  }
  apiGet("/api/invoices", "invoices").then(function (r) {
    var inv = (r.body.invoices || []).filter(function (i) { return i.id === id; })[0];
    if (inv) cb(inv);
    else setMsg("appMsg", "Save this bill first — Share gives the PDF and WhatsApp options for a saved bill.", true);
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
$("btnDocSharePdf").addEventListener("click", function () {
  hidePanel("docShareMenu");
  docShareTarget(function (inv) { downloadInvoicePdf(inv.id, false); });
});
$("btnDocShareWhats").addEventListener("click", function () {
  hidePanel("docShareMenu");
  docShareTarget(function (inv) { shareInvoiceWhatsApp(inv); });
});

function openDocForm(type) {
  type = DOC_META[type] ? type : "sale";
  navKey = type === "sale" ? "salebook" : type === "purchase" ? "purchasebook" : navKey;
  var existing = null;
  docTabs.forEach(function (t) { if (!existing && t.type === type && !t.editingId) existing = t; });
  if (existing) activateDocTab(existing);
  else newDocTab(type);
  showScreen("sale");
}
function loadSaleForm() {
  applyDocType();
  apiGet("/api/items", "items").then(function () {
    if (!editingInvoiceId && !$("saleLines").children.length) addSaleLine();
    calcSaleTotal();
  }).catch(function () { if (!$("saleLines").children.length) addSaleLine(); });
}
$("btnAddLine").addEventListener("click", function () { addSaleLine(); });
$("docRoundOff").addEventListener("change", calcSaleTotal);
$("docCashToggle").addEventListener("click", function () {
  docCashMode = !docCashMode;
  $("docCashToggle").classList.toggle("on", docCashMode);
  $("docCashToggle").setAttribute("aria-checked", docCashMode ? "true" : "false");
  $("salePaid").toggleAttribute("readonly", docCashMode);
  calcSaleTotal();
});
var lastInvoiceId = null;
function updateLoyaltyInfo() {
  var info = $("docLoyaltyInfo");
  if (!info) return;
  var pid = $("saleParty").value;
  var parties = ((cacheGet("parties") || {}).parties) || [];
  var party = parties.filter(function (p) { return String(p.id) === String(pid); })[0];
  var pts = party ? (party.loyaltyPoints || 0) : 0;
  info.textContent = party ? ("Available points: " + pts) : "Pick a party to use points.";
  $("docLoyaltyUse").max = pts;
}
$("docLoyaltyUse").addEventListener("input", calcSaleTotal);
$("saleParty").addEventListener("change", updateLoyaltyInfo);
$("saleForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  var lines = saleLinesData();
  if (!lines.length) { setMsg("appMsg", "Add at least one line.", true); return; }
  /* Insufficient stock (audit 2026-11-10, research §1): Vyapar asks
     BEFORE the sale completes — "Your stock for following item is not
     sufficient to fulfil the sale. <item> — Do you want to continue?"
     NO aborts, YES saves. With "Stop Sale on Negative Stock" ON the
     bill is refused instead (the server enforces the same rule).
     New Sale documents only; edits keep the post-save dialog. */
  if (!editingInvoiceId && docType === "sale") {
    var needByItem = {};
    lines.forEach(function (l) {
      if (!l.item_id) return;
      needByItem[l.item_id] = (needByItem[l.item_id] || 0) +
        (l.qty || 0) + (l.free_qty || 0);
    });
    var shortNames = [];
    Object.keys(needByItem).forEach(function (iid) {
      var itS = itemById(parseInt(iid, 10));
      if (itS && needByItem[iid] > (itS.stockQty || 0)) {
        shortNames.push(itS.name + " (stock " + (itS.stockQty || 0) +
          ", need " + needByItem[iid] + ")");
      }
    });
    if (shortNames.length) {
      if (QB.settings && QB.settings.stop_sale_negative_stock) {
        openDialog("Not enough stock",
          "<p>Your stock for following item is not sufficient to fulfil the sale:</p><ul><li>" +
          shortNames.map(esc).join("</li><li>") +
          "</li></ul><p>“Stop Sale on Negative Stock” is ON in Settings &gt; GENERAL, so this bill cannot be saved.</p>" +
          '<button class="btn btn-primary btn-block" id="stockStopOk" type="button">OK</button>');
        $("stockStopOk").addEventListener("click", closeDialog);
        return;
      }
      if (!confirm("Your stock for following item is not sufficient to fulfil the sale.\n" +
          shortNames.join("\n") + "\nDo you want to continue?")) return;
    }
  }
  var meta = DOC_META[docType];
  var t = docTotals();
  var tax = t.tax, disc = t.disc;
  if ($("docRoundOff").checked) {
    var delta = t.rounded - t.total;
    if (delta >= 0) tax += delta; else disc += -delta;
  }
  var loyUse = 0;
  if (docType === "sale" && QB.settings && QB.settings.loyalty && QB.settings.loyalty.enabled) {
    loyUse = parseInt($("docLoyaltyUse").value || "0", 10) || 0;
  }
  var payload = {
    type: docType,
    party_id: $("saleParty").value ? parseInt($("saleParty").value, 10) : null,
    discount_paisa: disc,
    tax_paisa: tax,
    paid_paisa: meta.noPayment ? 0 : rsToPaisa($("salePaid").value),
    loyalty_points_used: loyUse,
    lines: lines,
    due_date: $("docDueDate").value || null,
    payment_terms: $("docPayTerms").value || null,
    billing_address: $("docBilling").value.trim(),
    shipping_address: $("docShipping").value.trim(),
    po_number: $("docPoNumber").value.trim(),
    transport_name: $("docTransport").value.trim(),
    vehicle_no: $("docVehicle").value.trim(),
    delivery_date: $("docDeliveryDate").value || null,
    delivery_location: $("docDeliveryLoc").value.trim(),
    description: $("docDescription").value.trim(),
    terms_id: docTermsSelected || null,
    repeat_freq: $("docRepeat").value || null,
    attachment_name: $("docAttachment").value ? $("docAttachment").value.split("\\").pop() : ""
  };
  var payRows = meta.noPayment ? [] : docPaymentsData();
  if (payRows.length) payload.payments = payRows;
  var wasEditing = editingInvoiceId;
  function finishSave(inv, queuedFlag) {
    lastInvoiceId = inv ? inv.id : null;
    lastSavedInvoice = inv;
    var saveNew = docSaveNewFlag; docSaveNewFlag = false;
    dropActiveDocTab();
    resetDocForm();
    ensureDocTab();
    if (queuedFlag) {
      setMsg("appMsg", meta.title + " saved on this device — it will sync when you are online. PDF will be available after sync.");
      go("dashboard");
      return;
    }
    if (saveNew) {
      setMsg("appMsg", meta.title + " saved. Add the next one.");
      return;
    }
    if (payload.repeat_freq && !wasEditing) {
      showRepeatDialog(inv, payload);
    }
    openPreview(inv);
  }
  function handleResult(r) {
    if (r.queued) { finishSave(null, true); return; }
    var inv = r.body.invoice;
    var lows = r.body.lowStock || [];
    if (lows.length && QB.settings && QB.settings.low_stock_dialog !== false && !wasEditing) {
      showLowStockDialog(lows, function () { finishSave(inv, false); });
    } else {
      finishSave(inv, false);
    }
    loadDashboard();
  }
  if (wasEditing) {
    apiPut("/api/invoices/" + wasEditing, payload).then(handleResult)
      .catch(function (e) { setMsg("appMsg", e.message, true); });
  } else {
    apiCreate("invoice", payload).then(handleResult)
      .catch(function (e) { setMsg("appMsg", e.message, true); });
  }
});
function resetDocForm() {
  editingInvoiceId = null;
  docTermsSelected = null;
  $("saleLines").innerHTML = ""; addSaleLine();
  $("docPayRows").innerHTML = "";
  $("docPayTotal").textContent = "";
  $("salePaid").value = 0;
  $("docRoundOff").checked = false;
  hidePanel("docDescWrap"); hidePanel("docAttachWrap"); hidePanel("docPartyMenu");
  hidePanel("docBillingWrap"); docBillingAuto = "";
  ["docBilling", "docShipping", "docPoNumber", "docTransport", "docVehicle",
   "docDeliveryLoc", "docDescription", "docLoyaltyUse"].forEach(function (id) { $(id).value = ""; });
  $("docDueDate").value = ""; $("docDeliveryDate").value = "";
  $("docRepeat").value = "";
  /* Fresh form: both collapsible sections start closed. */
  docSectionSet("more", false);
  docSectionSet("terms", false);
  applyDocType();
}
$("btnSaleNew").addEventListener("click", function () {
  openDocForm(docType);
});
$("btnSaleBook").addEventListener("click", function () {
  if (docType === "sale") go("salebook");
  else if (docType === "purchase") go("purchasebook");
  else go({ estimate: "estimates", sale_order: "saleorders", purchase_order: "purchaseorders",
            challan: "challans", sale_return: "salereturns", purchase_return: "purchasereturns",
            proforma: "proforma" }[docType] || "dashboard");
});
function downloadInvoicePdf(id, thermal) {
  var path = "/api/invoices/" + id + "/pdf" + (thermal ? "?format=thermal" : "");
  fetch(path, { headers: authHeaders() })
    .then(function (resp) { if (!resp.ok) throw new Error("PDF failed (" + resp.status + ")"); return resp.blob(); })
    .then(function (blob) {
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = thermal ? "qbill-bill-thermal.pdf" : "qbill-invoice.pdf";
      a.click();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 4000);
    }).catch(function (e) { setMsg("appMsg", e.message, true); });
}

/* --------------------------------------------------- books & lists --- */
var bookFrom = "", bookTo = "";
function resetBookPeriod() { bookFrom = ""; bookTo = ""; $("bookFrom").value = ""; $("bookTo").value = ""; }
function inPeriod(inv) {
  var d = (inv.createdAt || "").slice(0, 10);
  if (bookFrom && d < bookFrom) return false;
  if (bookTo && d > bookTo) return false;
  return true;
}
function searchText() { return ($("searchTx").value || "").trim().toLowerCase(); }
function loadBook() {
  var meta = DOC_META[bookKind];
  $("btnBookAdd").textContent = meta.add;
  $("btnBookAdd").className = "btn " + (bookKind === "sale" ? "btn-addsale" : "btn-addpurchase");
  apiGet("/api/invoices", "invoices").then(function (r) {
    var partyNames = {};
    ((cacheGet("parties") || {}).parties || []).forEach(function (p) { partyNames[p.id] = p.name; });
    var q = searchText();
    var list = (r.body.invoices || []).filter(function (i) {
      if (i.type !== bookKind || !inPeriod(i)) return false;
      if (q && ((partyNames[i.partyId] || "") + " " + i.invoiceNo).toLowerCase().indexOf(q) < 0) return false;
      return true;
    }).slice().reverse();
    /* Book strip (audit 2026-11-10, research §12): Paid + Unpaid +
       Overdue = Total, all over the filtered list. Paid = money
       actually received, Unpaid = balance not yet due, Overdue =
       balance past its Due Date — the three sum exactly to Total. */
    var todayStr = new Date().toISOString().slice(0, 10);
    function isOverdue(i) {
      return i.balancePaisa > 0 && !!i.dueDate && i.dueDate < todayStr;
    }
    var paidSum = 0, unpaidSum = 0, overdueSum = 0, totalSum = 0;
    list.forEach(function (i) {
      paidSum += i.paidPaisa || 0;
      totalSum += i.totalPaisa || 0;
      if (isOverdue(i)) overdueSum += i.balancePaisa;
      else unpaidSum += i.balancePaisa || 0;
    });
    $("bookPaid").textContent = fmtRs(paidSum);
    $("bookUnpaid").textContent = fmtRs(unpaidSum);
    $("bookOverdue").textContent = fmtRs(overdueSum);
    $("bookTotal").textContent = fmtRs(totalSum);
    if (bookKind === "sale") {
      $("bookSaleWrap").classList.remove("hidden");
      $("bookSaleTotal").textContent = fmtRs(totalSum);
      $("bookSaleReceived").textContent = fmtRs(paidSum);
      $("bookSaleBalance").textContent = fmtRs(unpaidSum + overdueSum);
      apiGet("/api/dashboard", "dashboard").then(function (dr) {
        var pct = dr.body.dashboard.salesVsLastMonthPct;
        $("bookSaleVs").textContent = pct == null ? "" :
          (pct >= 0 ? "▲ " : "▼ ") + Math.abs(pct) + "% vs last month";
      }).catch(function () {});
    } else {
      $("bookSaleWrap").classList.add("hidden");
    }
    var body = $("bookBody");
    body.innerHTML = "";
    $("bookEmpty").classList.toggle("hidden", list.length > 0);
    list.forEach(function (inv) {
      var tr = document.createElement("tr");
      var cash = inv.paidPaisa >= inv.totalPaisa && inv.totalPaisa > 0;
      var status = inv.balancePaisa <= 0 ? "Paid"
        : isOverdue(inv) ? "Overdue"
        : (inv.paidPaisa > 0 ? "Partial" : "Unpaid");
      tr.innerHTML =
        "<td>" + esc((inv.createdAt || "").slice(0, 10)) + "</td>" +
        "<td>" + esc(inv.invoiceNo) + "</td>" +
        "<td>" + esc(inv.partyId ? (partyNames[inv.partyId] || "Party") : meta.cashName) + "</td>" +
        "<td>" + esc(meta.title) + "</td>" +
        "<td>" + (cash ? "Cash" : "Credit") + "</td>" +
        '<td class="num">' + fmtRs(inv.totalPaisa) + "</td>" +
        '<td class="num">' + fmtRs(inv.balancePaisa) + "</td>" +
        "<td>" + esc(inv.dueDate || "—") + "</td>" +
        "<td>" + status + "</td>";
      var td = document.createElement("td");
      td.className = "row-actions";
      var pdf = document.createElement("button");
      pdf.className = "btn btn-mini"; pdf.type = "button"; pdf.textContent = "PDF";
      pdf.addEventListener("click", function () { downloadInvoicePdf(inv.id); });
      td.appendChild(pdf);
      if (inv.balancePaisa > 0) {
        var settle = document.createElement("button");
        settle.className = "btn btn-mini btn-primary"; settle.type = "button";
        settle.textContent = meta.settle;
        settle.addEventListener("click", function () {
          startSettle(inv, bookKind === "sale" ? "in" : "out",
            meta.title + " " + inv.invoiceNo + " — balance " + fmtRs(inv.balancePaisa) + ".");
        });
        td.appendChild(settle);
      }
      appendDocRowActions(td, inv, meta, "book");
      tr.appendChild(td);
      body.appendChild(tr);
    });
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
$("btnBookAdd").addEventListener("click", function () { openDocForm(bookKind); });
$("bookPeriodMonth").addEventListener("click", function () { resetBookPeriod(); loadBook(); });
$("btnBookGo").addEventListener("click", function () {
  bookFrom = $("bookFrom").value; bookTo = $("bookTo").value; loadBook();
});

function loadDocs() {
  var meta = DOC_META[docsType];
  $("docsTitle").textContent = meta.bookTitle;
  $("btnDocsAdd").textContent = meta.add;
  apiGet("/api/invoices", "invoices").then(function (r) {
    var partyNames = {};
    ((cacheGet("parties") || {}).parties || []).forEach(function (p) { partyNames[p.id] = p.name; });
    var q = searchText();
    var list = (r.body.invoices || []).filter(function (i) {
      if (i.type !== docsType) return false;
      if (q && ((partyNames[i.partyId] || "") + " " + i.invoiceNo).toLowerCase().indexOf(q) < 0) return false;
      return true;
    }).slice().reverse();
    var body = $("docsBody");
    body.innerHTML = "";
    $("docsEmpty").classList.toggle("hidden", list.length > 0);
    var convSum = 0, openSum = 0;
    list.forEach(function (i) { if (i.convertedToId) convSum += i.totalPaisa; else openSum += i.totalPaisa; });
    $("docsTotals").classList.toggle("hidden", !CONVERTIBLE[docsType]);
    $("docsConverted").textContent = fmtRs(convSum);
    $("docsOpen").textContent = fmtRs(openSum);
    list.forEach(function (inv) {
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + esc((inv.createdAt || "").slice(0, 10)) + "</td>" +
        "<td>" + esc(inv.invoiceNo) + "</td>" +
        "<td>" + esc(inv.partyId ? (partyNames[inv.partyId] || "Party") : meta.cashName) + "</td>" +
        '<td class="num">' + fmtRs(inv.totalPaisa) + "</td>" +
        "<td>" + (inv.convertedToId ? "Converted" : "Open") + "</td>";
      var td = document.createElement("td");
      td.className = "row-actions";
      var pdf = document.createElement("button");
      pdf.className = "btn btn-mini"; pdf.type = "button"; pdf.textContent = "PDF";
      pdf.addEventListener("click", function () { downloadInvoicePdf(inv.id); });
      td.appendChild(pdf);
      if (CONVERTIBLE[docsType] && !inv.convertedToId) {
        var conv = document.createElement("button");
        conv.className = "btn btn-mini btn-primary"; conv.type = "button";
        conv.textContent = "Convert to Sale";
        conv.addEventListener("click", function () {
          if (!navigator.onLine) { setMsg("appMsg", "Converting needs internet — please try again when you are online.", true); return; }
          conv.disabled = true;
          apiPost("/api/invoices/" + inv.id + "/convert", {}).then(function (body) {
            setMsg("appMsg", "Converted to Sale Invoice " + body.invoice.invoiceNo + ".");
            loadDocs(); loadDashboard();
          }).catch(function (e) { setMsg("appMsg", e.message, true); conv.disabled = false; });
        });
        td.appendChild(conv);
      }
      appendDocRowActions(td, inv, meta, "docs");
      tr.appendChild(td);
      body.appendChild(tr);
    });
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
$("btnDocsAdd").addEventListener("click", function () { openDocForm(docsType); });

/* Shared per-transaction actions: Edit / History / WhatsApp / Delete. */
function appendDocRowActions(td, inv, meta, where) {
  var passcodeGate = QB.settings && QB.settings.txn_passcode_edit_delete;
  function passcodeCheck(next) {
    if (!passcodeGate) { next(); return; }
    var pw = prompt("Enter your QBill password to edit or delete this transaction:");
    if (pw == null) return;
    apiPost("/api/auth/verify-password", { password: pw }).then(next)
      .catch(function (e) { setMsg("appMsg", e.message, true); });
  }
  var edit = document.createElement("button");
  edit.className = "btn btn-mini"; edit.type = "button"; edit.textContent = "Edit";
  edit.addEventListener("click", function () { passcodeCheck(function () { startEditInvoice(inv); }); });
  td.appendChild(edit);
  var hist = document.createElement("button");
  hist.className = "btn btn-mini"; hist.type = "button"; hist.textContent = "History";
  hist.addEventListener("click", function () { showInvoiceHistory(inv); });
  td.appendChild(hist);
  var wa = document.createElement("button");
  wa.className = "btn btn-mini"; wa.type = "button"; wa.textContent = "WhatsApp";
  wa.addEventListener("click", function () { shareInvoiceWhatsApp(inv); });
  td.appendChild(wa);
  var del = document.createElement("button");
  del.className = "btn btn-mini"; del.type = "button"; del.textContent = "Delete";
  del.addEventListener("click", function () {
    passcodeCheck(function () {
      if (!confirm("Do you really want to delete this transaction?\n" + meta.title + " " + inv.invoiceNo + " moves to Utilities > Recycle Bin and its stock effect is reversed.")) return;
      apiDelete("/api/invoices/" + inv.id).then(function () {
        setMsg("appMsg", meta.title + " " + inv.invoiceNo + " moved to the Recycle Bin.");
        if (where === "book") loadBook(); else loadDocs();
        loadDashboard();
      }).catch(function (e) { setMsg("appMsg", e.message, true); });
    });
  });
  td.appendChild(del);
}
function startEditInvoice(inv) {
  var existingTab = null;
  docTabs.forEach(function (t) { if (t.editingId === inv.id) existingTab = t; });
  if (existingTab) { showScreen("sale"); activateDocTab(existingTab); return; }
  if (activeDocTab) activeDocTab.state = captureDocState();
  var tab = { id: ++docTabSeq, type: inv.type || "sale", editingId: inv.id,
              label: inv.invoiceNo || null, state: null, blank: null, baseline: null };
  docTabs.push(tab);
  activeDocTab = tab;
  renderDocTabs();
  apiGet("/api/items", "items").then(function () {
    return apiGet("/api/invoices/" + inv.id, null);
  }).then(function (r) {
    var full = r.body.invoice;
    docType = full.type;
    editingInvoiceId = full.id;
    showScreen("sale");
    $("saleParty").value = full.partyId || "";
    $("saleLines").innerHTML = "";
    (full.lines || []).forEach(function (l) { addSaleLine(l.itemId, l); });
    if (!(full.lines || []).length) addSaleLine();
    $("salePaid").value = ((full.paidPaisa || 0) / 100).toFixed(2);
    $("docBilling").value = full.billingAddress || "";
    $("docShipping").value = full.shippingAddress || "";
    $("docDueDate").value = full.dueDate || "";
    $("docPayTerms").value = full.paymentTerms || "";
    $("docPoNumber").value = full.poNumber || "";
    $("docTransport").value = full.transportName || "";
    $("docVehicle").value = full.vehicleNo || "";
    $("docDeliveryDate").value = full.deliveryDate || "";
    $("docDeliveryLoc").value = full.deliveryLocation || "";
    $("docDescription").value = full.description || "";
    /* Restore the bill's terms selection too — it lives inside the
       Terms collapsible, and an edit must not silently drop it. */
    docTermsSelected = full.termsId || null;
    loadTermsPicker();
    if (full.description) showPanel("docDescWrap");
    if (full.attachmentName) { $("docAttachment").value = full.attachmentName; showPanel("docAttachWrap"); }
    $("docNumber").textContent = full.invoiceNo;
    /* Editing: expand a collapsible section when the saved bill
       holds values inside it, so nothing is hidden. */
    docSectionSet("more", docMoreHasContent());
    docSectionSet("terms", !!docTermsSelected);
    updateDocPartyInfo();
    calcSaleTotal();
    if ((full.payments || []).length > 1) {
      $("docPayTotal").textContent = "This bill was paid in " + full.payments.length +
        " parts. Add payment rows below only to replace them.";
    }
    tab.state = captureDocState();
    tab.baseline = JSON.stringify(tab.state);
    renderDocTabs();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
function showInvoiceHistory(inv) {
  apiGet("/api/invoices/" + inv.id + "/history", null).then(function (r) {
    var rows = (r.body.history || []).map(function (h) {
      return '<div class="row">' + rowHtml(h.action + " — " + (h.actor || ""), h.createdAt, null, "") + "</div>";
    }).join("");
    openDialog("Edit History — " + inv.invoiceNo,
      rows || '<p class="empty">No history recorded for this transaction.</p>');
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
function shareInvoiceWhatsApp(inv) {
  var parties = ((cacheGet("parties") || {}).parties) || [];
  var party = parties.filter(function (p) { return p.id === inv.partyId; })[0];
  var phone = party && party.phone ? party.phone.replace(/\D/g, "") : "";
  if (phone && phone.charAt(0) === "0") phone = "92" + phone.slice(1);
  var s = QB.settings || {};
  var tmpl = s.msg_template || "Hello [Party Name], your [Transaction Type] [Transaction Number] of [Amount] is ready. [Business Name]";
  var msg = tmpl
    .split("[Party Name]").join(party ? party.name : "Customer")
    .split("[Transaction Type]").join(DOC_META[inv.type].title)
    .split("[Transaction Number]").join(inv.invoiceNo)
    .split("[Amount]").join(fmtRs(inv.totalPaisa))
    .split("[Business Name]").join((me && me.business && me.business.name) || "QBill");
  if (s.msg_include_balance && party) msg += "\nCurrent balance: " + fmtRs(party.balancePaisa || 0);
  if (s.msg_include_link) msg += "\n" + location.origin + "/api/invoices/" + inv.id + "/pdf";
  if (s.msg_footer) msg += "\n" + s.msg_footer;
  window.open("https://wa.me/" + phone + "?text=" + encodeURIComponent(msg), "_blank");
}
function showLowStockDialog(lows, done) {
  var list = lows.map(function (l) {
    return "<li><strong>" + esc(l.name) + "</strong> — stock " + l.stockQty +
      " is below the minimum to maintain (" + l.minStock + ").</li>";
  }).join("");
  openDialog("Low Stock", "<p>Some items are below their Min Stock To Maintain:</p><ul>" + list +
    "</ul><p>The bill is saved. Keep an eye on these items and reorder soon.</p>" +
    '<button class="btn btn-primary btn-block" id="dlgLowOk" type="button">OK</button>');
  $("dlgLowOk").addEventListener("click", function () { closeDialog(); done(); });
}
function showRepeatDialog(inv, payload) {
  var freqLabel = { daily: "every day", weekly: "every week", monthly: "every month", yearly: "every year" }[payload.repeat_freq] || "";
  openDialog("Repeat Invoice",
    "<p><strong>" + esc(inv.invoiceNo) + "</strong> is set to repeat <strong>" + freqLabel + "</strong>.</p>" +
    "<p>Create the next 3 copies now?</p>" +
    '<div style="display:flex; gap:8px"><button class="btn btn-primary" id="repYes" type="button">Create 3 copies</button>' +
    '<button class="btn" id="repNo" type="button">Not now</button></div>');
  $("repNo").addEventListener("click", closeDialog);
  $("repYes").addEventListener("click", function () {
    var chain = Promise.resolve();
    for (var k = 0; k < 3; k++) {
      chain = chain.then(function () {
        return apiPost("/api/invoices/" + inv.id + "/convert", {}).then(function () {});
      });
    }
    chain.then(function () {
      closeDialog();
      setMsg("appMsg", "Repeat copies created (see the book). Note: copies carry today's date.");
    }).catch(function (e) { setMsg("appMsg", e.message, true); });
  });
}

/* ------------------------------------------------- cash & bank, P&L -- */
function loadCashBank() {
  apiGet("/api/payments", "payments").then(function (r) {
    var parties = {};
    ((cacheGet("parties") || {}).parties || []).forEach(function (p) { parties[p.id] = p.name; });
    var list = (r.body.payments || []).slice().reverse();
    var tin = 0, tout = 0;
    list.forEach(function (p) { if (p.direction === "in") tin += p.amountPaisa; else tout += p.amountPaisa; });
    $("cbIn").textContent = fmtRs(tin);
    $("cbOut").textContent = fmtRs(tout);
    /* Cash In Hand table (audit 2026-11-10, research §10): Type /
       Name / Date / Amount columns, like Vyapar's screen. */
    var cbody = $("cashBody");
    cbody.innerHTML = "";
    $("cashEmpty").classList.toggle("hidden", list.length > 0);
    list.forEach(function (p) {
      var tr = document.createElement("tr");
      var isAdj = (p.note || "").indexOf("Cash Adjustment") === 0;
      var type = isAdj ? "Cash Adjustment" : (p.direction === "in" ? "Received" : "Paid");
      var name = (p.partyId ? (parties[p.partyId] || "") : "") ||
        (isAdj ? (p.note.replace(/^Cash Adjustment\s*—\s*/, "") || "Cash Adjustment") : (p.note || "Cash"));
      tr.innerHTML =
        "<td>" + esc(type) + "</td>" +
        "<td>" + esc(name) + "</td>" +
        "<td>" + esc((p.createdAt || "").slice(0, 10)) + "</td>" +
        '<td class="num">' + (p.direction === "in" ? "+" : "−") + fmtRs(p.amountPaisa) + "</td>";
      cbody.appendChild(tr);
    });
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
  apiGet("/api/dashboard", "dashboard").then(function (r) {
    $("cbCashAmt").textContent = fmtRs(r.body.dashboard.cashInHandPaisa);
  }).catch(function () {});
}
/* Cash In Hand > Adjust Cash (delta #4): records a cash-adjustment
   transaction that flows into Day Book, All Transactions and the
   Cash in Hand total. */
$("btnAdjustCash").addEventListener("click", function () {
  var today = new Date().toISOString().slice(0, 10);
  openDialog("Adjust Cash",
    '<label>Adjustment <select id="adjDir"><option value="in">Add Cash</option><option value="out">Remove Cash</option></select></label>' +
    '<label>Amount (Rs) <input id="adjAmt" type="number" step="0.01" min="0" value="0"></label>' +
    '<label>Adjustment Date <input id="adjDate" type="date" value="' + today + '"></label>' +
    '<label>Description <input id="adjDesc" type="text" placeholder="Why is cash being adjusted?"></label>' +
    '<button class="btn btn-primary btn-block" id="adjSave" type="button">Save Adjustment</button>');
  $("adjSave").addEventListener("click", function () {
    var amt = rsToPaisa($("adjAmt").value);
    if (amt <= 0) { setMsg("appMsg", "Enter an amount.", true); return; }
    apiPost("/api/cash-adjustments", {
      direction: $("adjDir").value, amount_paisa: amt,
      date: $("adjDate").value || null,
      description: $("adjDesc").value.trim(),
      client_ref: "adj-" + Date.now()
    }).then(function () {
      closeDialog();
      setMsg("appMsg", "Cash adjusted.");
      loadCashBank(); loadDashboard();
    }).catch(function (e) { setMsg("appMsg", e.message, true); });
  });
});
function loadPnl() {
  var q = "";
  var f = $("pnlFrom").value, t = $("pnlTo").value;
  var url = "/api/pnl" + (f && t ? "?from=" + f + "&to=" + t : "");
  apiGet(url, "pnl_view").then(function (r) {
    var p = r.body.pnl;
    $("pnlSales").textContent = fmtRs(p.salesPaisa);
    $("pnlPurchases").textContent = fmtRs(p.purchasesPaisa);
    if ($("pnlCogs")) $("pnlCogs").textContent = fmtRs(p.cogsPaisa || 0);
    $("pnlOtherIncome").textContent = fmtRs(p.otherIncomePaisa || 0);
    $("pnlExpenses").textContent = fmtRs(p.expensesPaisa);
    $("pnlProfit").textContent = fmtRs(p.profitPaisa);
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
$("btnPnlGo").addEventListener("click", loadPnl);
function loadDayBook() {
  var date = $("dbDate").value || new Date().toISOString().slice(0, 10);
  $("dbDate").value = date;
  apiGet("/api/reports/day-book?date=" + date, "daybook_" + date).then(function (r) {
    var t = r.body.totals || {};
    $("dbDocs").textContent = fmtRs(t.documentsPaisa || 0);
    $("dbIn").textContent = fmtRs(t.paymentsInPaisa || 0);
    $("dbOut").textContent = fmtRs(t.paymentsOutPaisa || 0);
    $("dbExp").textContent = fmtRs(t.expensesPaisa || 0);
    var list = r.body.entries || [];
    $("daybookList").innerHTML = list.length ? "" : '<p class="empty">No transactions on this date.</p>';
    list.forEach(function (e) {
      var div = document.createElement("div");
      div.className = "row";
      div.innerHTML = rowHtml(e.description, e.party || "", fmtRs(e.amountPaisa),
        e.kind === "expense" ? "neg" : "");
      $("daybookList").appendChild(div);
    });
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
$("btnDbGo").addEventListener("click", loadDayBook);
function loadStock() {
  apiGet("/api/reports/stock-summary", "stock_summary").then(function (r) {
    var list = r.body.items || [];
    var body = $("stockBody");
    body.innerHTML = "";
    $("stockEmpty").classList.toggle("hidden", list.length > 0);
    list.forEach(function (i) {
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + esc(i.name) + "</td>" +
        '<td class="num">' + i.openingQty + "</td>" +
        '<td class="num">' + i.inQty + "</td>" +
        '<td class="num">' + i.outQty + "</td>" +
        '<td class="num">' + i.closingQty + "</td>" +
        '<td class="num">' + fmtRs(i.valuePaisa) + "</td>";
      body.appendChild(tr);
    });
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}

/* ----------------------------------------------------------- payments -- */
var payInvoiceId = null;   /* set when settling a specific invoice */
function setPayFilter(filter, direction) {
  payFilter = filter;
  document.querySelectorAll("[data-payfilter]").forEach(function (c) {
    c.classList.toggle("active", c.getAttribute("data-payfilter") === filter);
  });
  if (direction) $("payDirection").value = direction;
}
function startSettle(inv, direction, note) {
  payInvoiceId = inv.id;
  setPayFilter(direction, direction);
  if (inv.partyId) $("payParty").value = inv.partyId;
  $("payAmount").value = (inv.balancePaisa / 100).toFixed(2);
  $("paySettleText").textContent = note + " Saving this payment will settle that bill.";
  $("paySettleNote").classList.remove("hidden");
  navKey = direction === "in" ? "paymentin" : "paymentout";
  showScreen("payments");
  showPanel("paymentPanel");
  fillPartySelects();
  if (inv.partyId) $("payParty").value = inv.partyId;
}
$("btnPaySettleClear").addEventListener("click", function () {
  payInvoiceId = null;
  $("paySettleNote").classList.add("hidden");
});
document.querySelectorAll("[data-payfilter]").forEach(function (chip) {
  chip.addEventListener("click", function () {
    var f = chip.getAttribute("data-payfilter");
    setPayFilter(f, f === "all" ? null : f);
    loadPayments();
  });
});
function loadPayments() {
  apiGet("/api/payments", "payments").then(function (r) {
    var parties = {};
    ((cacheGet("parties") || {}).parties || []).forEach(function (p) { parties[p.id] = p.name; });
    var q = searchText();
    var list = (r.body.payments || []).filter(function (p) {
      if (payFilter !== "all" && p.direction !== payFilter) return false;
      if (q && ((parties[p.partyId] || "")).toLowerCase().indexOf(q) < 0) return false;
      return true;
    }).slice().reverse();
    $("paymentList").innerHTML = list.length ? "" : '<p class="empty">No payments yet.</p>';
    list.forEach(function (p) {
      var div = document.createElement("div");
      div.className = "row";
      var metaBits = [p.mode || "", p.chequeNo ? "Chq " + p.chequeNo : "", p.referenceNo || ""]
        .filter(Boolean).join(" · ");
      div.innerHTML = rowHtml(
        (p.direction === "in" ? "Received" : "Paid") + (p.partyId ? " · " + (parties[p.partyId] || "") : ""),
        (p.createdAt || "").slice(0, 10) + (metaBits ? " · " + metaBits : ""),
        fmtRs(p.amountPaisa), p.direction === "in" ? "pos" : "neg");
      var pacts = document.createElement("div");
      pacts.className = "row-actions";
      pacts.appendChild(miniBtn("Delete", function () {
        deleteRecord("this payment of " + fmtRs(p.amountPaisa),
          "Only standalone cash payments can be deleted here - a payment recorded against a bill, or through a bank account, is refused with the reason so your balances never break.",
          "/api/payments/" + p.id, function () { loadPayments(); loadDashboard(); });
      }));
      div.appendChild(pacts);
      $("paymentList").appendChild(div);
    });
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
  var s = QB.settings || {};
  $("payDiscountWrap").classList.toggle("hidden", !s.discount_during_payments);
  $("allocateCard").classList.toggle("hidden", !s.link_payments_to_invoices);
  apiGet("/api/bank-accounts", null).then(function (r) {
    var sel = $("payBank");
    sel.innerHTML = '<option value="">— None —</option>' + (r.body.bankAccounts || []).map(function (a) {
      return '<option value="' + a.id + '">' + esc(a.displayName) + "</option>";
    }).join("");
  }).catch(function () {});
}
function syncPayModeFields() {
  var mode = $("payMode").value;
  $("payBankWrap").classList.toggle("hidden", !(mode === "bank" || mode === "jazzcash" || mode === "easypaisa"));
  $("payChequeWrap").classList.toggle("hidden", mode !== "cheque");
}
$("payMode").addEventListener("change", syncPayModeFields);
$("paymentForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  var payload = {
    party_id: $("payParty").value ? parseInt($("payParty").value, 10) : null,
    direction: $("payDirection").value,
    amount_paisa: rsToPaisa($("payAmount").value),
    mode: $("payMode").value,
    bank_account_id: $("payBank").value ? parseInt($("payBank").value, 10) : null,
    cheque_no: $("payCheque").value.trim(),
    reference_no: $("payReference").value.trim(),
    discount_paisa: rsToPaisa($("payDiscount").value)
  };
  if (payInvoiceId) payload.invoice_id = payInvoiceId;
  apiCreate("payment", payload).then(function (r) {
    $("paymentForm").reset();
    $("payDiscount").value = 0;
    syncPayModeFields();
    payInvoiceId = null;
    $("paySettleNote").classList.add("hidden");
    hidePanel("paymentPanel");
    setMsg("appMsg", r.queued ? "Saved on this device — will sync when online." : "Payment saved.");
    loadPayments();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});
$("btnAddPayment").addEventListener("click", function () {
  showPanel("paymentPanel");
  $("payAmount").focus();
});
$("btnPaymentBack").addEventListener("click", function () { hidePanel("paymentPanel"); });
$("btnAllocate").addEventListener("click", function () {
  var pid = $("allocParty").value;
  var amt = rsToPaisa($("allocAmount").value);
  if (!pid || amt <= 0) { setMsg("appMsg", "Pick a party and an amount to allocate.", true); return; }
  apiPost("/api/payments/allocate", {
    party_id: parseInt(pid, 10), amount_paisa: amt, direction: $("allocDirection").value
  }).then(function (body) {
    var parts = (body.allocations || []).map(function (a) {
      return a.invoiceNo + ": " + fmtRs(a.amountPaisa);
    }).join(" · ");
    $("allocResult").textContent = "Allocated — " + parts +
      (body.unallocatedPaisa > 0 ? " · Leftover unallocated: " + fmtRs(body.unallocatedPaisa) : "");
    loadPayments();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});

/* ----------------------------------------------------------- expenses -- */
function loadExpenses() {
  fillPartySelects();
  apiGet("/api/expense-categories", null).then(function (r) {
    $("expCategory").innerHTML = '<option value="">— None —</option>' + (r.body.categories || []).map(function (c) {
      var ctype = c.type || c.categoryType || "Indirect Expense";
      return '<option value="' + esc(c.name) + '" data-type="' + esc(ctype) + '">' +
        esc(c.name) + " (" + esc(ctype) + ")</option>";
    }).join("");
  }).catch(function () {});
  apiGet("/api/expense-items", null).then(function (r) {
    $("expItem").innerHTML = '<option value="">— None —</option>' + (r.body.items || []).map(function (i) {
      return "<option>" + esc(i.name) + "</option>";
    }).join("");
  }).catch(function () {});
  apiGet("/api/expenses", "expenses").then(function (r) {
    var q = searchText();
    lastExpenses = (r.body.expenses || []).filter(function (e) {
      return !q || (e.title || "").toLowerCase().indexOf(q) >= 0;
    }).slice().reverse();
    renderExpenses();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
/* Expenses layout (audit 2026-11-10, research §11): a category pane
   (each category with its total; click to filter) beside the detail
   table — DATE / EXPENSE / PARTY / PAYMENT TYPE / AMOUNT / BALANCE.
   QBill expenses are cash-book records, so the payment type reads
   Cash; they carry no EXP NO. sequence (see the audit record). */
var lastExpenses = [];
var expCatFilter = "";
function renderExpenses() {
  var partyNames = {};
  ((cacheGet("parties") || {}).parties || []).forEach(function (p) { partyNames[p.id] = p.name; });
  var totals = {}, grand = 0;
  lastExpenses.forEach(function (e) {
    var c = e.category || "Uncategorised";
    totals[c] = (totals[c] || 0) + e.amountPaisa;
    grand += e.amountPaisa;
  });
  var cats = Object.keys(totals).sort();
  $("expenseCats").innerHTML = "<h3 style='margin-top:0'>Categories</h3>" +
    ['<button type="button" class="exp-cat' + (expCatFilter === "" ? " sel" : "") +
      '" data-expcat=""><span>All Categories</span><strong>' + fmtRs(grand) + "</strong></button>"]
      .concat(cats.map(function (c) {
        return '<button type="button" class="exp-cat' + (expCatFilter === c ? " sel" : "") +
          '" data-expcat="' + esc(c) + '"><span>' + esc(c) + "</span><strong>" +
          fmtRs(totals[c]) + "</strong></button>";
      })).join("");
  $("expenseCats").querySelectorAll("[data-expcat]").forEach(function (b) {
    b.addEventListener("click", function () {
      expCatFilter = b.getAttribute("data-expcat");
      renderExpenses();
    });
  });
  var list = lastExpenses.filter(function (e) {
    return !expCatFilter || (e.category || "Uncategorised") === expCatFilter;
  });
  var body = $("expenseBody");
  body.innerHTML = "";
  $("expenseEmpty").classList.toggle("hidden", list.length > 0);
  list.forEach(function (e) {
    var tr = document.createElement("tr");
    tr.innerHTML =
      "<td>" + esc((e.createdAt || "").slice(0, 10)) + "</td>" +
      "<td>" + esc(e.title) +
        (e.expenseItem ? '<div class="sub">' + esc(e.expenseItem) + "</div>" : "") + "</td>" +
      "<td>" + esc(e.partyId ? (partyNames[e.partyId] || "—") : "—") + "</td>" +
      "<td>Cash</td>" +
      '<td class="num">' + fmtRs(e.amountPaisa) + "</td>" +
      '<td class="num">' + fmtRs(e.balancePaisa) + "</td>";
    var td = document.createElement("td");
    td.className = "row-actions";
    td.appendChild(miniBtn("Delete", function () {
      deleteRecord("expense '" + e.title + "' (" + fmtRs(e.amountPaisa) + ")",
        "It moves to the Recycle Bin (Utilities) and stops counting in your totals until you restore it.",
        "/api/expenses/" + e.id, function () { loadExpenses(); loadDashboard(); });
    }));
    tr.appendChild(td);
    body.appendChild(tr);
  });
}
$("expCategory").addEventListener("change", function () {
  var o = $("expCategory").options[$("expCategory").selectedIndex];
  if (o && o.getAttribute("data-type")) $("expCategoryType").value = o.getAttribute("data-type");
});
$("expTaxOn").addEventListener("change", function () {
  $("expTaxWrap").classList.toggle("hidden", !$("expTaxOn").checked);
});
$("expenseForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  var paidVal = $("expPaid").value;
  apiCreate("expense", {
    title: $("expTitle").value.trim(),
    category: ($("expCategoryNew").value.trim() || $("expCategory").value || "").trim(),
    category_type: $("expCategoryType").value,
    expense_item: ($("expItemNew").value.trim() || $("expItem").value || "").trim(),
    party_id: $("expParty").value ? parseInt($("expParty").value, 10) : null,
    amount_paisa: rsToPaisa($("expAmount").value),
    tax_paisa: $("expTaxOn").checked ? rsToPaisa($("expTax").value) : 0,
    paid_paisa: paidVal === "" ? null : rsToPaisa(paidVal)
  }).then(function (r) {
    $("expenseForm").reset();
    $("expTaxWrap").classList.add("hidden");
    hidePanel("expensePanel");
    setMsg("appMsg", r.queued ? "Saved on this device — will sync when online." : "Expense saved.");
    loadExpenses();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});
$("btnAddExpense").addEventListener("click", function () {
  showPanel("expensePanel");
  $("expTitle").focus();
});
$("btnExpenseBack").addEventListener("click", function () { hidePanel("expensePanel"); });

/* ----------------------------------------------------------- settings -- */
function setSettingsCat(cat) {
  settingsCat = cat;
  document.querySelectorAll(".set-cat").forEach(function (b) {
    b.classList.toggle("active", b.getAttribute("data-setcat") === cat);
  });
  document.querySelectorAll("[data-setcard]").forEach(function (card) {
    var show = card.getAttribute("data-setcard") === cat;
    if (show && (card.id === "ownerMailCard" || card.id === "ownerAccountsCard")) {
      show = !!(me && me.isOwner);
    }
    card.classList.toggle("hidden", !show);
  });
}
document.querySelectorAll(".set-cat").forEach(function (b) {
  b.addEventListener("click", function () { setSettingsCat(b.getAttribute("data-setcat")); });
});
/* Business identity (delta build #3, Fix 3): the account's OWN name
   and logo drive the sidebar card, the top header, the bill PDFs and
   the public store page. The logo <img> tags are fed from an authed
   fetch (blob URL) because the logo endpoint needs the session. */
var logoObjectUrl = null;
function applyBusinessIdentity(b) {
  if (!b) return;
  if (b.name) {
    $("sideBizName").textContent = b.name;
    $("headBizName").textContent = b.name;
  }
  if (b.email) $("sideBizEmail").textContent = b.email;
  refreshLogoImages(!!b.hasLogo);
}
function refreshLogoImages(hasLogo) {
  var imgs = [$("sideBizLogo"), $("headBizLogo")];
  if (!hasLogo) {
    imgs.forEach(function (im) { if (im) im.classList.add("hidden"); });
    return;
  }
  fetch("/api/business/logo", { headers: authHeaders() }).then(function (resp) {
    if (!resp.ok) throw new Error("logo " + resp.status);
    return resp.blob();
  }).then(function (blob) {
    if (logoObjectUrl) URL.revokeObjectURL(logoObjectUrl);
    logoObjectUrl = URL.createObjectURL(blob);
    imgs.forEach(function (im) {
      if (im) { im.src = logoObjectUrl; im.classList.remove("hidden"); }
    });
    var prev = $("profLogoPreview");
    if (prev) {
      prev.src = logoObjectUrl;
      prev.classList.remove("hidden");
      $("profLogoEmpty").classList.add("hidden");
      $("btnLogoRemove").classList.remove("hidden");
    }
  }).catch(function () {
    imgs.forEach(function (im) { if (im) im.classList.add("hidden"); });
  });
}
function openBusinessProfile() {
  go("settings");
  setSettingsCat("general");
  loadProfile();
}
$("sideBrand").addEventListener("click", openBusinessProfile);
$("headBrand").addEventListener("click", openBusinessProfile);
function loadProfile() {
  apiGet("/api/business", "business").then(function (r) {
    var b = r.body.business || {};
    $("profName").value = b.name || "";
    $("profOwner").value = b.ownerName || "";
    $("profAddress").value = b.address || "";
    $("profCity").value = b.city || "";
    $("profPhone").value = b.phone || "";
    $("profEmail").value = b.email || "";
    $("profBizType").value = b.businessType || "";
    $("profBizCategory").value = b.businessCategory || "";
    $("profPincode").value = b.pincode || "";
    $("profState").value = b.state || "";
    $("profSignature").value = b.signatureText || "";
    $("profTaxNote").value = b.taxNote || "";
    $("profTerms").value = b.terms || "";
    $("profLogoPreview").classList.toggle("hidden", !b.hasLogo);
    $("profLogoEmpty").classList.toggle("hidden", !!b.hasLogo);
    $("btnLogoRemove").classList.toggle("hidden", !b.hasLogo);
    applyBusinessIdentity(b);
  }).catch(function () {});
}
$("profileForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  apiPut("/api/business", {
    name: $("profName").value.trim(), owner_name: $("profOwner").value.trim(),
    address: $("profAddress").value.trim(), city: $("profCity").value.trim(),
    phone: $("profPhone").value.trim(), email: $("profEmail").value.trim(),
    business_type: $("profBizType").value.trim(),
    business_category: $("profBizCategory").value.trim(),
    pincode: $("profPincode").value.trim(),
    state: $("profState").value.trim(),
    signature_text: $("profSignature").value.trim(),
    tax_note: $("profTaxNote").value.trim(),
    terms: $("profTerms").value.trim()
  }).then(function (r) {
    cacheSet("business", { business: r.business });
    applyBusinessIdentity(r.business);
    setMsg("appMsg", "Profile saved. It appears on your invoices and PDFs.");
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});
/* Logo upload is ONLINE-ONLY: the image goes straight to the server
   (it is not queued in the offline outbox, which carries documents,
   not images). Offline, the shopkeeper gets a clear message. */
$("profLogoFile").addEventListener("change", function () {
  var f = $("profLogoFile").files[0];
  $("profLogoFile").value = "";
  if (!f) return;
  if (!navigator.onLine) {
    $("logoMsg").textContent = "You are offline — logo upload needs an internet connection. Please try again when you are back online.";
    return;
  }
  if (["image/png", "image/jpeg", "image/webp", "image/gif"].indexOf(f.type) < 0) {
    $("logoMsg").textContent = "Please choose a PNG, JPG, WebP or GIF image.";
    return;
  }
  if (f.size > 400 * 1024) {
    $("logoMsg").textContent = "That image is too large — please use a logo under 400 KB.";
    return;
  }
  var reader = new FileReader();
  reader.onload = function () {
    fetch("/api/business/logo", {
      method: "PUT", headers: authHeaders(),
      body: JSON.stringify({ logo_data: reader.result })
    }).then(function (resp) {
      return resp.json().then(function (body) {
        if (!resp.ok) throw new Error(body.error || "Logo upload failed.");
        return body;
      });
    }).then(function (body) {
      cacheSet("business", { business: body.business });
      applyBusinessIdentity(body.business);
      $("profLogoPreview").classList.remove("hidden");
      $("profLogoEmpty").classList.add("hidden");
      $("btnLogoRemove").classList.remove("hidden");
      $("logoMsg").textContent = "Logo saved. It now shows in the header, the sidebar and on your bill PDFs.";
      setMsg("appMsg", "Business logo saved.");
    }).catch(function (e) { $("logoMsg").textContent = e.message; });
  };
  reader.readAsDataURL(f);
});
$("btnLogoRemove").addEventListener("click", function () {
  if (!navigator.onLine) {
    $("logoMsg").textContent = "You are offline — removing the logo needs an internet connection.";
    return;
  }
  fetch("/api/business/logo", {
    method: "PUT", headers: authHeaders(),
    body: JSON.stringify({ logo_data: null })
  }).then(function (resp) {
    return resp.json().then(function (body) {
      if (!resp.ok) throw new Error(body.error || "Could not remove the logo.");
      return body;
    });
  }).then(function (body) {
    cacheSet("business", { business: body.business });
    applyBusinessIdentity(body.business);
    $("profLogoPreview").classList.add("hidden");
    $("profLogoEmpty").classList.remove("hidden");
    $("btnLogoRemove").classList.add("hidden");
    $("logoMsg").textContent = "Logo removed.";
  }).catch(function (e) { $("logoMsg").textContent = e.message; });
});

/* ------------------------------------------- app settings document --- */
/* One JSON settings doc per business (server: /api/settings). Inputs
   carry data-setting="<key>"; changes save immediately. */
var PRINT_THEMES = ["Advance Voucher Theme", "Advanced Theme", "Bill of Supply Theme",
  "GST Theme 1", "GST Theme 2", "GST Theme 3", "Modern Theme", "Stylish Theme",
  "Tally Theme", "Thermal Print", "GST Theme (With Barcode)", "Double Divine Theme"];
var PRINT_COLORS = ["#f26522", "#e53935", "#1e88e5", "#43a047", "#8e24aa", "#00acc1",
  "#fb8c00", "#5e35b1", "#3949ab", "#00897b", "#c0ca33", "#6d4c41", "#546e7a", "#d81b60"];
var DOC_TYPE_LABELS = { sale: "Sale", purchase: "Purchase", estimate: "Estimate",
  proforma: "Proforma", sale_order: "Sale Order", purchase_order: "Purchase Order",
  challan: "Delivery Challan", sale_return: "Sale Return", purchase_return: "Purchase Return",
  payment_in: "Payment In" };

function openDialog(title, html) {
  $("dlgTitle").textContent = title;
  $("dlgBody").innerHTML = html;
  $("dlgModal").classList.remove("hidden");
}
function closeDialog() { $("dlgModal").classList.add("hidden"); }
$("dlgClose").addEventListener("click", closeDialog);

function loadSettingsDoc() {
  return apiGet("/api/settings", null).then(function (r) {
    QB.settings = r.body.settings || {};
    applySettingsChrome();
    renderSettingsInputs();
    return QB.settings;
  }).catch(function () { return null; });
}
function saveSettings(patch) {
  return apiPut("/api/settings", { settings: patch }).then(function (body) {
    QB.settings = body.settings || QB.settings;
    applySettingsChrome();
    return QB.settings;
  });
}
function applySettingsChrome() {
  var s = QB.settings || {};
  var storeOn = s.store && s.store.enabled;
  $("navStore").classList.toggle("hidden", !storeOn);
  $("navAccounting").classList.toggle("hidden", !s.accounting_enabled);
  $("navOtherIncome").classList.toggle("hidden", s.more_tx_other_income === false);
  document.querySelectorAll(".mega-item").forEach(function (b) {
    var act = b.getAttribute("data-act") || "";
    var type = act.indexOf("doc:") === 0 ? act.slice(4) : "";
    var gate = { estimate: "more_tx_estimate", proforma: "more_tx_proforma",
      sale_order: "more_tx_sale_order", purchase_order: "more_tx_purchase_order",
      challan: "more_tx_challan", otherincome: "more_tx_other_income" }[act === "otherincome" ? "otherincome" : type];
    if (gate) b.disabled = s[gate] === false;
  });
  var inMemberMode = me && me.activeBusinessId &&
    !(me.businesses || []).some(function (b) { return b.id === me.activeBusinessId; });
  if (inMemberMode && me.activeRole) {
    $("memberBanner").classList.remove("hidden");
    $("memberRoleName").textContent = me.activeRole;
    $("memberBizName").textContent = (me.business && me.business.name) || "a shared business";
  } else {
    $("memberBanner").classList.add("hidden");
  }
  var ab = $("generalAutoBackup");
  if (ab) ab.textContent = s.auto_backup === false ? "OFF" : "ON";
}
$("btnSwitchOwn").addEventListener("click", function () {
  apiPost("/api/team/switch", { business_id: null }).then(function () { refreshMe(); })
    .catch(function (e) { setMsg("appMsg", e.message, true); });
});
function renderSettingsInputs() {
  var s = QB.settings || {};
  document.querySelectorAll("[data-setting]").forEach(function (inp) {
    var key = inp.getAttribute("data-setting");
    var val = s[key];
    if (inp.type === "checkbox") inp.checked = !!val;
    else if (val !== undefined && val !== null) inp.value = val;
  });
  var prefixes = s.prefixes || {};
  $("prefixGrid").innerHTML = Object.keys(DOC_TYPE_LABELS).map(function (t) {
    return '<label>' + DOC_TYPE_LABELS[t] + ' prefix <input type="text" data-prefix="' + t +
      '" value="' + esc(prefixes[t] || "") + '"></label>';
  }).join("");
  var ts = $("printThemeSel");
  ts.innerHTML = PRINT_THEMES.map(function (t) { return "<option>" + esc(t) + "</option>"; }).join("");
  if (s.print_theme) ts.value = s.print_theme;
  $("colorSwatches").innerHTML = PRINT_COLORS.map(function (c) {
    return '<button type="button" class="swatch' + (s.print_color === c ? " active" : "") +
      '" data-color="' + c + '" style="background:' + c + '" title="' + c + '"></button>';
  }).join("");
  $("colorSwatches").querySelectorAll(".swatch").forEach(function (b) {
    b.addEventListener("click", function () {
      saveSettings({ print_color: b.getAttribute("data-color") }).then(function () {
        renderSettingsInputs(); renderPrintPreview();
      });
    });
  });
  $("printNameSale").value = (s.print_names || {}).sale || "";
  $("printNamePurchase").value = (s.print_names || {}).purchase || "";
  $("defaultTaxPct").value = (s.default_tax_bp || 0) / 100;
  var auto = s.msg_auto || {};
  $("msgAutoGrid").innerHTML = Object.keys(DOC_TYPE_LABELS).map(function (t) {
    return '<label class="check-row"><input type="checkbox" data-msgauto="' + t + '"' +
      (auto[t] ? " checked" : "") + "> <span>" + DOC_TYPE_LABELS[t] + "</span></label>";
  }).join("");
  $("msgTemplate").value = s.msg_template || "";
  renderMsgPreview();
  $("partyExtraFields").innerHTML = (s.party_extra_fields || []).map(function (f, i) {
    return '<div class="grid2"><label class="check-row"><input type="checkbox" data-pef-on="' + i + '"' +
      (f.enabled ? " checked" : "") + '> <span>Additional Field ' + (i + 1) + '</span></label>' +
      '<label>Label <input type="text" data-pef-label="' + i + '" value="' + esc(f.label || "") + '"></label>' +
      '<label class="check-row"><input type="checkbox" data-pef-print="' + i + '"' +
      (f.show_in_print ? " checked" : "") + "> <span>Show In Print</span></label></div>";
  }).join("");
  $("itemExtraFields").innerHTML = (s.item_extra_fields || []).map(function (f, i) {
    return '<div class="grid2"><label class="check-row"><input type="checkbox" data-ief-on="' + i + '"' +
      (f.enabled ? " checked" : "") + '> <span>' + esc(f.key) + '</span></label>' +
      '<label>Label <input type="text" data-ief-label="' + i + '" value="' + esc(f.label || "") + '"></label></div>';
  }).join("");
  $("partyLoyaltyLink").checked = !!(s.loyalty && s.loyalty.enabled);
  renderPrintPreview();
  loadTermsList();
}
document.querySelectorAll("[data-setting]").forEach(function (inp) {
  inp.addEventListener("change", function () {
    var key = inp.getAttribute("data-setting");
    var patch = {};
    if (inp.type === "checkbox") patch[key] = inp.checked;
    else if (inp.type === "number") patch[key] = parseFloat(inp.value || "0") || 0;
    else patch[key] = inp.value;
    saveSettings(patch).then(function () {
      setMsg("appMsg", "Setting saved.");
      if (key === "print_theme") renderPrintPreview();
    }).catch(function (e) { setMsg("appMsg", e.message, true); });
  });
});
$("btnSavePrefixes").addEventListener("click", function () {
  var prefixes = {};
  document.querySelectorAll("[data-prefix]").forEach(function (inp) {
    prefixes[inp.getAttribute("data-prefix")] = inp.value.trim();
  });
  var names = {};
  if ($("printNameSale").value.trim()) names.sale = $("printNameSale").value.trim();
  if ($("printNamePurchase").value.trim()) names.purchase = $("printNamePurchase").value.trim();
  saveSettings({ prefixes: prefixes, print_names: names }).then(function () {
    setMsg("appMsg", "Prefixes saved.");
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});
$("defaultTaxPct").addEventListener("change", function () {
  saveSettings({ default_tax_bp: Math.round((parseFloat($("defaultTaxPct").value || "0") || 0) * 100) })
    .then(function () { setMsg("appMsg", "Default tax saved."); })
    .catch(function (e) { setMsg("appMsg", e.message, true); });
});
$("btnMsgSave").addEventListener("click", function () {
  var auto = {};
  document.querySelectorAll("[data-msgauto]").forEach(function (cb) {
    auto[cb.getAttribute("data-msgauto")] = cb.checked;
  });
  saveSettings({ msg_auto: auto, msg_template: $("msgTemplate").value }).then(function () {
    setMsg("appMsg", "Message settings saved.");
    renderMsgPreview();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});
function renderMsgPreview() {
  var s = QB.settings || {};
  var tmpl = $("msgTemplate").value || s.msg_template || "";
  var txt = tmpl.split("[Party Name]").join("Ahmed Traders")
    .split("[Transaction Type]").join("Sale Invoice")
    .split("[Transaction Number]").join("S-1001")
    .split("[Amount]").join(fmtRs(1250000))
    .split("[Business Name]").join("Your Business");
  if (s.msg_include_balance) txt += "\nCurrent balance: " + fmtRs(250000);
  if (s.msg_include_link) txt += "\n" + location.origin + "/api/invoices/1/pdf";
  if (s.msg_footer) txt += "\n" + s.msg_footer;
  $("msgPreview").textContent = txt;
}
$("msgTemplate").addEventListener("input", renderMsgPreview);
$("partyLoyaltyLink").addEventListener("change", function () {
  if ($("partyLoyaltyLink").checked) go("loyalty");
});
document.querySelectorAll("[data-printtab]").forEach(function (b) {
  b.addEventListener("click", function () {
    document.querySelectorAll("[data-printtab]").forEach(function (x) {
      x.classList.toggle("active", x === b);
    });
    $("printRegular").classList.toggle("hidden", b.getAttribute("data-printtab") !== "regular");
    $("printThermal").classList.toggle("hidden", b.getAttribute("data-printtab") !== "thermal");
  });
});
function renderPrintPreview() {
  var s = QB.settings || {};
  var color = s.print_color || "#f26522";
  var name = ((s.print_names || {}).sale) || "SALE INVOICE";
  var cols = s.print_item_columns || {};
  var head = "<tr><th>Item</th>" + (cols.qty ? "<th>Qty</th>" : "") +
    (cols.free_qty ? "<th>Free</th>" : "") + "<th>Price</th>" +
    (cols.discount ? "<th>Disc</th>" : "") + (cols.tax ? "<th>Tax</th>" : "") + "<th>Amount</th></tr>";
  var rows = "<tr><td>Sample Item</td>" + (cols.qty ? "<td>2</td>" : "") +
    (cols.free_qty ? "<td>1</td>" : "") + "<td>" + fmtRs(50000) + "</td>" +
    (cols.discount ? "<td>" + fmtRs(5000) + "</td>" : "") +
    (cols.tax ? "<td>" + fmtRs(0) + "</td>" : "") + "<td>" + fmtRs(95000) + "</td></tr>";
  var html = '<div style="border-top:6px solid ' + color + '; padding-top:8px">' +
    (s.print_company_info !== false ? "<strong>Your Business</strong><br>" : "") +
    '<div style="text-align:center; font-weight:800; color:' + color + '">' + esc(name) + "</div>" +
    "<table>" + head + rows + "</table>" +
    '<div style="text-align:right">Total: <strong>' + fmtRs(95000) + "</strong></div>" +
    (s.print_amount_words ? "<div>Rupees Ninety Five Only</div>" : "") +
    (s.print_terms ? "<div>Terms &amp; Conditions apply.</div>" : "") +
    (s.print_signature ? "<div style='text-align:right'>" + esc(s.signature_text || "For Your Business") + "</div>" : "") +
    '<div class="sub">Theme: ' + esc(s.print_theme || "Tally Theme") + " · " +
    esc(s.print_paper_size || "A4") + " · " + esc(s.print_orientation || "Portrait") + "</div></div>";
  $("printPreview").innerHTML = html;
}
$("btnItemTableCustom").addEventListener("click", function () {
  var s = QB.settings || {};
  var cols = s.print_item_columns || {};
  var labels = { qty: "Quantity", free_qty: "Free Quantity",
    discount: "Discount", tax: "Tax" };
  var html = "<p>Choose the columns printed in the bill's item table.</p>" +
    Object.keys(labels).map(function (k) {
      return '<label class="check-row"><input type="checkbox" data-col="' + k + '"' +
        (cols[k] ? " checked" : "") + "> <span>" + labels[k] + "</span></label>";
    }).join("") +
    '<button class="btn btn-primary btn-block" id="btnColsSave" type="button">Save columns</button>';
  openDialog("Item Table Customization", html);
  $("btnColsSave").addEventListener("click", function () {
    var out = {};
    document.querySelectorAll("[data-col]").forEach(function (cb) {
      out[cb.getAttribute("data-col")] = cb.checked;
    });
    saveSettings({ print_item_columns: out }).then(function () {
      closeDialog(); renderPrintPreview();
      setMsg("appMsg", "Item table columns saved.");
    });
  });
});
document.addEventListener("change", function (ev) {
  var t = ev.target;
  if (!t || !t.getAttribute) return;
  var s = QB.settings || {};
  function savePartyExtras() {
    var list = (s.party_extra_fields || []).map(function (f, i) {
      var on = document.querySelector('[data-pef-on="' + i + '"]');
      var lab = document.querySelector('[data-pef-label="' + i + '"]');
      var pr = document.querySelector('[data-pef-print="' + i + '"]');
      return { label: lab ? lab.value : f.label, enabled: on ? on.checked : f.enabled,
               show_in_print: pr ? pr.checked : f.show_in_print };
    });
    saveSettings({ party_extra_fields: list });
  }
  function saveItemExtras() {
    var list = (s.item_extra_fields || []).map(function (f, i) {
      var on = document.querySelector('[data-ief-on="' + i + '"]');
      var lab = document.querySelector('[data-ief-label="' + i + '"]');
      return { key: f.key, label: lab ? lab.value : f.label, enabled: on ? on.checked : f.enabled };
    });
    saveSettings({ item_extra_fields: list });
  }
  if (t.hasAttribute("data-pef-on") || t.hasAttribute("data-pef-label") || t.hasAttribute("data-pef-print")) savePartyExtras();
  if (t.hasAttribute("data-ief-on") || t.hasAttribute("data-ief-label")) saveItemExtras();
});
/* Terms & Conditions library */
function loadTermsList() {
  var box = $("termsList");
  if (!box) return;
  apiGet("/api/terms", null).then(function (r) {
    box.innerHTML = (r.body.terms || []).map(function (t) {
      return '<div class="row"><div><div class="main">' + esc(t.header) + '</div><div class="sub">' +
        esc((t.applies || []).map(function (a) { return DOC_TYPE_LABELS[a] || a; }).join(", ")) +
        " — " + esc(t.description) + '</div></div>' +
        '<button class="btn btn-mini" data-term-del="' + t.id + '">Delete</button></div>';
    }).join("") || '<p class="empty">No terms yet.</p>';
    box.querySelectorAll("[data-term-del]").forEach(function (b) {
      b.addEventListener("click", function () {
        apiDelete("/api/terms/" + b.getAttribute("data-term-del")).then(loadTermsList)
          .catch(function (e) { setMsg("appMsg", e.message, true); });
      });
    });
  }).catch(function () {});
}
var docTermsSelected = null;
function loadTermsPicker() {
  var box = $("docTermsPicker");
  if (!box) return;
  apiGet("/api/terms", null).then(function (r) {
    var list = (r.body.terms || []).filter(function (t) {
      return (t.applies || []).indexOf(docType) >= 0;
    });
    if (!list.length) {
      box.innerHTML = '<p class="empty">Standard terms will print. Add named terms in Settings &gt; TRANSACTION.</p>';
      return;
    }
    box.innerHTML = "";
    list.forEach(function (t) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "row" + (docTermsSelected === t.id ? " active-row" : "");
      b.innerHTML = rowHtml(t.header, t.description, docTermsSelected === t.id ? "✓ Selected" : "Tap to use", "");
      b.addEventListener("click", function () {
        docTermsSelected = docTermsSelected === t.id ? null : t.id;
        loadTermsPicker();
      });
      box.appendChild(b);
    });
  }).catch(function () {});
}
$("btnTermNew").addEventListener("click", function () {
  showPanel("termAddPanel");
  $("termHeader").focus();
});
$("btnTermCancel").addEventListener("click", function () { hidePanel("termAddPanel"); });
$("btnTermAdd").addEventListener("click", function () {
  var header = $("termHeader").value.trim();
  if (!header) { setMsg("appMsg", "Give the terms a header.", true); return; }
  apiPost("/api/terms", { header: header, description: $("termDesc").value.trim(),
    applies: [$("termApplies").value] }).then(function () {
      $("termHeader").value = ""; $("termDesc").value = "";
      hidePanel("termAddPanel");
      setMsg("appMsg", "Terms added to the library.");
      loadTermsList(); loadTermsPicker();
    }).catch(function (e) { setMsg("appMsg", e.message, true); });
});

function loadBackups() {
  var locals = localBackups();
  apiGet("/api/backups", "backups").then(function (r) {
    renderBackups(r.body.backups || [], locals);
  }).catch(function () { renderBackups([], locals); });
}
function renderBackups(serverList, locals) {
  var box = $("backupList");
  box.innerHTML = "";
  locals.forEach(function (s) {
    var div = document.createElement("div");
    div.className = "row";
    div.innerHTML = rowHtml("On this device — waiting to upload",
      (s.created_at || "").slice(0, 10), "Pending");
    box.appendChild(div);
  });
  serverList.forEach(function (b) {
    var div = document.createElement("div");
    div.className = "row";
    div.innerHTML = rowHtml("Backup · " + (b.createdAt || "").slice(0, 10),
      b.totals ? "Receivable " + fmtRs(b.totals.totalReceivablePaisa || 0) : "",
      b.emailed ? "Emailed ✓" : "Not emailed");
    var btn = document.createElement("button");
    btn.className = "btn"; btn.type = "button"; btn.textContent = "Restore";
    btn.addEventListener("click", function () {
      if (!confirm("Restore this backup? Current data will be replaced by the backup.")) return;
      apiPost("/api/backups/" + b.id + "/restore", {}).then(function () {
        setMsg("appMsg", "Backup restored.");
        loadBackups(); loadDashboard();
      }).catch(function (e) { setMsg("appMsg", e.message, true); });
    });
    div.appendChild(btn);
    var dl = document.createElement("button");
    dl.className = "btn"; dl.type = "button"; dl.textContent = "Backup To Computer";
    dl.addEventListener("click", function () {
      downloadAuthed("/api/backups/" + b.id + "/download", "qbill-backup.json")
        .catch(function (e) { setMsg("appMsg", e.message, true); });
    });
    div.appendChild(dl);
    box.appendChild(div);
  });
  if (!locals.length && !serverList.length) box.innerHTML = '<p class="empty">No backups yet.</p>';
}
$("btnBackupNow").addEventListener("click", function () {
  if (!navigator.onLine) { buildLocalBackup(new Date().toISOString().slice(0, 10)); loadBackups(); return; }
  apiPost("/api/backups/run", {}).then(function () {
    lsSet("qbill_backup_day", new Date().toISOString().slice(0, 10));
    setMsg("appMsg", "Backup created.");
    loadBackups();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});

/* ------------------------------------------------- owner: email setup -- */
function loadMailConfig() {
  apiGet("/api/admin/mail-config", null).then(function (r) {
    var routes = r.body.routes || {};
    function st(v) { return v ? "Saved ✓" : "Not saved"; }
    $("mailRoutes").textContent =
      "Resend: " + st(routes.resend) + " · Brevo: " + st(routes.brevo) +
      " · Mailjet: " + st(routes.mailjet) +
      (routes.dev_recorder ? " · Dev recorder ON (test mode)" : "");
    $("mkResend").placeholder = st(routes.resend);
    $("mkBrevo").placeholder = st(routes.brevo);
    $("mkMjKey").placeholder = st(routes.mailjet);
    $("mkMjSecret").placeholder = st(routes.mailjet);
    $("mkFromEmail").value = r.body.fromEmail || "";
    $("mkFromName").value = r.body.fromName || "";
  }).catch(function () {});
}
$("btnMailSave").addEventListener("click", function () {
  var payload = {
    from_email: $("mkFromEmail").value.trim(),
    from_name: $("mkFromName").value.trim()
  };
  if ($("mkResend").value) payload.resend_api_key = $("mkResend").value;
  if ($("mkBrevo").value) payload.brevo_api_key = $("mkBrevo").value;
  if ($("mkMjKey").value) payload.mj_api_key = $("mkMjKey").value;
  if ($("mkMjSecret").value) payload.mj_api_secret = $("mkMjSecret").value;
  apiPost("/api/admin/mail-config", payload).then(function () {
    ["mkResend", "mkBrevo", "mkMjKey", "mkMjSecret"].forEach(function (id) { $(id).value = ""; });
    setMsg("mailMsg", "Email settings saved.");
    loadMailConfig();
  }).catch(function (e) { setMsg("mailMsg", e.message, true); });
});
$("btnMailTest").addEventListener("click", function () {
  apiPost("/api/admin/mail-test", { to: $("mailTestTo").value.trim() }).then(function (body) {
    setMsg("mailMsg", "Test email sent via " + (body.route || "a route") + ".");
  }).catch(function (e) { setMsg("mailMsg", e.message, true); });
});

/* ------------------------------------------- owner: customer accounts -- */
/* The owner's selling control: every account, its IPs, and an
   Approve / Block button. A blocked account is also signed out
   server-side, so its next screen is the waiting page (or sign-in). */
function loadAccounts() {
  apiGet("/api/admin/accounts", null).then(function (r) {
    var list = r.body.accounts || [];
    var box = $("accountList");
    box.innerHTML = list.length ? "" : '<p class="empty">No accounts yet.</p>';
    list.forEach(function (a) {
      var div = document.createElement("div");
      div.className = "row";
      div.innerHTML = "<div>" +
        '<div class="main">' + esc(a.email) + "</div>" +
        '<div class="sub">' + (a.approved ? "Active" : "Waiting") + "</div>" +
        (a.rejectReason === "ip_match" ? '<div class="sub">Auto-rejected — same IP</div>' : "") +
        (a.signupIp ? '<div class="sub">Signed up from IP: ' + esc(a.signupIp) + "</div>" : "") +
        (a.lastIp ? '<div class="sub">Last IP: ' + esc(a.lastIp) + "</div>" : "") +
        "</div>";
      if (!a.isOwner) {
        var btn = document.createElement("button");
        btn.className = "btn"; btn.type = "button";
        btn.textContent = a.approved ? "Block" : "Approve";
        btn.addEventListener("click", function () {
          apiPost("/api/admin/accounts/" + (a.approved ? "block" : "approve"),
            { email: a.email }).then(function (body) {
              setMsg("accountsMsg", body.message || "Done.");
              loadAccounts();
            }).catch(function (e) { setMsg("accountsMsg", e.message, true); });
        });
        div.appendChild(btn);
      }
      box.appendChild(div);
    });
  }).catch(function () {});
}

/* ------------------------------------------- header / Add More / keys -- */
function toggleMega(force) {
  var m = $("addMoreMenu");
  var show = typeof force === "boolean" ? force : m.classList.contains("hidden");
  m.classList.toggle("hidden", !show);
}
$("btnAddMore").addEventListener("click", function (ev) { ev.stopPropagation(); toggleMega(); });
document.addEventListener("click", function (ev) {
  if (!$("addMoreMenu").classList.contains("hidden") &&
      !ev.target.closest("#addMoreMenu") && ev.target.id !== "btnAddMore") {
    $("addMoreMenu").classList.add("hidden");
  }
});
$("btnAddSale").addEventListener("click", function () { openDocForm("sale"); });
$("btnAddPurchase").addEventListener("click", function () { openDocForm("purchase"); });
$("btnMenu").addEventListener("click", function () {
  var open = !$("sidebar").classList.contains("open");
  $("sidebar").classList.toggle("open", open);
  $("sideScrim").classList.toggle("hidden", !open);
});
$("sideScrim").addEventListener("click", closeSidebarMobile);
function megaAction(act) {
  $("addMoreMenu").classList.add("hidden");
  if (!me) return;
  if (act.indexOf("doc:") === 0) openDocForm(act.slice(4));
  else if (act === "payin") go("paymentin");
  else if (act === "payout") go("paymentout");
  else if (act === "otherincome") { go("otherincome"); showPanel("incomePanel"); }
  else if (act === "expenses") { go("expenses"); showPanel("expensePanel"); }
}
document.querySelectorAll(".mega-item").forEach(function (b) {
  b.addEventListener("click", function () { megaAction(b.getAttribute("data-act")); });
});
var SHORTCUTS = {
  s: "doc:sale", i: "payin", r: "doc:sale_return", f: "doc:sale_order",
  m: "doc:estimate", d: "doc:challan", p: "doc:purchase", o: "payout",
  l: "doc:purchase_return", g: "doc:purchase_order", e: "expenses",
  n: "doc:proforma", y: "otherincome",
};
document.addEventListener("keydown", function (ev) {
  if (ev.ctrlKey && ev.key === "Enter") {
    ev.preventDefault();
    if (me) toggleMega();
    return;
  }
  if (!ev.altKey || ev.ctrlKey || ev.metaKey) return;
  var tag = (ev.target && ev.target.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
  var act = SHORTCUTS[(ev.key || "").toLowerCase()];
  if (act) { ev.preventDefault(); megaAction(act); }
});
$("searchTx").addEventListener("input", function () {
  if (currentScreen === "book") loadBook();
  else if (currentScreen === "docs") loadDocs();
  else if (currentScreen === "payments") loadPayments();
  else if (currentScreen === "parties") loadParties();
  else if (currentScreen === "items") loadItems();
  else if (currentScreen === "expenses") loadExpenses();
});
$("searchTx").addEventListener("keydown", function (ev) {
  if (ev.key === "Enter" && currentScreen === "dashboard") { ev.preventDefault(); openAnything(); }
});

/* -------------------------------------------------------------- boot --- */
/* A login WITHOUT "Remember me" ends when the page goes away: lock the
   session (best effort, keepalive) so reopening this tab asks for the
   password again. Remembered and code sign-ins stay open on the device. */
window.addEventListener("pagehide", function () {
  var passcodeOn = QB.settings && QB.settings.enable_passcode;
  if (!token()) return;
  if (!lsGet("qbill_token_temp", false) && !passcodeOn) return;
  try {
    fetch("/api/auth/lock", {
      method: "POST", headers: authHeaders(), body: "{}", keepalive: true
    }).catch(function () {});
  } catch (e) {}
});
window.addEventListener("online", function () { updateSyncPill(); syncNow(); });
window.addEventListener("offline", updateSyncPill);
document.addEventListener("visibilitychange", function () {
  if (!document.hidden) { updateSyncPill(); maybeDailyBackup(); }
});
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(function () {});
}
updateSyncPill();
if (token()) refreshMe(); else showAuth();

/* ============================================================ vyapar2 ==
   Dashboard extras, Reports, Stock (godowns/batches), Online Store,
   Loyalty, Accounting, Team & Roles, Utilities, Grow Your Business,
   Invoice Preview, Open Anything, Other Income, Cash & Bank tabs. */

/* ------------------------------------------------------- dashboard --- */
$("saleGraphPeriod").addEventListener("change", renderSaleGraph);
$("btnPrivacy").addEventListener("click", function () {
  QB.privacy = !QB.privacy;
  document.body.classList.toggle("privacy-on", QB.privacy);
  $("btnPrivacy").textContent = QB.privacy ? "👁 Show amounts" : "👁 Hide amounts";
});
document.querySelectorAll("[data-widget]").forEach(function (cb) {
  cb.addEventListener("change", function () {
    var w = (QB.settings && QB.settings.widgets) || {};
    w[cb.getAttribute("data-widget")] = cb.checked;
    saveSettings({ widgets: w }).then(applyWidgets)
      .catch(function (e) { setMsg("appMsg", e.message, true); });
  });
});
$("expiringRange").addEventListener("change", function () {
  // The server returns the ~30-day window; the range re-slices client-side
  // by expiry month distance for 7 / 30 / 90 day views.
  var days = parseInt($("expiringRange").value, 10) || 30;
  var exp = (dashData && dashData.expiringItems) || [];
  var now = new Date();
  var filtered = exp.filter(function (e) {
    if (!e.expDate) return true;
    var parts = e.expDate.split("/");
    if (parts.length !== 2) return true;
    var expDate = new Date(2000 + parseInt(parts[1], 10), parseInt(parts[0], 10), 0);
    return (expDate - now) <= days * 86400000;
  });
  $("dExpiringList").innerHTML = filtered.length ? filtered.slice(0, 8).map(function (e) {
    return '<div class="row">' + rowHtml(e.itemName, "Batch " + (e.batchNo || "—") + " · " + e.qty + " left",
      '<div class="amt">' + e.expDate + "</div>", "") + "</div>";
  }).join("") : '<p class="empty">No stock is expiring in this range.</p>';
});
document.querySelectorAll("[data-report-link]").forEach(function (b) {
  b.addEventListener("click", function () { openReports(b.getAttribute("data-report-link")); });
});
/* Home > Most Used Reports (audit 2026-11-10, research §7): the strip
   links the 2026 Vyapar set — Sale Report (the Sale book), Daybook
   Report (the Day Book), Party Statement (Parties > Ledger), View All. */
document.querySelectorAll("[data-most]").forEach(function (b) {
  b.addEventListener("click", function () {
    var k = b.getAttribute("data-most");
    if (k === "sale") go("salebook");
    else if (k === "daybook") go("daybook");
    else if (k === "party") go("parties");
    else openReports("all-transactions");
  });
});
$("docBarcode").addEventListener("keydown", function (ev) {
  if (ev.key !== "Enter") return;
  ev.preventDefault();
  var code = $("docBarcode").value.trim().toLowerCase();
  if (!code) return;
  var items = ((cacheGet("items") || {}).items) || [];
  var found = items.filter(function (i) {
    return (i.itemCode || "").toLowerCase() === code || i.name.toLowerCase() === code;
  })[0];
  if (found) {
    addSaleLine(found.id);
    $("docBarcode").value = "";
  } else {
    setMsg("appMsg", "No item found for code " + code + ".", true);
  }
});

/* --------------------------------------------------------- reports --- */
var REPORT_DEFS = [
  { key: "all-transactions", title: "All Transactions", path: "/api/reports/all-transactions", source: "entries",
    cols: [["date", "Date"], ["kind", "Type"], ["ref", "Number"], ["party", "Party"],
           ["amountPaisa", "Amount", "money"], ["paidPaisa", "Paid", "money"], ["balancePaisa", "Balance", "money"]] },
  { key: "bill-wise-profit", title: "Bill Wise Profit", path: "/api/reports/bill-wise-profit",
    cols: [["date", "Date"], ["invoiceNo", "Number"], ["party", "Party"], ["salePaisa", "Sale", "money"],
           ["costPaisa", "Cost", "money"], ["profitPaisa", "Profit", "money"]] },
  { key: "cash-flow", title: "Cash Flow", path: "/api/reports/cash-flow",
    cols: [["date", "Date"], ["inPaisa", "In", "money"], ["outPaisa", "Out", "money"],
           ["netPaisa", "Net", "money"], ["balancePaisa", "Balance", "money"]] },
  { key: "party-pnl", title: "Party wise P&L", path: "/api/reports/party-pnl",
    cols: [["party", "Party"], ["salesPaisa", "Sales", "money"], ["returnsPaisa", "Returns", "money"],
           ["costPaisa", "Cost", "money"], ["profitPaisa", "Profit", "money"], ["balancePaisa", "Balance", "money"]] },
  { key: "all-parties", title: "All Parties", path: "/api/reports/all-parties", source: "parties",
    cols: [["name", "Party"], ["phone", "Phone"], ["groupName", "Group"], ["status", "Status"],
           ["balancePaisa", "Balance", "money"], ["loyaltyPoints", "Loyalty Points"]] },
  { key: "party-report-by-item", title: "Party Report By Item", path: "/api/reports/party-report-by-item",
    cols: [["party", "Party"], ["item", "Item"], ["qty", "Qty"], ["amountPaisa", "Amount", "money"]] },
  { key: "sale-purchase-by-party", title: "Sale Purchase By Party", path: "/api/reports/sale-purchase-by-party",
    cols: [["party", "Party"], ["salePaisa", "Sales", "money"], ["purchasePaisa", "Purchases", "money"]] },
  { key: "item-report-by-party", title: "Item Report By Party", path: "/api/reports/item-report-by-party",
    cols: [["item", "Item"], ["party", "Party"], ["qty", "Qty"], ["amountPaisa", "Amount", "money"]] },
  { key: "item-wise-pnl", title: "Item Wise P&L", path: "/api/reports/item-wise-pnl",
    cols: [["item", "Item"], ["soldQty", "Qty Sold"], ["revenuePaisa", "Sales", "money"],
           ["costPaisa", "Cost", "money"], ["profitPaisa", "Profit", "money"], ["stockQty", "Stock"]] },
  { key: "item-detail", title: "Item Detail", path: "/api/reports/item-detail",
    cols: [["date", "Date"], ["kind", "Kind"], ["ref", "Detail"], ["qty", "Qty"],
           ["amountPaisa", "Amount", "money"], ["status", "STATUS"]] },
  { key: "item-wise-discount", title: "Item Wise Discount", path: "/api/reports/item-wise-discount",
    cols: [["item", "Item"], ["qty", "Qty"], ["discountPaisa", "Discount Given", "money"]] },
  { key: "discount-report", title: "Discount Report", path: "/api/reports/discount-report", totalKey: "totalDiscountPaisa",
    cols: [["date", "Date"], ["invoiceNo", "Number"], ["type", "Type"], ["party", "Party"],
           ["discountPaisa", "Discount", "money"]] },
  { key: "tax-report", title: "Tax Report", path: "/api/reports/tax-report", totalKey: "totalTaxPaisa",
    cols: [["type", "Type"], ["documents", "Bills"], ["taxablePaisa", "Taxable Amount", "money"], ["taxPaisa", "Tax", "money"]] },
  { key: "tax-rate-report", title: "Tax Rate Report", path: "/api/reports/tax-rate-report",
    cols: [["taxRatePct", "Tax %"], ["taxablePaisa", "Taxable Amount", "money"], ["taxPaisa", "Tax", "money"]] },
  { key: "low-stock", title: "Low Stock Summary", path: "/api/reports/low-stock",
    cols: [["item", "Item"], ["stockQty", "Stock"], ["minStock", "Min Stock To Maintain"]] },
  { key: "item-batch", title: "Item Batch Report", path: "/api/reports/item-batch",
    cols: [["item", "Item"], ["batchNo", "Batch No."], ["qty", "Qty"], ["expDate", "Expiry"], ["mfgDate", "Mfg"]] },
  { key: "stock-detail", title: "Stock Detail", path: "/api/reports/stock-detail",
    cols: [["item", "Item"], ["godown", "Godown"], ["qty", "Qty"], ["valuePaisa", "Value", "money"]] },
  { key: "expense", title: "Expense Report", path: "/api/reports/expense", totalKey: "totalPaisa",
    cols: [["createdAt", "Date"], ["title", "Title"], ["category", "Category"], ["categoryType", "Type"],
           ["expenseItem", "Expense Item"], ["amountPaisa", "Amount", "money"],
           ["paidPaisa", "Paid", "money"], ["balancePaisa", "Balance", "money"]] },
  { key: "expense-category", title: "Expense Category Report", path: "/api/reports/expense-category",
    cols: [["name", "Category"], ["type", "Type"], ["count", "Count"], ["amountPaisa", "Total", "money"]] },
  { key: "expense-item", title: "Expense Item Report", path: "/api/reports/expense-item",
    cols: [["name", "Expense Item"], ["count", "Count"], ["amountPaisa", "Total", "money"]] },
  { key: "other-income-report", title: "Other Income Report", path: "/api/reports/other-income", totalKey: "totalPaisa",
    cols: [["name", "Category"], ["count", "Entries"], ["amountPaisa", "Amount", "money"]] },
  { key: "store-report", title: "Store Reports", path: "/api/reports/store",
    cols: [["status", "Status"], ["orders", "Orders"], ["amountPaisa", "Amount", "money"]] },
  { key: "bank-statement", title: "Bank Statement", path: "/api/reports/bank-statement",
    cols: [["date", "Date"], ["note", "Note"], ["depositPaisa", "Deposit", "money"],
           ["withdrawPaisa", "Withdraw", "money"], ["balancePaisa", "Balance", "money"]] },
  { key: "sales-graph", title: "Sales Graph", path: "/api/reports/sales-graph", source: "points",
    cols: [["label", "Period"], ["amountPaisa", "Sales", "money"]] },
  { key: "trial-balance", title: "Trial Balance", path: "/api/reports/trial-balance",
    cols: [["account", "Account"], ["group", "Group"], ["debitPaisa", "Debit", "money"], ["creditPaisa", "Credit", "money"]] },
  { key: "item-serial", title: "Item Serial Report", path: "/api/reports/item-serial",
    cols: [["date", "Date"], ["invoiceNo", "Number"], ["item", "Item"], ["serialNo", "Serial No./IMEI"], ["party", "Party"], ["qty", "Qty"]] },
  { key: "sale-orders", title: "Sale Orders", path: "/api/reports/sale-orders",
    cols: [["date", "Date"], ["invoiceNo", "Number"], ["party", "Party"], ["amountPaisa", "Amount", "money"], ["status", "Status"]] },
  { key: "sale-order-items", title: "Sale Order Item Report", path: "/api/reports/sale-order-items",
    cols: [["date", "Date"], ["invoiceNo", "Number"], ["item", "Item"], ["party", "Party"], ["qty", "Qty"], ["ratePaisa", "Rate", "money"], ["amountPaisa", "Amount", "money"]] },
  { key: "loans", title: "Loan Accounts", path: "/api/loans", source: "loans",
    cols: [["name", "Loan"], ["lender", "Lender"], ["principalPaisa", "Loan Amount", "money"], ["paidPaisa", "Paid", "money"], ["balancePaisa", "Balance", "money"]] },
];
/* Report groups (delta #13) — the video's menu groups, Pakistan-adapted
   (no GST group; tax reports live under Business Status). */
var REPORT_GROUPS = {
  "all-transactions": "Transaction report", "bill-wise-profit": "Transaction report",
  "cash-flow": "Transaction report", "trial-balance": "Transaction report",
  "discount-report": "Transaction report",
  "party-pnl": "Party report", "all-parties": "Party report",
  "party-report-by-item": "Party report", "sale-purchase-by-party": "Party report",
  "item-wise-pnl": "Item-Stock report", "item-report-by-party": "Item-Stock report",
  "item-detail": "Item-Stock report", "item-wise-discount": "Item-Stock report",
  "item-batch": "Item-Stock report", "item-serial": "Item-Stock report",
  "low-stock": "Item-Stock report", "stock-detail": "Item-Stock report",
  "tax-report": "Business Status", "tax-rate-report": "Business Status",
  "sales-graph": "Business Status", "bank-statement": "Business Status",
  "store-report": "Business Status",
  "expense": "Expense report", "expense-category": "Expense report",
  "expense-item": "Expense report", "other-income-report": "Expense report",
  "sale-orders": "Sale Order report", "sale-order-items": "Sale Order report",
  "loans": "Loan Accounts"
};
var REPORT_GROUP_ORDER = ["Transaction report", "Party report", "Item-Stock report",
  "Business Status", "Expense report", "Sale Order report", "Loan Accounts"];
var currentReport = "all-transactions";
function reportDef(key) {
  return REPORT_DEFS.filter(function (d) { return d.key === key; })[0] || REPORT_DEFS[0];
}
function openReports(key) {
  currentReport = key || currentReport || "all-transactions";
  navKey = currentReport === "sales-graph" ? "salesgraph" : "reports";
  showScreen("reports");
  renderReportChips();
  loadReport();
}
function renderReportChips() {
  var html = "";
  REPORT_GROUP_ORDER.forEach(function (g) {
    var defs = REPORT_DEFS.filter(function (d) { return (REPORT_GROUPS[d.key] || "Reports") === g; });
    if (!defs.length) return;
    html += '<div class="rep-group">' + esc(g) + "</div>";
    html += defs.map(function (d) {
      return '<button class="fchip' + (d.key === currentReport ? " active" : "") +
        '" data-report="' + d.key + '" type="button">' + esc(d.title) + "</button>";
    }).join("");
  });
  $("reportChips").innerHTML = html;
  $("reportChips").querySelectorAll("[data-report]").forEach(function (b) {
    b.addEventListener("click", function () {
      currentReport = b.getAttribute("data-report");
      renderReportChips();
      loadReport();
    });
  });
}
var lastReportDef = null;
var lastReportRows = [];
function loadReport() {
  var def = reportDef(currentReport);
  $("reportTitle").textContent = def.title;
  $("repTxTools").classList.toggle("hidden", def.key !== "all-transactions");
  $("repTbOptions").classList.toggle("hidden", def.key !== "trial-balance");
  var f = $("repFrom").value, t = $("repTo").value;
  var url = def.path;
  var qs = [];
  if (f) qs.push("from=" + f);
  if (t) qs.push("to=" + t);
  if (def.key === "sales-graph") qs.push("period=" + $("graphPeriod").value);
  if (def.key === "bank-statement" && selectedBankId) qs.push("bank_id=" + selectedBankId);
  if (def.key === "trial-balance") {
    qs.push("working=" + ($("tbWorking").checked ? "1" : "0"));
    qs.push("zero=" + ($("tbZero").checked ? "1" : "0"));
  }
  if (qs.length) url += "?" + qs.join("&");
  apiGet(url, null).then(function (r) {
    var rows = def.source ? (r.body[def.source] || []) : (r.body.rows || []);
    rows = rows.map(function (row) {
      var copy = {};
      Object.keys(row).forEach(function (k) { copy[k] = row[k]; });
      if (copy.date && copy.date.length > 10) copy.date = copy.date.slice(0, 10);
      if (copy.createdAt && copy.createdAt.length > 10) copy.createdAt = copy.createdAt.slice(0, 10);
      return copy;
    });
    var cols = def.cols;
    if (def.key === "all-transactions") {
      var kindFilter = $("repTypeFilter").value;
      if (kindFilter) rows = rows.filter(function (row) { return row.kind === kindFilter; });
    }
    if (def.key === "trial-balance") {
      if ($("tbWorking").checked) {
        cols = cols.concat([["journalDebitPaisa", "Journal Debit", "money"],
                            ["journalCreditPaisa", "Journal Credit", "money"]]);
      }
      if ($("tbCollapse").checked) {
        var byGroup = {};
        rows.forEach(function (row) {
          var g = row.group || "Other";
          byGroup[g] = byGroup[g] || { account: g, group: "", debitPaisa: 0, creditPaisa: 0,
                                       journalDebitPaisa: 0, journalCreditPaisa: 0 };
          byGroup[g].debitPaisa += row.debitPaisa || 0;
          byGroup[g].creditPaisa += row.creditPaisa || 0;
          byGroup[g].journalDebitPaisa += row.journalDebitPaisa || 0;
          byGroup[g].journalCreditPaisa += row.journalCreditPaisa || 0;
        });
        rows = Object.keys(byGroup).map(function (k) { return byGroup[k]; });
      }
    }
    lastReportDef = def;
    lastReportRows = rows;
    $("reportHead").innerHTML = "<tr>" + cols.map(function (c) {
      return "<th" + (c[2] === "money" ? ' class="num"' : "") + ">" + esc(c[1]) + "</th>";
    }).join("") + "</tr>";
    $("reportBody").innerHTML = rows.map(function (row) {
      return "<tr>" + cols.map(function (c) {
        var v = row[c[0]];
        if (c[2] === "money") return '<td class="num">' + fmtRs(v || 0) + "</td>";
        return "<td>" + esc(v == null ? "" : v) + "</td>";
      }).join("") + "</tr>";
    }).join("");
    $("reportEmpty").classList.toggle("hidden", rows.length > 0);
    if (def.key === "all-transactions") {
      var tAmt = 0, tBal = 0;
      rows.forEach(function (row) { tAmt += row.amountPaisa || 0; tBal += row.balancePaisa || 0; });
      $("reportTotal").textContent = "Total Amount: " + fmtRs(tAmt) + " · Balance: " + fmtRs(tBal);
    } else if (def.key === "trial-balance") {
      $("reportTotal").textContent = "Total Debit: " + fmtRs(r.body.totalDebitPaisa || 0) +
        " · Total Credit: " + fmtRs(r.body.totalCreditPaisa || 0);
    } else {
      var total = def.totalKey ? r.body[def.totalKey] : r.body.totalPaisa;
      $("reportTotal").textContent = total != null ? ("Total: " + fmtRs(total)) : "";
    }
    if (def.key === "sales-graph") {
      $("reportGraphWrap").classList.remove("hidden");
      drawReportGraph(rows);
    } else {
      $("reportGraphWrap").classList.add("hidden");
    }
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
$("repTypeFilter").addEventListener("change", function () {
  if (currentReport === "all-transactions") loadReport();
});
[$("tbWorking"), $("tbZero"), $("tbCollapse")].forEach(function (el) {
  el.addEventListener("change", function () {
    if (currentReport === "trial-balance") loadReport();
  });
});
$("btnRepExcel").addEventListener("click", function () {
  if (!lastReportDef) return;
  var cols = lastReportDef.cols;
  var lines = [cols.map(function (c) { return c[1]; }).join(",")];
  lastReportRows.forEach(function (row) {
    lines.push(cols.map(function (c) {
      var v = row[c[0]];
      if (c[2] === "money") v = ((v || 0) / 100).toFixed(2);
      v = v == null ? "" : String(v);
      return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
    }).join(","));
  });
  var blob = new Blob([lines.join("\n")], { type: "text/csv" });
  var a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = lastReportDef.key + ".csv";
  a.click();
  setTimeout(function () { URL.revokeObjectURL(a.href); }, 4000);
});
$("btnRepPrint").addEventListener("click", function () {
  if (!lastReportDef) return;
  var cols = lastReportDef.cols;
  var w = window.open("", "_blank");
  if (!w) { setMsg("appMsg", "Allow pop-ups to print the report.", true); return; }
  var html = "<h2>" + esc(lastReportDef.title) + "</h2><table border='1' cellspacing='0' cellpadding='4'>" +
    "<tr>" + cols.map(function (c) { return "<th>" + esc(c[1]) + "</th>"; }).join("") + "</tr>" +
    lastReportRows.map(function (row) {
      return "<tr>" + cols.map(function (c) {
        var v = row[c[0]];
        return "<td>" + esc(c[2] === "money" ? fmtRs(v || 0) : (v == null ? "" : String(v))) + "</td>";
      }).join("") + "</tr>";
    }).join("") + "</table>";
  w.document.write(html);
  w.document.close();
  w.print();
});
function drawReportGraph(rows) {
  var svg = $("reportGraph");
  if (!rows.length) { svg.innerHTML = ""; return; }
  var W = 640, H = 180, padL = 8, padR = 8, padT = 14, padB = 22;
  var max = Math.max.apply(null, rows.map(function (r) { return r.amountPaisa; }).concat([1]));
  var step = rows.length > 1 ? (W - padL - padR) / (rows.length - 1) : 0;
  var coords = rows.map(function (r, i) {
    return { x: padL + step * i, y: padT + (H - padT - padB) * (1 - r.amountPaisa / max), label: r.label };
  });
  var line = coords.map(function (c, i) { return (i === 0 ? "M" : "L") + c.x.toFixed(1) + "," + c.y.toFixed(1); }).join(" ");
  svg.innerHTML = '<path d="' + line + '" fill="none" stroke="#f26522" stroke-width="2.5" stroke-linejoin="round"/>' +
    coords.map(function (c) {
      return '<circle cx="' + c.x.toFixed(1) + '" cy="' + c.y.toFixed(1) + '" r="4" fill="#fff" stroke="#f26522" stroke-width="2"/>';
    }).join("");
}
$("btnRepGo").addEventListener("click", loadReport);
$("graphPeriod").addEventListener("change", loadReport);

/* ------------------------------------------------- stock: godowns ---- */
function loadGodowns() {
  apiGet("/api/godowns", null).then(function (r) {
    var list = r.body.godowns || [];
    $("godownList").innerHTML = list.map(function (g) {
      return '<div class="row"><div><div class="main">' + esc(g.name) +
        (g.isMain ? " (Main)" : "") + '</div><div class="sub">' + esc(g.type || "Godown") +
        " · " + g.totalQty + ' units in stock</div></div>' +
        '<button class="btn btn-mini" data-godown-stock="' + g.id + '">Stock Summary</button>' +
        (g.isMain ? "" : '<button class="btn btn-mini" data-godown-del="' + g.id + '">Delete</button>') + '</div>';
    }).join("") || '<p class="empty">No godowns yet.</p>';
    $("godownList").querySelectorAll("[data-godown-del]").forEach(function (b) {
      b.addEventListener("click", function () {
        var g = list.filter(function (x) { return String(x.id) === b.getAttribute("data-godown-del"); })[0];
        deleteRecord("godown '" + (g ? g.name : "") + "'",
          "A godown holding stock, or one named in past transfers, cannot be deleted - you will see the reason if it is refused.",
          "/api/godowns/" + b.getAttribute("data-godown-del"), loadGodowns);
      });
    });
    $("godownList").querySelectorAll("[data-godown-stock]").forEach(function (b) {
      b.addEventListener("click", function () {
        var gid = b.getAttribute("data-godown-stock");
        var g = list.filter(function (x) { return String(x.id) === gid; })[0];
        apiGet("/api/godowns/" + gid + "/stock", null).then(function (sr) {
          $("godownStockCard").classList.remove("hidden");
          $("godownStockTitle").textContent = "Stock Summary — " + (g ? g.name : "");
          $("godownStockList").innerHTML = (sr.body.items || []).map(function (srow) {
            return '<div class="row">' + rowHtml(srow.name, "", "Qty: " + srow.qty) + "</div>";
          }).join("") || '<p class="empty">No stock in this godown.</p>';
        });
      });
    });
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
$("godownForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  apiPost("/api/godowns", { name: $("godownName").value.trim(), type: $("godownType").value })
    .then(function () {
      $("godownForm").reset();
      hidePanel("godownPanel");
      setMsg("appMsg", "Godown added.");
      loadGodowns();
    }).catch(function (e) { setMsg("appMsg", e.message, true); });
});
$("btnAddGodown").addEventListener("click", function () {
  showPanel("godownPanel");
  $("godownName").focus();
});
$("btnGodownBack").addEventListener("click", function () { hidePanel("godownPanel"); });
function loadTransfers() {
  Promise.all([apiGet("/api/godowns", null), apiGet("/api/items", "items"), apiGet("/api/stock-transfers", null)])
    .then(function (res) {
      var godowns = res[0].body.godowns || [];
      var items = res[1].body.items || [];
      var opts = godowns.map(function (g) { return '<option value="' + g.id + '">' + esc(g.name) + "</option>"; }).join("");
      $("trFrom").innerHTML = opts;
      $("trTo").innerHTML = opts;
      $("trItem").innerHTML = items.map(function (i) {
        return '<option value="' + i.id + '">' + esc(i.name) + "</option>";
      }).join("");
      var list = (res[2].body.transfers || []).slice().reverse();
      $("transferList").innerHTML = list.map(function (t) {
        return '<div class="row">' + rowHtml(t.itemName + " × " + t.qty,
          (t.fromGodown || "") + " → " + (t.toGodown || "") + " · " + (t.createdAt || "").slice(0, 10) +
          (t.note ? " · " + t.note : ""), null, "") + "</div>";
      }).join("") || '<p class="empty">No transfers yet.</p>';
    }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
$("transferForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  apiPost("/api/stock-transfers", {
    item_id: parseInt($("trItem").value, 10),
    qty: parseInt($("trQty").value || "0", 10),
    from_godown_id: parseInt($("trFrom").value, 10),
    to_godown_id: parseInt($("trTo").value, 10),
    note: $("trNote").value.trim()
  }).then(function () {
    hidePanel("transferPanel");
    setMsg("appMsg", "Stock transferred.");
    loadTransfers();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});
$("btnAddTransfer").addEventListener("click", function () { showPanel("transferPanel"); });
$("btnTransferBack").addEventListener("click", function () { hidePanel("transferPanel"); });

/* --------------------------------------------------- item detail ----- */
var detailItem = null;
function openItemDetail(item) {
  detailItem = item;
  document.querySelectorAll(".screen").forEach(function (s) { s.classList.add("hidden"); });
  $("screen-itemdetail").classList.remove("hidden");
  currentScreen = "itemdetail";
  $("viewTitle").textContent = "Item Detail — " + item.name;
  $("itemDetailTitle").textContent = item.name;
  $("itemDetailSub").textContent =
    "Sale " + fmtRs(item.salePricePaisa) + " · Purchase " + fmtRs(item.purchasePricePaisa) +
    " · Stock " + item.stockQty + (item.itemCode ? " · Code " + item.itemCode : "") +
    (item.location ? " · " + item.location : "");
  loadItemBatches();
  apiGet("/api/reports/item-detail?item_id=" + item.id, null).then(function (r) {
    $("itemTxBody").innerHTML = (r.body.rows || []).map(function (row) {
      return "<tr><td>" + esc((row.date || "").slice(0, 10)) + "</td><td>" + esc(row.kind || "") + "</td><td>" +
        esc(row.ref || row.batchNo || "") + "</td><td>" + row.qty + "</td><td>" +
        fmtRs(row.amountPaisa || 0) + "</td><td>" + esc(row.status || "") + "</td></tr>";
    }).join("");
    $("itemTxEmpty").classList.toggle("hidden", (r.body.rows || []).length > 0);
  }).catch(function () {});
}
function loadItemBatches() {
  if (!detailItem) return;
  apiGet("/api/items/" + detailItem.id + "/batches", null).then(function (r) {
    $("batchList").innerHTML = (r.body.batches || []).map(function (b) {
      return '<div class="row">' + rowHtml("Batch " + (b.batchNo || "—"),
        "Exp " + (b.expDate || "—") + (b.mfgDate ? " · Mfg " + b.mfgDate : ""), "Qty: " + b.qty) + "</div>";
    }).join("") || '<p class="empty">No batches for this item.</p>';
  }).catch(function () {});
}
$("btnItemBack").addEventListener("click", function () { showScreen("items"); });
$("btnAddBatch").addEventListener("click", function () {
  showPanel("batchAddPanel");
  $("batchNo").focus();
});
$("btnBatchCancel").addEventListener("click", function () { hidePanel("batchAddPanel"); });
$("btnBatchAdd").addEventListener("click", function () {
  if (!detailItem) return;
  var qty = parseInt($("batchQty").value || "0", 10);
  if (!$("batchNo").value.trim() || qty <= 0) { setMsg("appMsg", "Batch number and quantity are needed.", true); return; }
  apiPost("/api/items/" + detailItem.id + "/batches", {
    batch_no: $("batchNo").value.trim(), qty: qty,
    exp_date: $("batchExp").value.trim(), mfg_date: $("batchMfg").value.trim()
  }).then(function () {
    $("batchNo").value = ""; $("batchQty").value = ""; $("batchExp").value = ""; $("batchMfg").value = "";
    hidePanel("batchAddPanel");
    setMsg("appMsg", "Batch stock added.");
    loadItemBatches();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});

/* ------------------------------------------------- items bulk tools -- */
$("lnkItemTemplate").setAttribute("href", "/api/utilities/import-template?kind=items");
$("lnkPartyTemplate").setAttribute("href", "/api/utilities/import-template?kind=parties");
var importRowsCache = null;
$("btnBulkImport").addEventListener("click", function () {
  $("bulkImportBox").classList.toggle("hidden");
});
$("btnBulkUpdate").addEventListener("click", function () {
  $("bulkUpdateBox").classList.toggle("hidden");
  apiGet("/api/items", "items").then(renderBulkUpdateGrid);
});
$("btnBulkSheet").addEventListener("click", function () {
  $("bulkSheetBox").classList.toggle("hidden");
});
$("importFile").addEventListener("change", function () {
  var file = $("importFile").files[0];
  if (!file) { setMsg("appMsg", "Choose a CSV file first.", true); return; }
  var reader = new FileReader();
  reader.onload = function () {
    var lines = String(reader.result || "").split(/\r?\n/).filter(function (l) { return l.trim(); });
    if (lines.length < 2) { setMsg("appMsg", "The file has no data rows.", true); return; }
    var headers = lines[0].split(",").map(function (h) { return h.trim(); });
    var rows = lines.slice(1).map(function (line) {
      var cells = line.split(",");
      var obj = {};
      headers.forEach(function (h, i) { obj[h] = (cells[i] || "").trim(); });
      return obj;
    });
    apiPost("/api/utilities/import/validate", { kind: $("importKind").value, rows: rows })
      .then(function (body) {
        importRowsCache = body;
        renderImportResult();
      }).catch(function (e) { setMsg("appMsg", e.message, true); });
  };
  reader.readAsText(file);
});
var importTab = "valid";
document.querySelectorAll("[data-importtab]").forEach(function (b) {
  b.addEventListener("click", function () {
    importTab = b.getAttribute("data-importtab");
    document.querySelectorAll("[data-importtab]").forEach(function (x) {
      x.classList.toggle("active", x === b);
    });
    renderImportResult();
  });
});
function renderImportResult() {
  if (!importRowsCache) return;
  var valid = importRowsCache.valid || [], errors = importRowsCache.errors || [];
  $("importValidCount").textContent = valid.length;
  $("importErrorCount").textContent = errors.length;
  var impNoun = $("importKind").value === "items" ? "Items" : "Parties";
  document.querySelectorAll(".imp-noun").forEach(function (s) { s.textContent = impNoun; });
  /* Audit 2026-11-10: btnImportGo carried class "hidden" in the markup
     and nothing ever removed it — the built-in Bulk Import could never
     be confirmed. It now appears whenever valid rows exist. */
  $("btnImportGo").classList.toggle("hidden", !valid.length);
  if (importTab === "valid") {
    $("importResult").innerHTML = valid.slice(0, 50).map(function (d, i) {
      return '<div class="row">' + rowHtml("Row " + (i + 2) + " — " + (d.name || "Row"),
        "Ready to import", null, "") + "</div>";
    }).join("") || '<p class="empty">Nothing here.</p>';
  } else {
    $("importResult").innerHTML = errors.slice(0, 50).map(function (r) {
      return '<div class="row">' + rowHtml("Row " + r.row, r.message || "Error", null, "") + "</div>";
    }).join("") || '<p class="empty">Nothing here.</p>';
  }
  $("btnImportGo").textContent = "Import " + valid.length + " Valid " +
    ($("importKind").value === "items" ? "Items" : "Parties");
  $("btnImportGo").disabled = !valid.length;
}
$("btnImportGo").addEventListener("click", function () {
  if (!importRowsCache || !(importRowsCache.valid || []).length) return;
  var batch = "imp" + Date.now();
  apiPost("/api/utilities/import/confirm", {
    kind: $("importKind").value, batch: batch,
    rows: importRowsCache.valid
  }).then(function (body) {
    setMsg("appMsg", "Imported " + body.imported + " rows.");
    importRowsCache = null;
    $("importResult").innerHTML = "";
    loadItems(); loadParties();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});
function renderBulkUpdateGrid() {
  var items = ((cacheGet("items") || {}).items) || [];
  $("bulkUpdateBody").innerHTML = items.map(function (i) {
    return "<tr><td>" + esc(i.name) + '</td>' +
      '<td><input data-bu-sale="' + i.id + '" type="number" step="0.01" value="' + (i.salePricePaisa / 100).toFixed(2) + '"></td>' +
      '<td><input data-bu-purchase="' + i.id + '" type="number" step="0.01" value="' + (i.purchasePricePaisa / 100).toFixed(2) + '"></td>' +
      '<td><input data-bu-stock="' + i.id + '" type="number" step="1" value="' + i.stockQty + '"></td></tr>';
  }).join("");
}
$("btnBulkUpdateSave").addEventListener("click", function () {
  var updates = [];
  document.querySelectorAll("[data-bu-sale]").forEach(function (inp) {
    var id = inp.getAttribute("data-bu-sale");
    updates.push({
      id: parseInt(id, 10),
      sale_price_paisa: rsToPaisa(inp.value),
      purchase_price_paisa: rsToPaisa(document.querySelector('[data-bu-purchase="' + id + '"]').value),
      stock_qty: parseInt(document.querySelector('[data-bu-stock="' + id + '"]').value || "0", 10)
    });
  });
  if (!updates.length) { setMsg("appMsg", "Load the grid first.", true); return; }
  apiPost("/api/items/bulk-update", { updates: updates }).then(function (body) {
    setMsg("appMsg", "Updated " + body.updated + " items.");
    loadItems();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});
$("btnBulkSheetRow").addEventListener("click", function () {
  var row = document.createElement("div");
  row.className = "grid2";
  row.innerHTML = '<input class="bs-name" placeholder="Item name">' +
    '<input class="bs-sale" type="number" step="0.01" placeholder="Sale price (Rs)">' +
    '<input class="bs-purchase" type="number" step="0.01" placeholder="Purchase price (Rs)">' +
    '<input class="bs-stock" type="number" step="1" placeholder="Opening stock">';
  $("bulkSheetRows").appendChild(row);
});
$("btnBulkSheetSave").addEventListener("click", function () {
  var rows = [];
  document.querySelectorAll("#bulkSheetRows > div").forEach(function (row) {
    var name = row.querySelector(".bs-name").value.trim();
    if (!name) return;
    rows.push({ name: name, sale_price_paisa: rsToPaisa(row.querySelector(".bs-sale").value),
      purchase_price_paisa: rsToPaisa(row.querySelector(".bs-purchase").value),
      stock_qty: parseInt(row.querySelector(".bs-stock").value || "0", 10) || 0 });
  });
  if (!rows.length) { setMsg("appMsg", "Add at least one item row.", true); return; }
  var chain = Promise.resolve(), made = 0;
  rows.forEach(function (r) {
    chain = chain.then(function () {
      return apiCreate("item", r).then(function () { made += 1; });
    });
  });
  chain.then(function () {
    $("bulkSheetRows").innerHTML = "";
    setMsg("appMsg", made + " items added.");
    loadItems();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});

/* ---------------------------------------------------- other income --- */
function loadOtherIncome() {
  fillPartySelects();
  apiGet("/api/income-categories", null).then(function (r) {
    $("incCategory").innerHTML = '<option value="">— None —</option>' + (r.body.categories || []).map(function (c) {
      return "<option>" + esc(c.name) + "</option>";
    }).join("");
  }).catch(function () {});
  apiGet("/api/other-income", null).then(function (r) {
    var list = (r.body.incomes || []).slice().reverse();
    $("incomeList").innerHTML = list.length ? "" : '<p class="empty">No other income yet.</p>';
    list.forEach(function (inc) {
      var div = document.createElement("div");
      div.className = "row";
      div.innerHTML = rowHtml(inc.title,
        (inc.category ? inc.category + " · " : "") + (inc.createdAt || "").slice(0, 10),
        fmtRs(inc.amountPaisa), "pos");
      var iacts = document.createElement("div");
      iacts.className = "row-actions";
      iacts.appendChild(miniBtn("Delete", function () {
        deleteRecord("income '" + inc.title + "' (" + fmtRs(inc.amountPaisa) + ")",
          "It moves to the Recycle Bin (Utilities) and stops counting in your totals until you restore it.",
          "/api/other-income/" + inc.id, function () { loadOtherIncome(); loadDashboard(); });
      }));
      div.appendChild(iacts);
      $("incomeList").appendChild(div);
    });
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
$("incomeForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  apiPost("/api/other-income", {
    title: $("incTitle").value.trim(),
    category: ($("incCategoryNew").value.trim() || $("incCategory").value || "").trim(),
    party_id: $("incParty").value ? parseInt($("incParty").value, 10) : null,
    amount_paisa: rsToPaisa($("incAmount").value),
    note: $("incNote").value.trim()
  }).then(function () {
    $("incomeForm").reset();
    hidePanel("incomePanel");
    setMsg("appMsg", "Other income saved.");
    loadOtherIncome();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});
$("btnAddIncome").addEventListener("click", function () {
  showPanel("incomePanel");
  $("incTitle").focus();
});
$("btnIncomeBack").addEventListener("click", function () { hidePanel("incomePanel"); });

/* ------------------------------------------- cash & bank: sub-tabs --- */
var selectedBankId = null;
document.querySelectorAll("[data-cbtab]").forEach(function (b) {
  b.addEventListener("click", function () {
    document.querySelectorAll("[data-cbtab]").forEach(function (x) {
      x.classList.toggle("active", x === b);
    });
    var tab = b.getAttribute("data-cbtab");
    $("cbAccounts").classList.toggle("hidden", tab !== "accounts");
    $("cbCash").classList.toggle("hidden", tab !== "cash");
    $("cbCheques").classList.toggle("hidden", tab !== "cheques");
    $("cbLoans").classList.toggle("hidden", tab !== "loans");
    if (tab === "accounts") loadBankAccounts();
    if (tab === "cash") loadCashBank();
    if (tab === "cheques") loadCheques();
    if (tab === "loans") loadLoans();
  });
});
function loadBankAccounts() {
  apiGet("/api/bank-accounts", null).then(function (r) {
    var list = r.body.bankAccounts || [];
    var toSel = $("bankTxTo");
    if (toSel) {
      var keepTo = toSel.value;
      toSel.innerHTML = list.filter(function (a) { return a.id !== selectedBankId; }).map(function (a) {
        return '<option value="' + a.id + '">' + esc(a.displayName) + "</option>";
      }).join("");
      if (keepTo) toSel.value = keepTo;
    }
    $("bankList").innerHTML = list.length ? "" : '<p class="empty">No bank accounts yet.</p>';
    list.forEach(function (a) {
      var div = document.createElement("div");
      div.className = "row";
      div.innerHTML = rowHtml(a.displayName,
        (a.bankName || "") + (a.accountNumber ? " · " + a.accountNumber : "") +
        (a.printQr ? " · QR on invoices" : "") + (a.printDetails ? " · Details on invoices" : ""),
        fmtRs(a.balancePaisa));
      var btn = document.createElement("button");
      btn.className = "btn btn-mini"; btn.type = "button"; btn.textContent = "Transactions";
      btn.addEventListener("click", function () {
        selectedBankId = a.id;
        $("bankTxCard").classList.remove("hidden");
        $("bankTxTitle").textContent = "Transactions — " + a.displayName + " · Balance " + fmtRs(a.balancePaisa);
        loadBankTransactions();
      });
      div.appendChild(btn);
      div.appendChild(miniBtn("Delete", function () {
        deleteRecord("bank account '" + a.displayName + "'",
          "An account with entries cannot be deleted - you will see the reason if it is refused.",
          "/api/bank-accounts/" + a.id, loadBankAccounts);
      }));
      $("bankList").appendChild(div);
    });
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
function loadBankTransactions() {
  if (!selectedBankId) return;
  apiGet("/api/bank-accounts/" + selectedBankId + "/transactions", null).then(function (r) {
    $("bankTxList").innerHTML = (r.body.transactions || []).slice().reverse().map(function (t) {
      return '<div class="row">' + rowHtml(t.kind === "deposit" ? "Deposit" : "Withdraw",
        (t.createdAt || "").slice(0, 10) + (t.note ? " · " + t.note : ""),
        fmtRs(t.amountPaisa), t.kind === "deposit" ? "pos" : "neg") + "</div>";
    }).join("") || '<p class="empty">No transactions yet.</p>';
  }).catch(function () {});
}
$("bankForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  apiPost("/api/bank-accounts", {
    display_name: $("bankName1").value.trim(),
    opening_balance_paisa: rsToPaisa($("bankOpening").value),
    as_of_date: $("bankAsOf").value || null,
    account_number: $("bankAccNo").value.trim(),
    iban: $("bankIban").value.trim(),
    qr_handle: $("bankQr").value.trim(),
    bank_name: $("bankBankName").value.trim(),
    holder_name: $("bankHolder").value.trim(),
    print_details: $("bankPrintDetails").checked,
    print_qr: $("bankPrintQr").checked
  }).then(function () {
    $("bankForm").reset();
    hidePanel("bankPanel");
    setMsg("appMsg", "Bank account added.");
    loadBankAccounts();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});
$("btnAddBank").addEventListener("click", function () {
  showPanel("bankPanel");
  $("bankName1").focus();
});
$("btnBankBack").addEventListener("click", function () { hidePanel("bankPanel"); });
function bankTransact(kind) {
  if (!selectedBankId) { setMsg("appMsg", "Open an account's Transactions first.", true); return; }
  var amt = rsToPaisa($("bankTxAmount").value);
  if (amt <= 0) { setMsg("appMsg", "Enter an amount.", true); return; }
  apiPost("/api/bank-accounts/" + selectedBankId + "/transact", {
    kind: kind, amount_paisa: amt, note: $("bankTxNote").value.trim()
  }).then(function () {
    $("bankTxAmount").value = 0; $("bankTxNote").value = "";
    setMsg("appMsg", kind === "deposit" ? "Deposited." : "Withdrawn.");
    loadBankAccounts(); loadBankTransactions();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
/* Bank account transfers (delta #12): the Deposit/Withdraw buttons
   became a 4-action transfer menu; every action records real
   movements on the server. */
function bankTransfer(kind) {
  if (!selectedBankId) { setMsg("appMsg", "Open an account's Transactions first.", true); return; }
  var payload = { kind: kind, note: $("bankTxNote").value.trim(), client_ref: "xfer-" + Date.now() };
  if (kind === "adjust") {
    payload.adjust_to_paisa = rsToPaisa($("bankTxAdjustTo").value);
  } else {
    var amt = rsToPaisa($("bankTxAmount").value);
    if (amt <= 0) { setMsg("appMsg", "Enter an amount.", true); return; }
    payload.amount_paisa = amt;
    if (kind === "bank_to_bank") {
      var to = parseInt($("bankTxTo").value || "0", 10);
      if (!to) { setMsg("appMsg", "Choose the account to transfer to.", true); return; }
      payload.to_account_id = to;
    }
  }
  apiPost("/api/bank-accounts/" + selectedBankId + "/transfer", payload).then(function () {
    $("bankTxAmount").value = 0; $("bankTxNote").value = "";
    setMsg("appMsg", "Transfer saved.");
    loadBankAccounts(); loadBankTransactions(); loadCashBank();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
$("btnBankToCash").addEventListener("click", function () { bankTransfer("bank_to_cash"); });
$("btnCashToBank").addEventListener("click", function () { bankTransfer("cash_to_bank"); });
$("btnBankToBank").addEventListener("click", function () { bankTransfer("bank_to_bank"); });
$("btnAdjustBank").addEventListener("click", function () { bankTransfer("adjust"); });
$("btnBankStatement").addEventListener("click", function () {
  if (!selectedBankId) return;
  openReports("bank-statement");
});
function loadCheques() {
  fillPartySelects();
  apiGet("/api/cheque-payments", null).then(function (r) {
    var mk = function (list, emptyText) {
      if (!list.length) return '<p class="empty">' + emptyText + "</p>";
      return list.map(function (c) {
        return '<div class="row">' + rowHtml("Cheque " + (c.refNo || "—"),
          (c.date || "") + (c.party ? " · " + c.party : "") + (c.invoiceNo ? " · " + c.invoiceNo : ""),
          fmtRs(c.amountPaisa)) + "</div>";
      }).join("");
    };
    $("chqPayInList").innerHTML = mk(r.body.received || [], "No received cheques yet.") +
      '<p class="note">Total received: ' + fmtRs(r.body.receivedTotalPaisa || 0) + "</p>";
    $("chqPayOutList").innerHTML = mk(r.body.paid || [], "No paid cheques yet.") +
      '<p class="note">Total paid: ' + fmtRs(r.body.paidTotalPaisa || 0) + "</p>";
  }).catch(function () {});
  apiGet("/api/cheques", null).then(function (r) {
    var list = (r.body.cheques || []).slice().reverse();
    $("chequeList").innerHTML = list.length ? "" : '<p class="empty">No cheques yet.</p>';
    list.forEach(function (c) {
      var div = document.createElement("div");
      div.className = "row";
      div.innerHTML = rowHtml("Cheque " + (c.chequeNo || "—"),
        (c.bankName || "") + " · " + c.status + (c.dueDate ? " · Due " + c.dueDate : ""),
        fmtRs(c.amountPaisa));
      if (c.status === "pending") {
        ["cleared", "bounced"].forEach(function (st) {
          var b = document.createElement("button");
          b.className = "btn btn-mini"; b.type = "button";
          b.textContent = st === "cleared" ? "Mark Cleared" : "Mark Bounced";
          b.addEventListener("click", function () {
            apiPut("/api/cheques/" + c.id, { status: st }).then(loadCheques)
              .catch(function (e) { setMsg("appMsg", e.message, true); });
          });
          div.appendChild(b);
        });
      }
      div.appendChild(miniBtn("Delete", function () {
        deleteRecord("cheque " + (c.chequeNo || "") + " (" + fmtRs(c.amountPaisa) + ")",
          "This removes the cheque-book entry only.",
          "/api/cheques/" + c.id, loadCheques);
      }));
      $("chequeList").appendChild(div);
    });
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
$("chequeForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  apiPost("/api/cheques", {
    cheque_no: $("chqNo").value.trim(), bank_name: $("chqBank").value.trim(),
    amount_paisa: rsToPaisa($("chqAmount").value),
    party_id: $("chqParty").value ? parseInt($("chqParty").value, 10) : null,
    due_date: $("chqDue").value || null
  }).then(function () {
    $("chequeForm").reset();
    hidePanel("chequePanel");
    setMsg("appMsg", "Cheque added.");
    loadCheques();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});
$("btnAddCheque").addEventListener("click", function () {
  showPanel("chequePanel");
  $("chqNo").focus();
});
$("btnChequeBack").addEventListener("click", function () { hidePanel("chequePanel"); });
function loadLoans() {
  apiGet("/api/loans", null).then(function (r) {
    var list = r.body.loans || [];
    $("loanList").innerHTML = list.length ? "" : '<p class="empty">No loan accounts yet.</p>';
    list.forEach(function (l) {
      var div = document.createElement("div");
      div.className = "row";
      div.innerHTML = rowHtml(l.name,
        (l.lender ? l.lender + " · " : "") + "Paid " + fmtRs(l.paidPaisa) +
        " of " + fmtRs(l.principalPaisa + Math.round(l.principalPaisa * (l.interestBp || 0) / 10000)),
        "Balance " + fmtRs(l.balancePaisa), "neg");
      var btn = document.createElement("button");
      btn.className = "btn btn-mini"; btn.type = "button"; btn.textContent = "Pay Instalment";
      btn.addEventListener("click", function () {
        var v = prompt("Instalment amount (Rs):");
        if (!v) return;
        apiPost("/api/loans/" + l.id + "/pay", { amount_paisa: rsToPaisa(v) }).then(function () {
          setMsg("appMsg", "Instalment recorded.");
          loadLoans();
        }).catch(function (e) { setMsg("appMsg", e.message, true); });
      });
      div.appendChild(btn);
      div.appendChild(miniBtn("Delete", function () {
        deleteRecord("loan account '" + l.name + "'",
          "A loan with repayments recorded cannot be deleted - you will see the reason if it is refused.",
          "/api/loans/" + l.id, loadLoans);
      }));
      $("loanList").appendChild(div);
    });
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
$("loanForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  apiPost("/api/loans", {
    name: $("loanName").value.trim(), lender: $("loanLender").value.trim(),
    principal_paisa: rsToPaisa($("loanPrincipal").value),
    interest_bp: Math.round((parseFloat($("loanInterest").value || "0") || 0) * 100),
    start_date: $("loanStart").value || null, note: $("loanNote").value.trim()
  }).then(function () {
    $("loanForm").reset();
    hidePanel("loanPanel");
    setMsg("appMsg", "Loan account added.");
    loadLoans();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});
$("btnAddLoan").addEventListener("click", function () {
  showPanel("loanPanel");
  $("loanName").focus();
});
$("btnLoanBack").addEventListener("click", function () { hidePanel("loanPanel"); });

/* ------------------------------------------------------ online store - */
var storeTab = "orders";
document.querySelectorAll("[data-storetab]").forEach(function (b) {
  b.addEventListener("click", function () {
    storeTab = b.getAttribute("data-storetab");
    document.querySelectorAll("[data-storetab]").forEach(function (x) {
      x.classList.toggle("active", x === b);
    });
    $("storeOrdersBox").classList.toggle("hidden", storeTab !== "orders");
    $("storeItemsBox").classList.toggle("hidden", storeTab !== "items");
    $("storeSettingsForm").classList.toggle("hidden", storeTab !== "settings");
    loadStore();
  });
});
function loadStore() {
  apiGet("/api/store/settings", null).then(function (r) {
    var s = r.body.store || {};
    if (QB.settings) QB.settings.store = s;
    $("storeEnabled").checked = !!s.enabled;
    $("storeName").value = s.name || "";
    $("storeAccept").checked = s.accept_orders !== false;
    $("storeMinOrder").value = ((s.min_order_paisa || 0) / 100).toFixed(2);
    $("storeDelivery").value = ((s.delivery_charges_paisa || 0) / 100).toFixed(2);
    $("storeDiscounts").checked = !!s.item_discounts;
    $("storeLinkStock").checked = s.link_stock !== false;
    $("storeLink").textContent = r.body.businessId
      ? (location.origin + "/store/" + r.body.businessId) : "—";
  }).catch(function () {});
  if (storeTab === "orders") {
    apiGet("/api/store/orders", null).then(function (r) {
      var list = (r.body.orders || []).slice().reverse();
      $("storeOrderList").innerHTML = list.length ? "" : '<p class="empty">No online orders yet.</p>';
      list.forEach(function (o) {
        var div = document.createElement("div");
        div.className = "row";
        div.innerHTML = rowHtml(o.customerName + " · " + o.phone,
          (o.createdAt || "").slice(0, 10) + " · " + o.status +
          (o.address ? " · " + o.address : "") +
          (o.lines || []).map(function (l) { return " · " + l.itemName + " ×" + l.qty; }).join(""),
          fmtRs(o.totalPaisa));
        var acts = document.createElement("div");
        acts.className = "row-actions";
        if (o.status === "pending") {
          var conv = document.createElement("button");
          conv.className = "btn btn-mini btn-primary"; conv.type = "button"; conv.textContent = "Convert to Sale";
          conv.addEventListener("click", function () {
            apiPost("/api/store/orders/" + o.id + "/convert", {}).then(function (body) {
              setMsg("appMsg", "Order converted to a Sale Invoice (see the Sale book).");
              loadStore();
            }).catch(function (e) { setMsg("appMsg", e.message, true); });
          });
          acts.appendChild(conv);
          var del = document.createElement("button");
          del.className = "btn btn-mini"; del.type = "button"; del.textContent = "Deliver Order";
          del.addEventListener("click", function () {
            apiPost("/api/store/orders/" + o.id + "/status", { status: "delivered" }).then(loadStore)
              .catch(function (e) { setMsg("appMsg", e.message, true); });
          });
          acts.appendChild(del);
        }
        if (o.status === "delivered") {
          var re = document.createElement("button");
          re.className = "btn btn-mini"; re.type = "button"; re.textContent = "Reopen";
          re.addEventListener("click", function () {
            apiPost("/api/store/orders/" + o.id + "/status", { status: "pending" }).then(loadStore)
              .catch(function (e) { setMsg("appMsg", e.message, true); });
          });
          acts.appendChild(re);
        }
        div.appendChild(acts);
        $("storeOrderList").appendChild(div);
      });
    }).catch(function (e) { setMsg("appMsg", e.message, true); });
  }
  if (storeTab === "items") {
    apiGet("/api/store/items", null).then(function (r) {
      $("storeItemBody").innerHTML = (r.body.items || []).map(function (i) {
        return "<tr><td>" + esc(i.name) + '</td><td class="num">' + fmtRs(i.salePricePaisa) + "</td>" +
          '<td><input data-store-price="' + i.itemId + '" type="number" step="0.01" value="' +
          ((i.storePricePaisa != null ? i.storePricePaisa : i.salePricePaisa) / 100).toFixed(2) + '"></td>' +
          '<td class="num">' + i.stockQty + "</td>" +
          '<td><input type="checkbox" data-store-stock="' + i.itemId + '"' + (i.inStock ? " checked" : "") + "></td>" +
          '<td><input type="checkbox" data-store-listed="' + i.itemId + '"' + (i.listed ? " checked" : "") + "></td></tr>";
      }).join("");
      $("storeItemBody").querySelectorAll("input").forEach(function (inp) {
        inp.addEventListener("change", saveStoreItemRow);
      });
    }).catch(function (e) { setMsg("appMsg", e.message, true); });
  }
}
function saveStoreItemRow(ev) {
  var tr = ev.target.closest("tr");
  var priceInp = tr.querySelector("[data-store-price]");
  var id = priceInp.getAttribute("data-store-price");
  apiPut("/api/store/items/" + id, {
    store_price_paisa: rsToPaisa(priceInp.value),
    in_stock: tr.querySelector("[data-store-stock]").checked,
    listed: tr.querySelector("[data-store-listed]").checked
  }).then(function () { setMsg("appMsg", "Store item saved."); })
    .catch(function (e) { setMsg("appMsg", e.message, true); });
}
$("storeSettingsForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  var store = {
    enabled: $("storeEnabled").checked,
    name: $("storeName").value.trim(),
    accept_orders: $("storeAccept").checked,
    min_order_paisa: rsToPaisa($("storeMinOrder").value),
    delivery_charges_paisa: rsToPaisa($("storeDelivery").value),
    item_discounts: $("storeDiscounts").checked,
    link_stock: $("storeLinkStock").checked
  };
  apiPut("/api/store/settings", { store: store }).then(function (body) {
    if (QB.settings) QB.settings.store = body.store || store;
    applySettingsChrome();
    setMsg("appMsg", "Store settings saved.");
    loadStore();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});

/* ---------------------------------------------------------- loyalty -- */
function loadLoyalty() {
  var loy = (QB.settings || {}).loyalty || {};
  $("loyEnabled").checked = !!loy.enabled;
  $("loySpend").value = ((loy.spend_per_point_paisa || 10000) / 100).toFixed(2);
  $("loyMinEarn").value = ((loy.min_invoice_earn_paisa || 0) / 100).toFixed(2);
  $("loyExpiry").value = loy.expiry_days != null ? loy.expiry_days : 365;
  $("loyRedeemValue").value = ((loy.redeem_value_paisa || 100) / 100).toFixed(2);
  $("loyMaxPct").value = loy.max_redeem_pct != null ? loy.max_redeem_pct : 50;
  $("loyMinRedeem").value = ((loy.min_invoice_redeem_paisa || 0) / 100).toFixed(2);
  apiGet("/api/parties", "parties").then(function (r) {
    var list = (r.body.parties || []).filter(function (p) { return (p.loyaltyPoints || 0) > 0; });
    $("loyaltyBody").innerHTML = list.map(function (p) {
      var val = p.loyaltyPoints * (loy.redeem_value_paisa || 100);
      return "<tr><td>" + esc(p.name) + '</td><td class="num">' + p.loyaltyPoints +
        '</td><td class="num">' + fmtRs(val) + "</td></tr>";
    }).join("");
    $("loyaltyEmpty").classList.toggle("hidden", list.length > 0);
  }).catch(function () {});
}
$("loyaltyForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  saveSettings({ loyalty: {
    enabled: $("loyEnabled").checked,
    spend_per_point_paisa: rsToPaisa($("loySpend").value),
    min_invoice_earn_paisa: rsToPaisa($("loyMinEarn").value),
    expiry_days: parseInt($("loyExpiry").value || "0", 10) || 0,
    redeem_value_paisa: rsToPaisa($("loyRedeemValue").value),
    max_redeem_pct: parseInt($("loyMaxPct").value || "0", 10) || 0,
    min_invoice_redeem_paisa: rsToPaisa($("loyMinRedeem").value)
  } }).then(function () {
    hidePanel("loyaltyPanel");
    setMsg("appMsg", "Loyalty setup saved.");
    loadLoyalty();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});
$("btnLoyaltySetup").addEventListener("click", function () { showPanel("loyaltyPanel"); });
$("btnLoyaltyBack").addEventListener("click", function () { hidePanel("loyaltyPanel"); });

/* ------------------------------------------------------- accounting -- */
var coaCache = [];
function loadAccounting() {
  var on = QB.settings && QB.settings.accounting_enabled;
  $("accountingOffCard").classList.toggle("hidden", !!on);
  $("accountingMain").classList.toggle("hidden", !on);
  if (!on) return;
  apiGet("/api/accounts", null).then(function (r) {
    coaCache = r.body.accounts || [];
    var groups = {};
    coaCache.forEach(function (a) { (groups[a.group] = groups[a.group] || []).push(a); });
    $("coaList").innerHTML = Object.keys(groups).map(function (g) {
      return '<div class="sub" style="font-weight:700; margin-top:8px">' + esc(g) + "</div>" +
        groups[g].map(function (a) {
          var bal = a.systemBalancePaisa != null ? a.systemBalancePaisa :
            (a.journalDebitPaisa || 0) - (a.journalCreditPaisa || 0);
          return '<div class="row">' + rowHtml(a.name, "", fmtRs(bal)) + "</div>";
        }).join("");
    }).join("") || '<p class="empty">No accounts yet.</p>';
    renderJournalLineSelects();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
  apiGet("/api/journal", null).then(function (r) {
    $("journalList").innerHTML = (r.body.entries || []).slice().reverse().map(function (j) {
      return '<div class="row">' + rowHtml(j.narration || "Journal Entry",
        (j.date || "") + " · " + (j.lines || []).length + " lines", fmtRs(j.totalPaisa || 0)) + "</div>";
    }).join("") || '<p class="empty">No journal entries yet.</p>';
  }).catch(function () {});
  apiGet("/api/reports/balance-sheet", null).then(function (r) {
    function side(title, rows, total) {
      return "<h4>" + title + "</h4>" + (rows || []).map(function (x) {
        return '<div class="row">' + rowHtml(x.name, "", fmtRs(x.amountPaisa || 0)) + "</div>";
      }).join("") + '<div class="row"><strong>Total</strong><strong>' + fmtRs(total || 0) + "</strong></div>";
    }
    $("bsBody").innerHTML = side("Assets", r.body.assets, r.body.totalAssetsPaisa) +
      side("Liabilities", r.body.liabilities, r.body.totalLiabilitiesPaisa) +
      '<div class="row">' + rowHtml("Capital (balancing figure)", "", fmtRs(r.body.capitalPaisa || 0)) + "</div>" +
      '<div class="row">' + rowHtml("Profit", "", fmtRs(r.body.profitPaisa || 0)) + "</div>" +
      '<div class="row"><strong>Total Liabilities + Capital</strong><strong>' +
      fmtRs(r.body.totalLiabilitiesCapitalPaisa || 0) + "</strong></div>";
  }).catch(function () {});
  apiGet("/api/reports/trial-balance", null).then(function (r) {
    var tb = r.body || {};
    $("tbBody").innerHTML = '<table class="tx-table"><thead><tr><th>Account</th><th class="num">Debit</th><th class="num">Credit</th></tr></thead><tbody>' +
      (tb.rows || []).map(function (x) {
        return "<tr><td>" + esc(x.account) + '</td><td class="num">' + fmtRs(x.debitPaisa || 0) +
          '</td><td class="num">' + fmtRs(x.creditPaisa || 0) + "</td></tr>";
      }).join("") + '</tbody></table><p class="note">Total Debit ' + fmtRs(tb.totalDebitPaisa || 0) +
      " · Total Credit " + fmtRs(tb.totalCreditPaisa || 0) + "</p>";
  }).catch(function () {});
}
$("btnEnableAccounting").addEventListener("click", function () {
  saveSettings({ accounting_enabled: true }).then(loadAccounting);
});
function renderJournalLineSelects() {
  document.querySelectorAll(".jr-account").forEach(function (sel) {
    var keep = sel.value;
    sel.innerHTML = coaCache.map(function (a) {
      return '<option value="' + a.id + '">' + esc(a.name) + "</option>";
    }).join("");
    if (keep) sel.value = keep;
  });
}
function addJournalRow() {
  var row = document.createElement("div");
  row.className = "grid2 jr-row";
  row.innerHTML = '<label>Account <select class="jr-account"></select></label>' +
    '<label>Debit (Rs) <input class="jr-debit" type="number" step="0.01" min="0" value="0"></label>' +
    '<label>Credit (Rs) <input class="jr-credit" type="number" step="0.01" min="0" value="0"></label>';
  $("journalLines").appendChild(row);
  row.querySelectorAll("input").forEach(function (inp) {
    inp.addEventListener("input", journalTotals);
  });
  renderJournalLineSelects();
  journalTotals();
}
function journalTotals() {
  var d = 0, c = 0;
  document.querySelectorAll(".jr-row").forEach(function (row) {
    d += rsToPaisa(row.querySelector(".jr-debit").value);
    c += rsToPaisa(row.querySelector(".jr-credit").value);
  });
  $("jrDebitTotal").textContent = fmtRs(d);
  $("jrCreditTotal").textContent = fmtRs(c);
  $("jrSave").disabled = !(d > 0 && d === c);
  return { d: d, c: c };
}
$("btnJournalRow").addEventListener("click", addJournalRow);
$("coaForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  apiPost("/api/accounts", { name: $("coaName").value.trim(), group_name: $("coaGroup").value })
    .then(function () {
      $("coaForm").reset();
      hidePanel("coaPanel");
      setMsg("appMsg", "Account added.");
      loadAccounting();
    }).catch(function (e) { setMsg("appMsg", e.message, true); });
});
$("btnAddCoa").addEventListener("click", function () {
  showPanel("coaPanel");
  $("coaName").focus();
});
$("btnCoaCancel").addEventListener("click", function () { hidePanel("coaPanel"); });
$("btnAddJournal").addEventListener("click", function () { showPanel("journalPanel"); });
$("btnJournalBack").addEventListener("click", function () { hidePanel("journalPanel"); });
$("journalForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  var lines = [];
  document.querySelectorAll(".jr-row").forEach(function (row) {
    lines.push({
      account_id: parseInt(row.querySelector(".jr-account").value, 10),
      debit_paisa: rsToPaisa(row.querySelector(".jr-debit").value),
      credit_paisa: rsToPaisa(row.querySelector(".jr-credit").value)
    });
  });
  apiPost("/api/journal", {
    entry_date: $("jrDate").value || null,
    narration: $("jrNarration").value.trim(),
    lines: lines
  }).then(function () {
    $("journalLines").innerHTML = "";
    addJournalRow(); addJournalRow();
    $("jrNarration").value = "";
    hidePanel("journalPanel");
    setMsg("appMsg", "Journal entry saved.");
    loadAccounting();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});

/* --------------------------------------------------- team & roles ---- */
var teamRoles = [];
var teamPresets = {};
var PERM_MODULES = [];
function loadTeam() {
  apiGet("/api/team/roles", null).then(function (r) {
    teamRoles = r.body.roles || [];
    teamPresets = r.body.presets || {};
    PERM_MODULES = r.body.modules || [];
    var sel = $("teamRole");
    sel.innerHTML = teamRoles.map(function (x) { return "<option>" + esc(x) + "</option>"; }).join("");
    applyRolePreset();
  }).catch(function () {});
  apiGet("/api/team", null).then(function (r) {
    var list = r.body.members || [];
    $("teamBody").innerHTML = list.map(function (m) {
      return "<tr><td>" + esc(m.name) + "</td><td>" + esc(m.contact) + "</td><td>" +
        esc(m.status) + "</td><td>" + esc(m.role) + "</td>" +
        '<td><button class="btn btn-mini" data-team-del="' + m.id + '">Remove</button></td></tr>';
    }).join("");
    $("teamEmpty").classList.toggle("hidden", list.length > 0);
    $("teamBody").querySelectorAll("[data-team-del]").forEach(function (b) {
      b.addEventListener("click", function () {
        if (!confirm("Remove this team member?")) return;
        apiDelete("/api/team/" + b.getAttribute("data-team-del")).then(loadTeam)
          .catch(function (e) { setMsg("appMsg", e.message, true); });
      });
    });
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
  // shared with me
  var mine = (me && me.memberOf) || [];
  $("sharedList").innerHTML = mine.length ? "" : '<p class="empty">No business has been shared with you.</p>';
  mine.forEach(function (mb) {
    var div = document.createElement("div");
    div.className = "row";
    div.innerHTML = rowHtml(mb.businessName, "Role: " + mb.role, null, "");
    var btn = document.createElement("button");
    btn.className = "btn btn-mini btn-primary"; btn.type = "button"; btn.textContent = "Open";
    btn.addEventListener("click", function () {
      apiPost("/api/team/switch", { business_id: mb.businessId }).then(function () { refreshMe(); })
        .catch(function (e) { setMsg("appMsg", e.message, true); });
    });
    div.appendChild(btn);
    $("sharedList").appendChild(div);
  });
  var s = QB.settings || {};
  $("teamAutoBackup").textContent = s.auto_backup === false ? "OFF" : "ON";
  apiGet("/api/team/activity", null).then(function (r) {
    $("activityList").innerHTML = (r.body.activity || []).slice(0, 20).map(function (a) {
      return '<div class="row">' + rowHtml(a.email, (a.createdAt || "") + " · " + (a.ip || ""), null, "") + "</div>";
    }).join("") || '<p class="empty">No sign-in activity recorded.</p>';
  }).catch(function () {});
  apiGet("/api/backups", "backups").then(function (r) {
    var list = r.body.backups || [];
    $("teamLastBackup").textContent = list.length ? (list[list.length - 1].createdAt || "").slice(0, 10) : "—";
  }).catch(function () {});
}
function applyRolePreset() {
  var perms = teamPresets[$("teamRole").value] || {};
  $("permBody").innerHTML = PERM_MODULES.map(function (mod) {
    var p = perms[mod] || {};
    return "<tr><td>" + esc(mod) + "</td>" + ["view", "create", "edit", "share", "delete"].map(function (a) {
      return '<td><input type="checkbox" data-perm-mod="' + esc(mod) + '" data-perm-act="' + a + '"' +
        (p[a] ? " checked" : "") + "></td>";
    }).join("") + "</tr>";
  }).join("");
}
$("teamRole").addEventListener("change", applyRolePreset);
$("teamForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  var perms = {};
  document.querySelectorAll("[data-perm-mod]").forEach(function (cb) {
    var mod = cb.getAttribute("data-perm-mod");
    perms[mod] = perms[mod] || {};
    perms[mod][cb.getAttribute("data-perm-act")] = cb.checked;
  });
  apiPost("/api/team", {
    name: $("teamName").value.trim(),
    contact: $("teamContact").value.trim(),
    role: $("teamRole").value,
    perms: perms
  }).then(function () {
    $("teamForm").reset();
    hidePanel("teamPanel");
    setMsg("appMsg", "Team member added. They sign in with this email and open your business from Sync, Share & Backup.");
    loadTeam();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});
$("btnAddTeamUser").addEventListener("click", function () {
  showPanel("teamPanel");
  $("teamName").focus();
});
$("btnTeamBack").addEventListener("click", function () { hidePanel("teamPanel"); });
$("btnBackupComputer").addEventListener("click", function () {
  apiGet("/api/backups", "backups").then(function (r) {
    var list = r.body.backups || [];
    if (!list.length) { setMsg("appMsg", "No backup yet — use Backup Now first.", true); return; }
    downloadAuthed("/api/backups/" + list[list.length - 1].id + "/download", "qbill-backup.json")
      .catch(function (e) { setMsg("appMsg", e.message, true); });
  });
});
$("btnBackupNowTeam").addEventListener("click", function () {
  apiPost("/api/backups/run", {}).then(function () {
    setMsg("appMsg", "Backup created.");
    loadTeam();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});
$("btnBackupSettings").addEventListener("click", function () {
  go("settings");
  setSettingsCat("backups");
});

/* ------------------------------------------------------- utilities --- */
function loadUtilities() {
  renderRecycleBin();
  var items = ((cacheGet("items") || {}).items) || [];
  $("bcItem").innerHTML = items.map(function (i) {
    return '<option value="' + i.id + '">' + esc(i.name) + "</option>";
  }).join("");
  drawBarcode();
}
function renderRecycleBin() {
  apiGet("/api/recycle-bin", null).then(function (r) {
    var box = $("recycleList");
    box.innerHTML = "";
    var any = false;
    function section(title, rows, label, sub, amount, restorePath, permPath) {
      if (!rows.length) return;
      any = true;
      var h = document.createElement("p");
      h.className = "bin-section"; h.textContent = title;
      box.appendChild(h);
      rows.forEach(function (row) {
        var div = document.createElement("div");
        div.className = "row";
        div.innerHTML = rowHtml(label(row), sub(row), amount ? amount(row) : null);
        var acts = document.createElement("div");
        acts.className = "row-actions";
        var restore = document.createElement("button");
        restore.className = "btn btn-mini btn-primary"; restore.type = "button"; restore.textContent = "Restore";
        restore.addEventListener("click", function () {
          apiPost(restorePath(row), {}).then(function () {
            setMsg("appMsg", "Restored.");
            renderRecycleBin(); loadDashboard();
          }).catch(function (e) { setMsg("appMsg", e.message, true); });
        });
        acts.appendChild(restore);
        var perm = document.createElement("button");
        perm.className = "btn btn-mini"; perm.type = "button"; perm.textContent = "Delete Forever";
        perm.addEventListener("click", function () {
          if (!confirm("Permanently delete " + label(row) + "? This cannot be undone.")) return;
          apiDelete(permPath(row)).then(function () {
            setMsg("appMsg", "Deleted forever.");
            renderRecycleBin();
          }).catch(function (e) { setMsg("appMsg", e.message, true); });
        });
        acts.appendChild(perm);
        div.appendChild(acts);
        box.appendChild(div);
      });
    }
    section("Transactions", (r.body.invoices || []).slice().reverse(),
      function (inv) { return inv.invoiceNo + " - " + (DOC_META[inv.type] || {}).title; },
      function (inv) { return "Deleted " + (inv.deletedAt || "").slice(0, 10); },
      function (inv) { return fmtRs(inv.totalPaisa); },
      function (inv) { return "/api/recycle-bin/" + inv.id + "/restore"; },
      function (inv) { return "/api/recycle-bin/" + inv.id; });
    section("Parties", r.body.parties || [],
      function (p) { return p.name; },
      function (p) { return "Deleted " + (p.deletedAt || "").slice(0, 10) + (p.phone ? " - " + p.phone : ""); },
      function (p) { return fmtRs(p.balancePaisa || 0); },
      function (p) { return "/api/recycle-bin/parties/" + p.id + "/restore"; },
      function (p) { return "/api/recycle-bin/parties/" + p.id; });
    section("Items", r.body.items || [],
      function (i) { return i.name; },
      function (i) { return "Deleted " + (i.deletedAt || "").slice(0, 10) + " - Stock " + i.stockQty; },
      function (i) { return fmtRs(i.salePricePaisa || 0); },
      function (i) { return "/api/recycle-bin/items/" + i.id + "/restore"; },
      function (i) { return "/api/recycle-bin/items/" + i.id; });
    section("Expenses", r.body.expenses || [],
      function (e) { return e.title; },
      function (e) { return "Deleted " + (e.deletedAt || "").slice(0, 10); },
      function (e) { return fmtRs(e.amountPaisa); },
      function (e) { return "/api/recycle-bin/expenses/" + e.id + "/restore"; },
      function (e) { return "/api/recycle-bin/expenses/" + e.id; });
    section("Other Income", r.body.incomes || [],
      function (e) { return e.title; },
      function (e) { return "Deleted " + (e.deletedAt || "").slice(0, 10); },
      function (e) { return fmtRs(e.amountPaisa); },
      function (e) { return "/api/recycle-bin/incomes/" + e.id + "/restore"; },
      function (e) { return "/api/recycle-bin/incomes/" + e.id; });
    if (!any) box.innerHTML = '<p class="empty">The Recycle Bin is empty.</p>';
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
$("btnVerify").addEventListener("click", function () {
  apiGet("/api/utilities/verify", null).then(function (r) {
    $("verifyList").innerHTML = (r.body.checks || []).map(function (c) {
      return '<div class="row">' + rowHtml((c.ok ? "✓ " : "✗ ") + c.name, c.detail || "",
        null, c.ok ? "pos" : "neg") + "</div>";
    }).join("");
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});
document.querySelectorAll("[data-export]").forEach(function (b) {
  b.addEventListener("click", function () {
    var kind = b.getAttribute("data-export");
    downloadAuthed("/api/utilities/export/" + kind, "qbill-" + kind + ".csv")
      .catch(function (e) { setMsg("appMsg", e.message, true); });
  });
});
$("btnSetupBusiness").addEventListener("click", function () { go("settings"); });

/* ------------------------------------------- import wizard (Fix 5) -- */
var WIZ_FIELDS = {
  parties: [["name", "Party Name"], ["phone", "Phone"], ["address", "Address"],
            ["opening_balance", "Opening Balance (Rs)"], ["email", "Email"],
            ["group", "Group"]],
  items: [["name", "Item Name"], ["item_code", "Item Code"],
          ["sale_price", "Sale Price (Rs)"], ["purchase_price", "Purchase Price (Rs)"],
          ["wholesale_price", "Wholesale Price (Rs)"], ["mrp", "MRP (Rs)"],
          ["unit", "Unit"], ["stock_qty", "Stock Qty"], ["tax_pct", "Tax %"],
          ["category", "Category"], ["min_stock", "Min Stock"]]
};
var wiz = { headers: [], rows: [], preview: null, units: [] };
function openImportWizard(kind) {
  go("utilities");
  showPanel("importWizard");
  if (kind) $("wizKind").value = kind;
  wizSyncKind();
}
function wizSyncKind() {
  $("wizDateWrap").classList.toggle("hidden", $("wizKind").value !== "parties");
  $("lnkWizTemplate").setAttribute("href",
    "/api/utilities/import-template?kind=" + $("wizKind").value);
  if (!$("wizOpeningDate").value) {
    $("wizOpeningDate").value = new Date().toISOString().slice(0, 10);
  }
}
$("wizKind").addEventListener("change", wizSyncKind);
$("btnUtilImportItems").addEventListener("click", function () { openImportWizard("items"); });
$("btnUtilImportParties").addEventListener("click", function () { openImportWizard("parties"); });
$("btnWizParties").addEventListener("click", function () { openImportWizard("parties"); });
$("btnWizItems").addEventListener("click", function () { openImportWizard("items"); });
$("btnWizClose").addEventListener("click", function () { hidePanel("importWizard"); });
$("btnWizParse").addEventListener("click", function () {
  var file = $("wizFile").files[0];
  if (!file) { $("wizMsg").textContent = "Choose an Excel (.xlsx) or CSV file first."; return; }
  var fd = new FormData();
  fd.append("kind", $("wizKind").value);
  fd.append("file", file);
  var headers = authHeaders();
  delete headers["Content-Type"];
  $("wizMsg").textContent = "Reading the file...";
  fetch("/api/utilities/import/parse", { method: "POST", headers: headers, body: fd })
    .then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (body) {
        if (!resp.ok) throw new Error(body.error || ("Request failed (" + resp.status + ")"));
        return body;
      });
    })
    .then(function (body) {
      wiz.headers = body.headers || [];
      wiz.rows = body.rows || [];
      var fields = WIZ_FIELDS[$("wizKind").value];
      $("wizMapRows").innerHTML = fields.map(function (f) {
        var opts = '<option value="">- not imported -</option>' + wiz.headers.map(function (h) {
          return '<option value="' + esc(h) + '"' +
            ((body.mapping || {})[f[0]] === h ? " selected" : "") + ">" + esc(h) + "</option>";
        }).join("");
        return '<div class="wiz-map-row"><span>' + esc(f[1]) + '</span>' +
          '<select data-wiz-field="' + f[0] + '">' + opts + "</select></div>";
      }).join("");
      showPanel("wizMapWrap");
      hidePanel("wizPreviewWrap");
      $("wizMsg").textContent = wiz.rows.length + " rows read. Check the column matches, then preview.";
    })
    .catch(function (e) { $("wizMsg").textContent = e.message; });
});
function wizMapping() {
  var m = {};
  document.querySelectorAll("[data-wiz-field]").forEach(function (sel) {
    m[sel.getAttribute("data-wiz-field")] = sel.value;
  });
  return m;
}
$("btnWizValidate").addEventListener("click", function () {
  $("wizMsg").textContent = "Checking the rows...";
  apiPost("/api/utilities/import/wizard-validate", {
    kind: $("wizKind").value, mapping: wizMapping(), rows: wiz.rows
  }).then(function (r) {
    wiz.preview = r.body.rows || [];
    wiz.units = r.body.units || [];
    var c = r.body.counts || {};
    $("wizCounts").textContent = "New: " + (c.new || 0) + " - Already exists (skipped unless you choose Update): " +
      (c.duplicate || 0) + " - Errors: " + (c.error || 0);
    /* Preview wording (audit 2026-11-10, research §5): tabs read
       "Valid Items : N" / "Items with Errors : N" and the action
       button "Import N Valid Items" (the noun follows the kind). */
    var wizNoun = $("wizKind").value === "items" ? "Items" : "Parties";
    document.querySelector('[data-wiztab="new"]').textContent =
      "Valid " + wizNoun + " : " + (c.new || 0);
    document.querySelector('[data-wiztab="dup"]').textContent =
      "Duplicates : " + (c.duplicate || 0);
    document.querySelector('[data-wiztab="err"]').textContent =
      wizNoun + " with Errors : " + (c.error || 0);
    $("btnWizConfirm").textContent = "Import " + (c.new || 0) + " Valid " + wizNoun;
    $("btnWizConfirm").disabled = !((c.new || 0) + (c.duplicate || 0));
    function line(e, extra) {
      var d = e.data || {};
      return '<div class="row"><div><div class="main">' + esc(d.name || "(no name)") + '</div><div class="sub">' +
        esc(extra || "") + '</div></div></div>';
    }
    $("wizRowsNew").innerHTML = wiz.preview.filter(function (e) { return e.status === "new"; })
      .map(function (e) { return line(e, "Row " + (e.index + 2)); }).join("") ||
      '<p class="empty">No new rows.</p>';
    $("wizRowsDup").innerHTML = wiz.preview.filter(function (e) { return e.status === "duplicate"; })
      .map(function (e) {
        return '<div class="row"><div><div class="main">' + esc((e.data || {}).name || "") + '</div>' +
          '<div class="sub">Already exists as "' + esc(e.existingName || "") + '"</div></div>' +
          '<select data-wiz-dup="' + e.index + '"><option value="skip">Skip (keep mine)</option>' +
          '<option value="update">Update with file values</option></select></div>';
      }).join("") || '<p class="empty">No duplicates.</p>';
    $("wizRowsErr").innerHTML = wiz.preview.filter(function (e) { return e.status === "error"; })
      .map(function (e) { return line(e, "Row " + (e.index + 2) + ": " + (e.errors || []).join(" ")); }).join("") ||
      '<p class="empty">No errors.</p>';
    showPanel("wizPreviewWrap");
    $("wizMsg").textContent = "Preview ready. Duplicates are skipped unless you change them to Update.";
  }).catch(function (e) { $("wizMsg").textContent = e.message; });
});
document.querySelectorAll("[data-wiztab]").forEach(function (b) {
  b.addEventListener("click", function () {
    document.querySelectorAll("[data-wiztab]").forEach(function (x) {
      x.classList.toggle("active", x === b);
    });
    var t = b.getAttribute("data-wiztab");
    $("wizRowsNew").classList.toggle("hidden", t !== "new");
    $("wizRowsDup").classList.toggle("hidden", t !== "dup");
    $("wizRowsErr").classList.toggle("hidden", t !== "err");
  });
});
$("btnWizConfirm").addEventListener("click", function () {
  if (!wiz.preview) return;
  var rows = wiz.preview.filter(function (e) { return e.status !== "error"; }).map(function (e) {
    var action = "create";
    if (e.status === "duplicate") {
      var sel = document.querySelector('[data-wiz-dup="' + e.index + '"]');
      action = sel ? sel.value : "skip";
    }
    return { data: e.data, action: action };
  });
  $("wizMsg").textContent = "Importing...";
  apiPost("/api/utilities/import/wizard-confirm", {
    kind: $("wizKind").value, batch: "w" + Date.now(),
    opening_date: $("wizKind").value === "parties" ? $("wizOpeningDate").value : "",
    rows: rows
  }).then(function (r) {
    var b = r.body;
    if (wiz.units.length) {
      var have = itemUnits().map(function (u) { return u.name; });
      var add = wiz.units.filter(function (u) { return have.indexOf(u) < 0; })
        .map(function (u) { return { name: u, code: "", secondary: "", conversion: 0 }; });
      if (add.length) setItemUnits(itemUnits().concat(add));
    }
    $("wizMsg").textContent = "Import done: " + b.created + " created, " + b.updated +
      " updated, " + b.skipped + " skipped. " +
      (wiz.units.length ? "Units from the file are now in your unit list." : "");
    hidePanel("wizPreviewWrap");
    wiz.preview = null;
    loadDashboard();
  }).catch(function (e) { $("wizMsg").textContent = e.message; });
});
function runSearchInto(input, box) {
  var q = input.value.trim();
  if (q.length < 2) { box.innerHTML = ""; return; }
  apiGet("/api/search?q=" + encodeURIComponent(q), null).then(function (r) {
    box.innerHTML = (r.body.results || []).map(function (x) {
      return '<button class="row" type="button" data-search-kind="' + x.kind + '" data-search-id="' + x.id + '">' +
        rowHtml(x.label, x.kind + (x.sub ? " · " + x.sub : ""), x.amountPaisa != null ? fmtRs(x.amountPaisa) : null, "") +
        "</button>";
    }).join("") || '<p class="empty">No matches.</p>';
    box.querySelectorAll("[data-search-kind]").forEach(function (b) {
      b.addEventListener("click", function () {
        $("searchModal").classList.add("hidden");
        var kind = b.getAttribute("data-search-kind");
        if (kind === "party") loadParties(), showScreen("parties");
        else if (kind === "item") showScreen("items");
        else showScreen("book");
      });
    });
  }).catch(function () {});
}
$("utilSearch").addEventListener("input", function () { runSearchInto($("utilSearch"), $("utilSearchResults")); });
/* Open Anything overlay (Ctrl+F) */
function openAnything() {
  $("searchModal").classList.remove("hidden");
  $("globalSearch").value = "";
  $("globalSearchResults").innerHTML = "";
  setTimeout(function () { $("globalSearch").focus(); }, 30);
}
$("globalSearch").addEventListener("input", function () {
  runSearchInto($("globalSearch"), $("globalSearchResults"));
});
$("searchClose").addEventListener("click", function () { $("searchModal").classList.add("hidden"); });
document.addEventListener("keydown", function (ev) {
  if ((ev.ctrlKey || ev.metaKey) && (ev.key === "f" || ev.key === "F") && me && currentScreen) {
    var tag = (ev.target && ev.target.tagName) || "";
    if (tag !== "TEXTAREA") { ev.preventDefault(); openAnything(); }
  }
  if (ev.key === "Escape") { $("searchModal").classList.add("hidden"); closeDialog(); }
});
/* Barcode Generator — simple Code39-style bars from the value's digits. */
function drawBarcode() {
  var canvas = $("bcCanvas");
  if (!canvas) return;
  var item = itemById(parseInt($("bcItem").value, 10) || 0);
  var value = $("bcValue").value.trim() || (item && item.itemCode) || "0000";
  var ctx = canvas.getContext("2d");
  ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#000";
  var lines = [];
  document.querySelectorAll(".bc-line").forEach(function (cb) { if (cb.checked) lines.push(cb.value); });
  var y = 8;
  ctx.font = "bold 13px Arial"; ctx.textAlign = "center";
  if (item && lines.indexOf("name") >= 0) { ctx.fillText(item.name.slice(0, 34), 160, y + 12); y += 18; }
  if (item && lines.indexOf("business") >= 0) {
    ctx.font = "11px Arial";
    ctx.fillText(((me && me.business && me.business.name) || "").slice(0, 40), 160, y + 10); y += 14;
    ctx.font = "bold 13px Arial";
  }
  var x = 20;
  var digits = ("*" + value + "*").split("");
  digits.forEach(function (ch) {
    var code = ch.charCodeAt(0) % 7 + 2;
    for (var i = 0; i < code; i++) {
      var w = (i % 3 === 0) ? 3 : 1;
      if (i % 2 === 0) ctx.fillRect(x, y, w, 52);
      x += w + 1;
      if (x > 290) break;
    }
    x += 3;
  });
  y += 62;
  ctx.font = "12px Arial";
  ctx.fillText(value, 160, y + 10); y += 16;
  if (item && lines.indexOf("price") >= 0) { ctx.fillText("Sale Price: " + fmtRs(item.salePricePaisa), 160, y + 10); y += 16; }
  if (item && lines.indexOf("mrp") >= 0 && item.mrpPaisa) { ctx.fillText("MRP: " + fmtRs(item.mrpPaisa), 160, y + 10); }
}
["bcItem", "bcValue"].forEach(function (id) {
  $(id).addEventListener("input", drawBarcode);
  $(id).addEventListener("change", drawBarcode);
});
document.querySelectorAll(".bc-line").forEach(function (cb) {
  cb.addEventListener("change", drawBarcode);
});
$("btnBcPrint").addEventListener("click", function () {
  var w = window.open("", "_blank", "width=400,height=300");
  if (!w) { setMsg("appMsg", "Allow popups to print the label.", true); return; }
  w.document.write('<img src="' + $("bcCanvas").toDataURL("image/png") + '" style="width:320px">');
  w.document.close();
  w.focus();
  setTimeout(function () { w.print(); }, 300);
});

/* -------------------------------------------------- grow your business */
var POSTERS = {
  greetings: [
    { title: "Eid Mubarak!", sub: "Warm wishes to you and your family.", cta: "Visit our shop today", bg: "#0f766e" },
    { title: "Jummah Mubarak", sub: "May your Friday be blessed.", cta: "Open all week", bg: "#1d4ed8" },
    { title: "Ramzan Mubarak", sub: "Special Ramzan offers inside.", cta: "Shop now", bg: "#7c3aed" },
    { title: "Happy New Year", sub: "New year, new deals for your home.", cta: "Start shopping", bg: "#b45309" },
  ],
  trending: [
    { title: "Biggest Sale of the Season!", sub: "Limited time — while stock lasts.", cta: "Order on WhatsApp", bg: "#dc2626" },
    { title: "New Stock Arrived", sub: "Fresh items just landed in the shop.", cta: "See what's new", bg: "#0369a1" },
    { title: "Deal of the Day", sub: "One day only. Don't miss it.", cta: "Grab the deal", bg: "#be185d" },
  ],
  business: [
    { title: "We Are Open!", sub: "Quality products, honest prices, every day.", cta: "Visit us today", bg: "#065f46" },
    { title: "Home Delivery Available", sub: "Order on WhatsApp and get it at your door.", cta: "Order now", bg: "#0e7490" },
    { title: "Easy Instalments", sub: "Take it home today, pay in easy parts.", cta: "Ask in shop", bg: "#854d0e" },
  ],
  offers: [
    { title: "Buy 1 Get 1 Free", sub: "On selected items. Terms apply.", cta: "Shop the offer", bg: "#c2410c" },
    { title: "Flat 20% Off", sub: "This week only, on everything in store.", cta: "Save today", bg: "#15803d" },
    { title: "Festival Offer", sub: "Special prices for the festive season.", cta: "Don't miss out", bg: "#a21caf" },
  ],
};
var growTab = "greetings";
document.querySelectorAll("[data-growtab]").forEach(function (b) {
  b.addEventListener("click", function () {
    growTab = b.getAttribute("data-growtab");
    document.querySelectorAll("[data-growtab]").forEach(function (x) {
      x.classList.toggle("active", x === b);
    });
    loadGrow();
  });
});
function loadGrow() {
  $("posterGrid").innerHTML = "";
  (POSTERS[growTab] || []).forEach(function (p, idx) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "poster";
    btn.style.background = "linear-gradient(150deg, " + p.bg + ", #111827)";
    btn.innerHTML = '<div><div class="poster-title">' + esc(p.title) + '</div>' +
      '<div class="poster-sub">' + esc(p.sub) + "</div></div>" +
      '<span class="poster-cta">' + esc(p.cta) + "</span>";
    btn.addEventListener("click", function () { openPosterDialog(p); });
    $("posterGrid").appendChild(btn);
  });
}
function posterCanvas(p, extraText) {
  var canvas = document.createElement("canvas");
  canvas.width = 720; canvas.height = 900;
  var ctx = canvas.getContext("2d");
  var grad = ctx.createLinearGradient(0, 0, 720, 900);
  grad.addColorStop(0, p.bg); grad.addColorStop(1, "#111827");
  ctx.fillStyle = grad; ctx.fillRect(0, 0, 720, 900);
  ctx.fillStyle = "rgba(255,255,255,.14)";
  ctx.beginPath(); ctx.arc(620, 120, 190, 0, 7); ctx.fill();
  ctx.beginPath(); ctx.arc(60, 820, 150, 0, 7); ctx.fill();
  ctx.fillStyle = "#fff"; ctx.textAlign = "center";
  ctx.font = "800 64px Arial";
  wrapText(ctx, p.title, 360, 300, 620, 76);
  ctx.font = "30px Arial";
  wrapText(ctx, p.sub, 360, 470, 560, 42);
  ctx.font = "700 30px Arial";
  ctx.fillText(p.cta, 360, 600);
  var biz = (me && me.business) || {};
  ctx.font = "800 36px Arial";
  ctx.fillText((biz.name || "Your Business").slice(0, 34), 360, 730);
  ctx.font = "26px Arial";
  var contact = [biz.phone || "", biz.address || ""].filter(Boolean).join(" · ");
  wrapText(ctx, contact.slice(0, 60), 360, 775, 620, 34);
  if (extraText) {
    ctx.font = "italic 26px Arial";
    wrapText(ctx, extraText.slice(0, 90), 360, 845, 620, 34);
  }
  return canvas;
}
function wrapText(ctx, text, x, y, maxWidth, lineHeight) {
  var words = String(text || "").split(" ");
  var line = "", yy = y;
  words.forEach(function (word) {
    var test = line ? line + " " + word : word;
    if (ctx.measureText(test).width > maxWidth && line) {
      ctx.fillText(line, x, yy); line = word; yy += lineHeight;
    } else line = test;
  });
  if (line) ctx.fillText(line, x, yy);
  return yy;
}
function openPosterDialog(p) {
  var html = '<div style="text-align:center; margin-bottom:10px">' +
    '<canvas id="posterCanvas" width="360" height="450" style="max-width:100%; border-radius:12px"></canvas></div>' +
    '<label>Additional text <input id="posterExtra" type="text" placeholder="e.g. Offer ends Sunday"></label>' +
    '<div style="display:flex; gap:8px; flex-wrap:wrap; margin-top:10px">' +
    '<button class="btn btn-primary" id="btnPosterDownload" type="button">Download Image</button>' +
    '<button class="btn" id="btnPosterShare" type="button">Share on WhatsApp</button></div>' +
    '<p class="note">Your business name and contact are printed on the poster automatically.</p>';
  openDialog(p.title, html);
  function redraw() {
    var big = posterCanvas(p, $("posterExtra").value);
    var small = $("posterCanvas").getContext("2d");
    small.drawImage(big, 0, 0, 360, 450);
  }
  redraw();
  $("posterExtra").addEventListener("input", redraw);
  $("btnPosterDownload").addEventListener("click", function () {
    var a = document.createElement("a");
    a.href = posterCanvas(p, $("posterExtra").value).toDataURL("image/png");
    a.download = "qbill-poster.png";
    a.click();
  });
  $("btnPosterShare").addEventListener("click", function () {
    var text = p.title + " — " + p.sub + " " +
      (((me && me.business) || {}).name || "") + " " + (((me && me.business) || {}).phone || "");
    window.open("https://wa.me/?text=" + encodeURIComponent(text), "_blank");
  });
}

/* -------------------------------------------------- invoice preview -- */
function openPreview(inv) {
  if (!inv) { go("dashboard"); return; }
  if (QB.settings && QB.settings.do_not_show_invoice_preview && !$("previewNoShow").checked) {
    // honour the setting: skip straight back to the dashboard
    setMsg("appMsg", DOC_META[inv.type].title + " " + inv.invoiceNo + " saved.");
    go("dashboard");
    return;
  }
  lastSavedInvoice = inv;
  var themeSel = $("previewTheme");
  themeSel.innerHTML = PRINT_THEMES.map(function (t) { return "<option>" + esc(t) + "</option>"; }).join("");
  themeSel.value = (QB.settings && QB.settings.print_theme) || "Tally Theme";
  $("previewColor").value = (QB.settings && QB.settings.print_color) || "#f26522";
  renderPreviewBody();
  document.querySelectorAll(".screen").forEach(function (s) { s.classList.add("hidden"); });
  $("screen-preview").classList.remove("hidden");
  currentScreen = "preview";
  navKey = "quickbilling";
  $("viewTitle").textContent = "Invoice Preview";
  setActiveNav();
}
function renderPreviewBody() {
  var inv = lastSavedInvoice;
  if (!inv) return;
  var s = QB.settings || {};
  var color = $("previewColor").value || s.print_color || "#f26522";
  var parties = ((cacheGet("parties") || {}).parties) || [];
  var party = parties.filter(function (p) { return p.id === inv.partyId; })[0];
  var rows = (inv.lines || []).map(function (l) {
    var free = l.freeQty ? " + " + l.freeQty + " free" : "";
    var item = l.itemId ? itemById(l.itemId) : null;
    var lname = (item && item.name) || l.description || "Item";
    return "<tr><td>" + esc(lname) + free + "</td><td>" + l.qty +
      "</td><td>" + fmtRs(l.ratePaisa) + "</td><td>" + fmtRs((l.amountPaisa != null ? l.amountPaisa : l.qty * l.ratePaisa)) + "</td></tr>";
  }).join("");
  var saved = (inv.discountPaisa || 0) + (inv.loyaltyRedeemedPaisa || 0);
  $("previewBody").innerHTML =
    '<div style="border-top:8px solid ' + color + '; padding-top:10px">' +
    "<strong>" + esc((me && me.business && me.business.name) || "Your Business") + "</strong>" +
    '<div style="text-align:center; font-weight:800; font-size:19px; color:' + color + '">' +
    esc(((s.print_names || {})[inv.type]) || DOC_META[inv.type].title.toUpperCase()) + "</div>" +
    "<p>" + esc(inv.invoiceNo) + " · " + esc((inv.createdAt || "").slice(0, 10)) +
    (party ? " · " + esc(party.name) : " · Cash") + "</p>" +
    "<table><thead><tr><th>Item</th><th>Qty</th><th>Rate</th><th>Amount</th></tr></thead><tbody>" +
    rows + "</tbody></table>" +
    '<div style="text-align:right; margin-top:8px">Total: <strong>' + fmtRs(inv.totalPaisa) + "</strong><br>" +
    "Paid: " + fmtRs(inv.paidPaisa || 0) + " · Balance: " + fmtRs(inv.balancePaisa || 0) +
    (saved > 0 ? "<br>You Saved: " + fmtRs(saved) : "") +
    (inv.loyaltyPointsEarned ? "<br>Loyalty points earned: " + inv.loyaltyPointsEarned : "") +
    "</div></div>";
}
$("previewTheme").addEventListener("change", function () {
  saveSettings({ print_theme: $("previewTheme").value });
  renderPreviewBody();
});
$("previewColor").addEventListener("input", function () {
  saveSettings({ print_color: $("previewColor").value });
  renderPreviewBody();
});
$("previewNoShow").addEventListener("change", function () {
  if ($("previewNoShow").checked) {
    saveSettings({ do_not_show_invoice_preview: true }).then(function () {
      setMsg("appMsg", "Invoice Preview will not open after saving. Turn it back on in Settings > TRANSACTION.");
    });
  }
});
/* --------------------------------------------------- Quick Billing ---- */
/* POS-style sale screen (delta #1). Each bill tab lives in memory;
   Save & Print posts a normal sale through the existing API and
   opens the bill PDF. */
var qbBills = [];
var qbActive = null;
var qbSeq = 0;
var qbSelectedRow = null;
function qbNewBill() {
  return { id: ++qbSeq, partyId: null, partyName: "", items: {}, order: [],
           received: 0, payMode: "cash", partial: false, useSplit: false,
           payRows: [], billDiscountPaisa: 0, billTaxPct: 0, loyalty: 0, remarks: "" };
}
function qbEnsure() {
  if (!qbBills.length) { var b = qbNewBill(); qbBills.push(b); qbActive = b; }
  if (!qbActive) qbActive = qbBills[0];
}
function qbLineCalc(bill, entry) {
  if (entry.custom) {
    return { rate: entry.ratePaisa || 0, gross: entry.ratePaisa || 0, disc: 0, tax: 0, total: entry.ratePaisa || 0 };
  }
  var it = itemById(entry.itemId) || {};
  var rate = it.salePricePaisa || 0;
  var gross = entry.qty * rate;
  var disc = entry.discPct != null ? Math.round(gross * entry.discPct / 100)
    : (it.discountType === "amount" ? (it.discountAmountPaisa || 0) * entry.qty
       : Math.round(gross * (it.discountBp || 0) / 10000));
  var taxPct = entry.taxPct != null ? entry.taxPct : ((it.taxBp || 0) / 100);
  var tax = Math.round((gross - disc) * taxPct / 100);
  return { rate: rate, gross: gross, disc: disc, tax: tax, total: gross - disc + tax };
}
function qbTotals(bill) {
  var sub = 0, count = 0, qtyTotal = 0;
  bill.order.forEach(function (id) {
    var e = bill.items[id];
    if (!e) return;
    sub += qbLineCalc(bill, e).total;
    count += 1;
    qtyTotal += e.qty || 0;
  });
  var afterDisc = Math.max(sub - (bill.billDiscountPaisa || 0), 0);
  var billTax = Math.round(afterDisc * (bill.billTaxPct || 0) / 100);
  return { sub: sub, count: count, qty: qtyTotal, total: afterDisc + billTax, billTax: billTax };
}
function qbSyncSide() {
  var bill = qbActive;
  if (!bill) return;
  bill.received = rsToPaisa($("qbReceived").value);
  bill.remarks = $("qbRemarks").value;
  bill.payMode = $("qbPayMode").value;
}
function renderQuickTotals() {
  var bill = qbActive;
  if (!bill) return;
  var t = qbTotals(bill);
  $("qbSubTotal").textContent = fmtRs(t.sub);
  $("qbTotalLine").textContent = "Total Amount: " + fmtRs(t.total) +
    " (Items: " + t.count + ", Quantity: " + t.qty + ")";
  var received = bill.useSplit ? qbSplitSum() : rsToPaisa($("qbReceived").value);
  $("qbChange").textContent = fmtRs(Math.max(received - t.total, 0));
}
function qbSplitSum() {
  return (qbActive.payRows || []).reduce(function (s, p) { return s + (p.amount_paisa || 0); }, 0);
}
function syncQbPayRow(p, row) {
  var v = row.querySelector(".qb-pay-type").value;
  p.amount_paisa = rsToPaisa(row.querySelector(".qb-pay-amt").value);
  p.ref = row.querySelector(".qb-pay-ref").value.trim();
  if (v.indexOf("bank:") === 0) { p.mode = "bank"; p.bank_account_id = parseInt(v.slice(5), 10); }
  else { p.mode = v; delete p.bank_account_id; }
}
function renderQbPayRows() {
  var bill = qbActive;
  var list = $("qbPayRowsList");
  list.innerHTML = "";
  (bill.payRows || []).forEach(function (p, idx) {
    var row = document.createElement("div");
    row.className = "pay-row";
    row.innerHTML = '<select class="qb-pay-type"></select>' +
      '<input class="qb-pay-amt" type="number" step="0.01" min="0" value="' + ((p.amount_paisa || 0) / 100) + '">' +
      '<input class="qb-pay-ref' + (p.mode === "cash" ? " hidden" : "") + '" type="text" placeholder="Reference No." value="' + esc(p.ref || "") + '">' +
      '<button class="rm" type="button" title="Remove payment">×</button>';
    var sel = row.querySelector(".qb-pay-type");
    sel.innerHTML = '<option value="cash">Cash</option><option value="cheque">Cheque</option>' +
      (docBankAccounts || []).map(function (a) {
        return '<option value="bank:' + a.id + '">' + esc(a.displayName) + "</option>";
      }).join("");
    sel.value = p.mode === "bank" ? "bank:" + p.bank_account_id : p.mode;
    sel.addEventListener("change", function () {
      syncQbPayRow(p, row);
      row.querySelector(".qb-pay-ref").classList.toggle("hidden", sel.value === "cash");
    });
    row.querySelector(".qb-pay-amt").addEventListener("input", function () { syncQbPayRow(p, row); renderQuickTotals(); });
    row.querySelector(".qb-pay-ref").addEventListener("input", function () { syncQbPayRow(p, row); });
    row.querySelector(".rm").addEventListener("click", function () {
      bill.payRows.splice(idx, 1);
      renderQbPayRows();
      renderQuickTotals();
    });
    list.appendChild(row);
  });
}
function markQbRow() {
  $("qbGrid").querySelectorAll("[data-qbrow]").forEach(function (tr) {
    tr.style.background = tr.getAttribute("data-qbrow") === String(qbSelectedRow) ? "#eef4ff" : "";
  });
}
function renderQuick() {
  qbEnsure();
  var bill = qbActive;
  $("qbBillTabs").innerHTML = "";
  qbBills.forEach(function (b, i) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "doc-tab" + (b === bill ? " active" : "");
    btn.textContent = "#" + (i + 1);
    btn.addEventListener("click", function () { qbSyncSide(); qbActive = b; renderQuick(); });
    $("qbBillTabs").appendChild(btn);
  });
  var add = document.createElement("button");
  add.type = "button";
  add.className = "doc-tab";
  add.textContent = "+ New Bill";
  add.addEventListener("click", function () {
    qbSyncSide();
    var b = qbNewBill();
    qbBills.push(b);
    qbActive = b;
    renderQuick();
  });
  $("qbBillTabs").appendChild(add);
  var items = ((cacheGet("items") || {}).items) || [];
  var byId = {};
  items.forEach(function (i) { byId[i.id] = i; });
  $("qbGrid").innerHTML = bill.order.map(function (id, idx) {
    var e = bill.items[id];
    var it = byId[id] || {};
    var c = qbLineCalc(bill, e);
    return "<tr data-qbrow='" + id + "'>" +
      "<td>" + (idx + 1) + "</td><td>" + esc(e.custom ? "—" : (it.itemCode || "—")) + "</td>" +
      "<td>" + esc(e.custom ? e.name : (it.name || "")) + "</td>" +
      '<td><input class="qb-grid-input qb-qty" data-qbqty="' + id + '" type="number" min="1" step="1" value="' + e.qty + '"></td>' +
      "<td>" + esc(e.custom ? "—" : (it.unit || "Pcs")) + "</td>" +
      "<td>" + fmtRs(c.rate) + "</td><td>" + fmtRs(c.disc) + "</td><td>" + fmtRs(c.tax) + "</td>" +
      "<td>" + fmtRs(c.total) + "</td></tr>";
  }).join("");
  $("qbEmpty").classList.toggle("hidden", bill.order.length > 0);
  $("qbGrid").querySelectorAll("[data-qbqty]").forEach(function (inp) {
    inp.addEventListener("change", function () {
      var id = inp.getAttribute("data-qbqty");
      bill.items[id].qty = Math.max(parseInt(inp.value || "1", 10) || 1, 1);
      renderQuick();
    });
  });
  $("qbGrid").querySelectorAll("[data-qbrow]").forEach(function (tr) {
    tr.addEventListener("click", function () {
      qbSelectedRow = tr.getAttribute("data-qbrow");
      markQbRow();
    });
  });
  markQbRow();
  $("qbPartyChosen").textContent = bill.partyId ? ("Customer: " + bill.partyName) : "Cash Sale (no customer chosen)";
  $("qbPayMode").value = bill.payMode || "cash";
  $("qbPayRowsWrap").classList.toggle("hidden", !bill.useSplit);
  renderQbPayRows();
  $("qbReceived").value = bill.received ? (bill.received / 100).toFixed(2) : "";
  $("qbRemarks").value = bill.remarks || "";
  renderQuickTotals();
}
function loadQuick() {
  qbEnsure();
  Promise.all([
    apiGet("/api/items", "items"),
    apiGet("/api/parties", "parties"),
    apiGet("/api/bank-accounts", "bankaccounts")
  ]).then(function () {
    docBankAccounts = ((cacheGet("bankaccounts") || {}).bankAccounts) || [];
    var sel = $("qbPayMode");
    var keep = qbActive.payMode || sel.value;
    sel.innerHTML = '<option value="cash">Cash</option><option value="cheque">Cheque</option>' +
      docBankAccounts.map(function (a) {
        return '<option value="bank:' + a.id + '">' + esc(a.displayName) + "</option>";
      }).join("");
    sel.value = keep || "cash";
    renderQuick();
  }).catch(function () { renderQuick(); });
}
function qbAddItem(id) {
  var bill = qbActive;
  if (bill.items[id]) bill.items[id].qty += 1;
  else { bill.items[id] = { itemId: id, qty: 1, discPct: null, taxPct: null }; bill.order.push(id); }
  qbSelectedRow = String(id);
  $("qbSearch").value = "";
  $("qbResults").innerHTML = "";
  renderQuick();
  $("qbSearch").focus();
}
$("qbSearch").addEventListener("input", function () {
  var q = $("qbSearch").value.trim().toLowerCase();
  var box = $("qbResults");
  if (!q) { box.innerHTML = ""; return; }
  var items = ((cacheGet("items") || {}).items) || [];
  var hits = items.filter(function (i) {
    return i.name.toLowerCase().indexOf(q) >= 0 || (i.itemCode || "").toLowerCase().indexOf(q) >= 0;
  }).slice(0, 8);
  box.innerHTML = hits.map(function (i) {
    return '<button type="button" class="qb-result" data-qbadd="' + i.id + '">' + esc(i.name) +
      (i.itemCode ? " · " + esc(i.itemCode) : "") + " · " + fmtRs(i.salePricePaisa) + "</button>";
  }).join("");
  box.querySelectorAll("[data-qbadd]").forEach(function (b) {
    b.addEventListener("click", function () { qbAddItem(+b.getAttribute("data-qbadd")); });
  });
});
$("qbSearch").addEventListener("keydown", function (ev) {
  if (ev.key !== "Enter") return;
  ev.preventDefault();
  var q = $("qbSearch").value.trim().toLowerCase();
  if (!q) return;
  var items = ((cacheGet("items") || {}).items) || [];
  var hit = items.filter(function (i) { return (i.itemCode || "").toLowerCase() === q; })[0] ||
            items.filter(function (i) { return i.name.toLowerCase().indexOf(q) >= 0; })[0];
  if (hit) qbAddItem(hit.id);
});
$("qbPartySearch").addEventListener("input", function () {
  var q = $("qbPartySearch").value.trim().toLowerCase();
  var box = $("qbPartyResults");
  if (!q) { box.innerHTML = ""; return; }
  var parties = ((cacheGet("parties") || {}).parties) || [];
  var hits = parties.filter(function (p) {
    return p.name.toLowerCase().indexOf(q) >= 0 || (p.phone || "").indexOf(q) >= 0;
  }).slice(0, 8);
  box.innerHTML = hits.map(function (p) {
    return '<button type="button" class="qb-result" data-qbparty="' + p.id + '">' + esc(p.name) +
      (p.phone ? " · " + esc(p.phone) : "") + "</button>";
  }).join("") + '<button type="button" class="qb-result" data-qbparty="">Cash Sale (no customer)</button>';
  box.querySelectorAll("[data-qbparty]").forEach(function (b) {
    b.addEventListener("click", function () {
      var id = b.getAttribute("data-qbparty");
      var p = parties.filter(function (x) { return String(x.id) === id; })[0];
      qbActive.partyId = p ? p.id : null;
      qbActive.partyName = p ? p.name : "";
      $("qbPartySearch").value = "";
      box.innerHTML = "";
      renderQuick();
    });
  });
});
$("qbReceived").addEventListener("input", function () {
  if (!qbActive) return;
  qbActive.received = rsToPaisa($("qbReceived").value);
  renderQuickTotals();
});
$("qbPayMode").addEventListener("change", function () {
  qbActive.payMode = $("qbPayMode").value;
  renderQuickTotals();
});
$("qbRemarks").addEventListener("input", function () { qbActive.remarks = $("qbRemarks").value; });
$("qbAddPayRow").addEventListener("click", function () {
  qbActive.payRows.push({ mode: "cash", amount_paisa: 0, ref: "" });
  renderQbPayRows();
});
function qbNeedRow() {
  if (!qbActive || qbSelectedRow == null || !qbActive.items[qbSelectedRow]) {
    setMsg("qbMsg", "Select an item row first.", true);
    return null;
  }
  return qbActive.items[qbSelectedRow];
}
$("qbBtnQty").addEventListener("click", function () {
  var e = qbNeedRow(); if (!e) return;
  var v = prompt("Quantity for this item:", e.qty);
  if (v == null) return;
  e.qty = Math.max(parseInt(v, 10) || 1, 1);
  renderQuick();
});
$("qbBtnDisc").addEventListener("click", function () {
  var e = qbNeedRow(); if (!e) return;
  var v = prompt("Discount % for this item:", e.discPct == null ? "" : e.discPct);
  if (v == null) return;
  e.discPct = Math.max(parseFloat(v) || 0, 0);
  renderQuick();
});
$("qbBtnRemove").addEventListener("click", function () {
  if (!qbNeedRow()) return;
  delete qbActive.items[qbSelectedRow];
  qbActive.order = qbActive.order.filter(function (id) { return String(id) !== String(qbSelectedRow); });
  qbSelectedRow = null;
  renderQuick();
});
$("qbBtnTax").addEventListener("click", function () {
  var v = prompt("Bill tax % (applied on the total after bill discount):", qbActive.billTaxPct || 0);
  if (v == null) return;
  qbActive.billTaxPct = Math.max(parseFloat(v) || 0, 0);
  renderQuick();
});
$("qbBtnCharges").addEventListener("click", function () {
  var v = prompt("Additional charges (Rs):", "0");
  if (v == null) return;
  var amt = rsToPaisa(v);
  if (amt <= 0) return;
  var key = "custom" + (++qbSeq);
  qbActive.items[key] = { itemId: null, custom: true, name: "Additional Charges", ratePaisa: amt, qty: 1 };
  qbActive.order.push(key);
  renderQuick();
});
$("qbBtnBillDisc").addEventListener("click", function () {
  var v = prompt("Bill discount (Rs):", ((qbActive.billDiscountPaisa || 0) / 100).toFixed(2));
  if (v == null) return;
  qbActive.billDiscountPaisa = Math.max(rsToPaisa(v), 0);
  renderQuick();
});
$("qbBtnLoyalty").addEventListener("click", function () {
  if (!qbActive.partyId) { setMsg("qbMsg", "Choose a customer first to use loyalty points.", true); return; }
  var v = prompt("Loyalty points to use:", qbActive.loyalty || 0);
  if (v == null) return;
  qbActive.loyalty = Math.max(parseInt(v, 10) || 0, 0);
  setMsg("qbMsg", "Loyalty points set: " + qbActive.loyalty + " (applied on save).");
});
$("qbBtnRemarks").addEventListener("click", function () { $("qbRemarks").focus(); });
$("qbBtnPartial").addEventListener("click", function () {
  qbActive.partial = !qbActive.partial;
  setMsg("qbMsg", qbActive.partial
    ? "Partial Pay ON — only the Amount Received will be recorded as paid."
    : "Partial Pay OFF — the full bill amount will be recorded as paid.");
  $("qbReceived").focus();
});
$("qbBtnMulti").addEventListener("click", function () {
  qbActive.useSplit = !qbActive.useSplit;
  if (qbActive.useSplit && !qbActive.payRows.length) {
    qbActive.payRows.push({ mode: "cash", amount_paisa: 0, ref: "" });
  }
  renderQuick();
});
function qbSave() {
  var bill = qbActive;
  qbSyncSide();
  if (!bill.order.length) { setMsg("qbMsg", "Add at least one item.", true); return; }
  var t = qbTotals(bill);
  var lines = bill.order.map(function (id) {
    var e = bill.items[id];
    if (e.custom) return { qty: 1, rate_paisa: e.ratePaisa || 0, description: e.name };
    var c = qbLineCalc(bill, e);
    return { item_id: +id, qty: e.qty, rate_paisa: c.rate, discount_paisa: c.disc, tax_paisa: c.tax };
  });
  var payload = { type: "sale", party_id: bill.partyId || null, lines: lines,
                  discount_paisa: Math.min(bill.billDiscountPaisa || 0, t.sub),
                  tax_paisa: t.billTax, description: bill.remarks || "" };
  if (bill.loyalty > 0) payload.loyalty_points_used = bill.loyalty;
  if (bill.useSplit) {
    var rows = bill.payRows.filter(function (p) { return p.amount_paisa > 0; }).map(function (p) {
      var row = { mode: p.mode, amount_paisa: p.amount_paisa };
      if (p.mode === "bank") { row.bank_account_id = p.bank_account_id; row.reference_no = p.ref || ""; }
      if (p.mode === "cheque") row.cheque_no = p.ref || "";
      return row;
    });
    if (rows.length) payload.payments = rows;
  }
  if (!payload.payments) {
    var paid = bill.partial ? Math.min(bill.received || 0, t.total) : t.total;
    if (bill.payMode === "cheque") payload.payments = [{ mode: "cheque", amount_paisa: paid }];
    else if (bill.payMode && bill.payMode.indexOf("bank:") === 0) {
      payload.payments = [{ mode: "bank", bank_account_id: parseInt(bill.payMode.slice(5), 10), amount_paisa: paid }];
    } else payload.paid_paisa = paid;
  }
  apiCreate("invoice", payload).then(function (r) {
    if (r.queued) {
      setMsg("qbMsg", "Saved on this device — it will sync when online. The PDF will be available after sync.");
    } else {
      setMsg("qbMsg", "Bill " + (r.body.invoice ? r.body.invoice.invoiceNo : "") + " saved.");
      if (r.body.invoice) downloadInvoicePdf(r.body.invoice.id, false);
    }
    qbBills = qbBills.filter(function (b) { return b !== bill; });
    qbActive = null;
    qbSelectedRow = null;
    qbEnsure();
    renderQuick();
    loadDashboard();
  }).catch(function (e) { setMsg("qbMsg", e.message, true); });
}
$("qbBtnSavePrint").addEventListener("click", qbSave);

$("btnPreviewPdf").addEventListener("click", function () {
  if (lastSavedInvoice) downloadInvoicePdf(lastSavedInvoice.id, false);
});
$("btnPreviewThermal").addEventListener("click", function () {
  if (lastSavedInvoice) downloadInvoicePdf(lastSavedInvoice.id, true);
});
$("btnPreviewPrint").addEventListener("click", function () { window.print(); });
$("btnPreviewWhats").addEventListener("click", function () {
  if (lastSavedInvoice) shareInvoiceWhatsApp(lastSavedInvoice);
});
$("btnPreviewBack").addEventListener("click", function () { go("dashboard"); });
$("btnPreviewGmail").addEventListener("click", function () {
  if (!lastSavedInvoice) return;
  var inv = lastSavedInvoice;
  var subject = encodeURIComponent(DOC_META[inv.type].title + " " + inv.invoiceNo);
  var body = encodeURIComponent("Your bill of " + fmtRs(inv.totalPaisa) + " is ready. PDF: " +
    location.origin + "/api/invoices/" + inv.id + "/pdf");
  window.open("https://mail.google.com/mail/?view=cm&su=" + subject + "&body=" + body, "_blank");
});
$("btnPreviewMsg").addEventListener("click", function () {
  if (!lastSavedInvoice) return;
  var inv = lastSavedInvoice;
  var text = DOC_META[inv.type].title + " " + inv.invoiceNo + " of " + fmtRs(inv.totalPaisa) +
    ". PDF: " + location.origin + "/api/invoices/" + inv.id + "/pdf";
  if (navigator.share) navigator.share({ text: text }).catch(function () {});
  else window.open("sms:?&body=" + encodeURIComponent(text), "_blank");
});
