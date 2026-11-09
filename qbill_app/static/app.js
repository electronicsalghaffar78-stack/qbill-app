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
function fmtRs(paisa) {
  var neg = paisa < 0; paisa = Math.abs(paisa || 0);
  var rupees = Math.floor(paisa / 100), ps = paisa % 100;
  var s = rupees.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return (neg ? "-" : "") + "Rs " + s + "." + (ps < 10 ? "0" : "") + ps;
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

/* ------------------------------------------------------------ syncing -- */
var syncing = false;
function updateSyncPill() {
  var pill = $("syncPill");
  var pending = outbox().length + localBackups().length;
  $("offlineNote").classList.toggle("hidden", navigator.onLine);
  if (!navigator.onLine) {
    pill.textContent = "Offline · Auto-sync OFF — kaam chalta rahega";
    pill.className = "pill pill-off";
  } else if (pending > 0) {
    pill.textContent = "Online · Auto-sync ON · " + pending + " pending";
    pill.className = "pill pill-pending";
  } else {
    pill.textContent = "Online · Auto-sync ON";
    pill.className = "pill pill-on";
  }
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
   - "Create an account" (stepSignup): email -> code -> create password.
   - "Forgot your password?" (stepForgot): email -> code -> stepReset,
     a NEW password for the SAME account (needs an OTP-fresh session).
   - "Email me a login code instead" (stepEmail): the original code
     route; after the code, a locked session lands on stepWelcome
     (welcome back) to finish with the password.
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
var AUTH_STEPS = ["stepLogin", "stepEmail", "stepSignup", "stepForgot",
  "stepCode", "stepCreate", "stepWelcome", "stepReset"];
function showAuthStep(step) {
  $("appView").classList.add("hidden");
  $("btnLock").classList.add("hidden");
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
function showApp() {
  $("authModal").classList.add("hidden");
  $("appView").classList.remove("hidden");
  $("btnLock").classList.remove("hidden");
  $("btnLogout").classList.remove("hidden");
  $("ownerMailCard").classList.toggle("hidden", !(me && me.isOwner));
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
    if (!body.hasAppPassword) {
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
$("btnLoginSignup").addEventListener("click", function () { showAuthStep("stepSignup"); });
$("btnCodeInstead").addEventListener("click", function () { showAuthStep("stepEmail"); });
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
$("btnLogout").addEventListener("click", function () {
  apiPost("/api/auth/logout", {}).catch(function () {}).then(function () {
    setToken(null);
    forgotIntent = false;
    showAuth();
  });
});

/* ------------------------------------------------------------ screens -- */
var currentScreen = "dashboard";
function showScreen(name) {
  currentScreen = name;
  document.querySelectorAll(".screen").forEach(function (s) { s.classList.add("hidden"); });
  var el = $("screen-" + name);
  if (el) el.classList.remove("hidden");
  document.querySelectorAll(".navbtn").forEach(function (b) {
    b.classList.toggle("active", b.getAttribute("data-nav") === name);
  });
  loadScreen(name);
}
document.querySelectorAll(".navbtn").forEach(function (b) {
  b.addEventListener("click", function () { showScreen(b.getAttribute("data-nav")); });
});
function loadScreen(name, silent) {
  if (name === "dashboard") loadDashboard();
  else if (name === "parties") loadParties();
  else if (name === "items") loadItems();
  else if (name === "sale") { loadSaleForm(); loadInvoices(); }
  else if (name === "payments") { loadPayments(); fillPartySelects(); }
  else if (name === "expenses") loadExpenses();
  else if (name === "settings") { loadProfile(); loadBackups(); if (me && me.isOwner) loadMailConfig(); }
}

/* ---------------------------------------------------------- dashboard -- */
function loadDashboard() {
  apiGet("/api/dashboard", "dashboard").then(function (r) {
    var d = r.body.dashboard;
    $("dToday").textContent = fmtRs(d.todaySalesPaisa);
    $("dMonth").textContent = fmtRs(d.monthSalesPaisa);
    $("dReceivable").textContent = fmtRs(d.totalReceivablePaisa);
    $("dCash").textContent = fmtRs(d.cashInHandPaisa);
    $("dStock").textContent = fmtRs(d.stockValuePaisa);
  }).catch(function () {});
  apiGet("/api/pnl", "pnl").then(function (r) {
    $("dProfit").textContent = fmtRs(r.body.pnl.profitPaisa);
  }).catch(function () {});
}

/* ------------------------------------------------------------ parties -- */
function rowHtml(main, sub, amt, cls) {
  return '<div><div class="main">' + esc(main) + '</div>' +
    (sub ? '<div class="sub">' + esc(sub) + "</div>" : "") + "</div>" +
    (amt != null ? '<div class="amt ' + (cls || "") + '">' + amt + "</div>" : "");
}
function loadParties() {
  apiGet("/api/parties", "parties").then(function (r) {
    var list = r.body.parties || [];
    $("partyList").innerHTML = list.length ? "" : '<p class="empty">No parties yet.</p>';
    list.forEach(function (p) {
      var btn = document.createElement("button");
      btn.className = "row"; btn.type = "button";
      btn.innerHTML = rowHtml(p.name, p.phone || "", fmtRs(p.balancePaisa),
        p.balancePaisa > 0 ? "neg" : "");
      btn.addEventListener("click", function () { openLedger(p); });
      $("partyList").appendChild(btn);
    });
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
$("partyForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  apiCreate("party", {
    name: $("partyName").value.trim(), phone: $("partyPhone").value.trim(),
    opening_balance_paisa: rsToPaisa($("partyOpening").value)
  }).then(function (r) {
    $("partyForm").reset(); $("partyOpening").value = 0;
    setMsg("appMsg", r.queued ? "Saved on this device — will sync when online." : "Party added.");
    loadParties();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});

/* ------------------------------------------------------------- ledger -- */
function openLedger(party) {
  showScreen("parties");
  document.querySelectorAll(".screen").forEach(function (s) { s.classList.add("hidden"); });
  $("screen-ledger").classList.remove("hidden");
  currentScreen = "ledger";
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
function loadItems() {
  apiGet("/api/items", "items").then(function (r) {
    var list = r.body.items || [];
    $("itemList").innerHTML = list.length ? "" : '<p class="empty">No items yet.</p>';
    list.forEach(function (i) {
      var div = document.createElement("div");
      div.className = "row";
      div.innerHTML = rowHtml(i.name,
        "Sale " + fmtRs(i.salePricePaisa) + " · Purchase " + fmtRs(i.purchasePricePaisa),
        "Stock: " + i.stockQty);
      $("itemList").appendChild(div);
    });
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
$("itemForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  apiCreate("item", {
    name: $("itemName").value.trim(),
    sale_price_paisa: rsToPaisa($("itemSale").value),
    purchase_price_paisa: rsToPaisa($("itemPurchase").value),
    stock_qty: parseInt($("itemStock").value || "0", 10)
  }).then(function (r) {
    $("itemForm").reset();
    setMsg("appMsg", r.queued ? "Saved on this device — will sync when online." : "Item added.");
    loadItems();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});

/* --------------------------------------------------------------- sale -- */
function fillPartySelects() {
  apiGet("/api/parties", "parties").then(function (r) {
    var parties = r.body.parties || [];
    [["saleParty", true], ["payParty", false]].forEach(function (pair) {
      var sel = $(pair[0]); if (!sel) return;
      var keep = sel.value;
      sel.innerHTML = pair[1] ? '<option value="">Cash Sale</option>' : '<option value="">— None —</option>';
      parties.forEach(function (p) {
        var o = document.createElement("option");
        o.value = p.id; o.textContent = p.name;
        sel.appendChild(o);
      });
      sel.value = keep;
    });
  }).catch(function () {});
}
function addSaleLine(item) {
  var wrap = document.createElement("div");
  wrap.className = "sale-line";
  wrap.innerHTML =
    '<label>Item<select class="sl-item"></select></label>' +
    '<label>Qty<input class="sl-qty" type="number" step="1" min="1" value="1"></label>' +
    '<label>Rate (Rs)<input class="sl-rate" type="number" step="0.01" min="0" value="0"></label>' +
    '<button class="rm" type="button" title="Remove line">×</button>';
  var sel = wrap.querySelector(".sl-item");
  var customs = document.createElement("option");
  customs.value = ""; customs.textContent = "— Custom line —";
  sel.appendChild(customs);
  ((cacheGet("items") || {}).items || []).forEach(function (i) {
    var o = document.createElement("option");
    o.value = i.id; o.textContent = i.name; o.setAttribute("data-rate", i.salePricePaisa);
    sel.appendChild(o);
  });
  var desc = document.createElement("input");
  desc.className = "sl-desc"; desc.placeholder = "Description (for custom line)";
  desc.style.gridColumn = "1 / -1";
  wrap.appendChild(desc);
  sel.addEventListener("change", function () {
    var o = sel.options[sel.selectedIndex];
    if (o && o.getAttribute("data-rate")) wrap.querySelector(".sl-rate").value = (o.getAttribute("data-rate") / 100).toFixed(2);
    calcSaleTotal();
  });
  wrap.querySelector(".rm").addEventListener("click", function () { wrap.remove(); calcSaleTotal(); });
  wrap.querySelector(".sl-qty").addEventListener("input", calcSaleTotal);
  wrap.querySelector(".sl-rate").addEventListener("input", calcSaleTotal);
  if (item) { sel.value = item; sel.dispatchEvent(new Event("change")); }
  $("saleLines").appendChild(wrap);
}
function saleLinesData() {
  var lines = [];
  document.querySelectorAll(".sale-line").forEach(function (w) {
    var itemId = w.querySelector(".sl-item").value;
    var line = {
      qty: parseInt(w.querySelector(".sl-qty").value || "0", 10),
      rate_paisa: rsToPaisa(w.querySelector(".sl-rate").value)
    };
    if (itemId) line.item_id = parseInt(itemId, 10);
    var d = w.querySelector(".sl-desc").value.trim();
    if (d) line.description = d;
    if (line.qty > 0) lines.push(line);
  });
  return lines;
}
function calcSaleTotal() {
  var total = 0;
  saleLinesData().forEach(function (l) { total += l.qty * l.rate_paisa; });
  total -= rsToPaisa($("saleDiscount").value);
  $("saleTotal").textContent = fmtRs(Math.max(total, 0));
}
function loadSaleForm() {
  fillPartySelects();
  apiGet("/api/items", "items").then(function () {
    if (!$("saleLines").children.length) addSaleLine();
  }).catch(function () { if (!$("saleLines").children.length) addSaleLine(); });
}
$("btnAddLine").addEventListener("click", function () { addSaleLine(); });
$("saleDiscount").addEventListener("input", calcSaleTotal);
var lastInvoiceId = null;
$("saleForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  var lines = saleLinesData();
  if (!lines.length) { setMsg("appMsg", "Add at least one line.", true); return; }
  var payload = {
    type: "sale",
    party_id: $("saleParty").value ? parseInt($("saleParty").value, 10) : null,
    discount_paisa: rsToPaisa($("saleDiscount").value),
    paid_paisa: rsToPaisa($("salePaid").value),
    lines: lines
  };
  apiCreate("invoice", payload).then(function (r) {
    $("saleDone").classList.remove("hidden");
    if (r.queued) {
      lastInvoiceId = null;
      $("saleDoneText").textContent = "Invoice saved on this device — it will sync when you are online. PDF will be available after sync.";
      $("btnSalePdf").classList.add("hidden");
    } else {
      lastInvoiceId = r.body.invoice.id;
      $("saleDoneText").textContent = "Invoice " + r.body.invoice.invoiceNo + " saved. Total " +
        fmtRs(r.body.invoice.totalPaisa) + " · Balance " + fmtRs(r.body.invoice.balancePaisa) + ".";
      $("btnSalePdf").classList.remove("hidden");
    }
    loadInvoices(); loadDashboard();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});
$("btnSaleNew").addEventListener("click", function () {
  $("saleDone").classList.add("hidden");
  $("saleLines").innerHTML = ""; addSaleLine();
  $("saleDiscount").value = 0; $("salePaid").value = 0; calcSaleTotal();
});
$("btnSalePdf").addEventListener("click", function () {
  if (!lastInvoiceId) return;
  fetch("/api/invoices/" + lastInvoiceId + "/pdf", { headers: authHeaders() })
    .then(function (resp) { if (!resp.ok) throw new Error("PDF failed (" + resp.status + ")"); return resp.blob(); })
    .then(function (blob) {
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "qbill-invoice.pdf";
      a.click();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 4000);
    }).catch(function (e) { setMsg("appMsg", e.message, true); });
});
function loadInvoices() {
  apiGet("/api/invoices", "invoices").then(function (r) {
    var list = (r.body.invoices || []).slice().reverse();
    $("invoiceList").innerHTML = list.length ? "" : '<p class="empty">No invoices yet.</p>';
    list.forEach(function (inv) {
      var div = document.createElement("div");
      div.className = "row";
      div.innerHTML = rowHtml(inv.invoiceNo + " · " + (inv.type === "purchase" ? "Purchase" : "Sale"),
        (inv.createdAt || "").slice(0, 10) + " · Bal " + fmtRs(inv.balancePaisa), fmtRs(inv.totalPaisa));
      $("invoiceList").appendChild(div);
    });
  }).catch(function () {});
}

/* ----------------------------------------------------------- payments -- */
function loadPayments() {
  apiGet("/api/payments", "payments").then(function (r) {
    var parties = {};
    ((cacheGet("parties") || {}).parties || []).forEach(function (p) { parties[p.id] = p.name; });
    var list = (r.body.payments || []).slice().reverse();
    $("paymentList").innerHTML = list.length ? "" : '<p class="empty">No payments yet.</p>';
    list.forEach(function (p) {
      var div = document.createElement("div");
      div.className = "row";
      div.innerHTML = rowHtml(
        (p.direction === "in" ? "Received" : "Paid") + (p.partyId ? " · " + (parties[p.partyId] || "") : ""),
        (p.createdAt || "").slice(0, 10), fmtRs(p.amountPaisa), p.direction === "in" ? "pos" : "neg");
      $("paymentList").appendChild(div);
    });
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
$("paymentForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  apiCreate("payment", {
    party_id: $("payParty").value ? parseInt($("payParty").value, 10) : null,
    direction: $("payDirection").value,
    amount_paisa: rsToPaisa($("payAmount").value)
  }).then(function (r) {
    $("paymentForm").reset();
    setMsg("appMsg", r.queued ? "Saved on this device — will sync when online." : "Payment saved.");
    loadPayments();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});

/* ----------------------------------------------------------- expenses -- */
function loadExpenses() {
  apiGet("/api/expenses", "expenses").then(function (r) {
    var list = (r.body.expenses || []).slice().reverse();
    $("expenseList").innerHTML = list.length ? "" : '<p class="empty">No expenses yet.</p>';
    list.forEach(function (e) {
      var div = document.createElement("div");
      div.className = "row";
      div.innerHTML = rowHtml(e.title, (e.category ? e.category + " · " : "") + (e.createdAt || "").slice(0, 10),
        fmtRs(e.amountPaisa), "neg");
      $("expenseList").appendChild(div);
    });
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
}
$("expenseForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  apiCreate("expense", {
    title: $("expTitle").value.trim(),
    category: $("expCategory").value.trim(),
    amount_paisa: rsToPaisa($("expAmount").value)
  }).then(function (r) {
    $("expenseForm").reset();
    setMsg("appMsg", r.queued ? "Saved on this device — will sync when online." : "Expense saved.");
    loadExpenses();
  }).catch(function (e) { setMsg("appMsg", e.message, true); });
});

/* ----------------------------------------------------------- settings -- */
function loadProfile() {
  apiGet("/api/business", "business").then(function (r) {
    var b = r.body.business || {};
    $("profName").value = b.name || "";
    $("profAddress").value = b.address || "";
    $("profPhone").value = b.phone || "";
    $("profEmail").value = b.email || "";
  }).catch(function () {});
}
$("profileForm").addEventListener("submit", function (ev) {
  ev.preventDefault();
  apiPut("/api/business", {
    name: $("profName").value.trim(), address: $("profAddress").value.trim(),
    phone: $("profPhone").value.trim(), email: $("profEmail").value.trim()
  }).then(function (r) {
    cacheSet("business", { business: r.business });
    setMsg("appMsg", "Profile saved. It appears on your invoices and PDFs.");
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

/* -------------------------------------------------------------- boot --- */
/* A login WITHOUT "Remember me" ends when the page goes away: lock the
   session (best effort, keepalive) so reopening this tab asks for the
   password again. Remembered and code sign-ins stay open on the device. */
window.addEventListener("pagehide", function () {
  if (!token() || !lsGet("qbill_token_temp", false)) return;
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
