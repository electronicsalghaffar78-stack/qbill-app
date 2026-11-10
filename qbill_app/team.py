"""Team & Roles — Sync, Share & Backup (Vyapar).

The business owner adds team members by name + phone/email and a role.
Each role carries a permission matrix (VIEW / CREATE / EDIT / SHARE /
DELETE per module). When a member signs in with their own QBill
account, the business appears under "Companies Shared with Me"; opening
it switches their session into that business (sessions.active_
business_id), and every API call is then checked against their matrix
by the before-request guard below. Owners working in their own
business are never restricted.
"""
import json

from flask import Blueprint, g, jsonify, request

from . import db
from .auth import require_approved, require_session, require_unlocked
from .crud import _business_id
from .security import ApiError, now_iso

bp = Blueprint("team", __name__, url_prefix="/api")

MODULES = [
    "All Transactions", "Sale", "Purchase", "Payment-In", "Payment-Out",
    "Expenses", "Other Income", "Parties", "Items", "Stock Transfer",
    "Journal Entries", "Cash & Bank", "Online Store", "Reports",
    "Settings",
]
ACTIONS = ("view", "create", "edit", "share", "delete")


def _matrix(**grants):
    """grants: module -> tuple of allowed actions."""
    matrix = {m: {a: False for a in ACTIONS} for m in MODULES}
    for module, actions in grants.items():
        for action in actions:
            matrix[module][action] = True
    return matrix


_ALL = ACTIONS
_VC = ("view", "create")
_V = ("view",)
_VCS = ("view", "create", "share")
_VCE = ("view", "create", "edit")
_VCES = ("view", "create", "edit", "share")

ROLE_PRESETS = {
    "Secondary Admin": _matrix(**{m: _ALL for m in MODULES}),
    "Salesman": _matrix(**{
        "Sale": _VCES, "Payment-In": _VC, "Parties": _VC, "Items": _V,
        "All Transactions": _V,
    }),
    "Biller": _matrix(**{
        "Sale": _VC, "Purchase": _VC, "Payment-In": _VC, "Payment-Out": _VC,
        "Parties": _V, "Items": _V, "All Transactions": _V,
    }),
    "Biller and Salesman": _matrix(**{
        "Sale": _VCES, "Purchase": _VC, "Payment-In": _VC, "Payment-Out": _VC,
        "Parties": _VC, "Items": _V, "All Transactions": _V,
    }),
    "CA/Accountant": _matrix(**{
        "All Transactions": ("view", "share"), "Reports": ("view", "share"),
        "Journal Entries": _V, "Parties": _V, "Items": _V,
        "Cash & Bank": _V, "Expenses": _V, "Other Income": _V,
    }),
    "CA/Accountant (Edit Access)": _matrix(**{
        "All Transactions": _ALL, "Reports": ("view", "share"),
        "Journal Entries": _VCES, "Settings": ("view", "edit"),
        "Parties": _VCE, "Items": _VCE, "Cash & Bank": _V,
        "Expenses": _VCE, "Other Income": _VCE,
    }),
    "Stock Keeper": _matrix(**{
        "Purchase": _VC, "Stock Transfer": _VCS, "Items": ("view", "edit"),
        "Parties": _V, "All Transactions": _V,
    }),
    "Field Salesman": _matrix(**{
        "Sale": _VC, "Parties": _VC, "Items": _V, "Payment-In": _VC,
    }),
}
ROLES = list(ROLE_PRESETS)


def _body():
    return request.get_json(silent=True) or {}


def _member_dict(row):
    try:
        perms = json.loads(row["perms"] or "{}")
    except ValueError:
        perms = {}
    return {
        "id": row["id"],
        "name": row["name"],
        "contact": row["contact"],
        "role": row["role"],
        "status": row["status"],
        "perms": perms,
    }


def memberships_for(account_id):
    rows = db.query_all(
        "SELECT m.*, b.name AS business_name FROM team_members m "
        "JOIN businesses b ON b.id = m.business_id "
        "JOIN site_accounts a ON a.email = m.contact "
        "WHERE a.id = ? AND m.status = 'active'",
        (account_id,),
    )
    return [
        {"businessId": r["business_id"], "businessName": r["business_name"],
         "role": r["role"], "memberId": r["id"]}
        for r in rows
    ]


def active_membership(tx, account_id, business_id):
    return tx.query_one(
        "SELECT m.* FROM team_members m "
        "JOIN site_accounts a ON a.email = m.contact "
        "WHERE a.id = ? AND m.business_id = ? AND m.status = 'active'",
        (account_id, business_id),
    )


# ------------------------------------------------- permission enforcement --
def _module_action_for_request():
    """Map the current request to a (module, action) pair, or None when
    the route is not role-guarded."""
    path = request.path
    method = request.method
    action = {"GET": "view", "POST": "create", "PUT": "edit",
              "PATCH": "edit", "DELETE": "delete"}.get(method, "view")
    if path.endswith("/pdf") and method == "GET":
        return "All Transactions", "share"
    if path.startswith("/api/invoices"):
        if method == "POST" and path == "/api/invoices":
            doc_type = str((_body().get("type") or "sale")).lower()
            module = {"sale": "Sale", "purchase": "Purchase"}.get(
                doc_type, "All Transactions")
            return module, "create"
        if "/convert" in path:
            return "Sale", "create"
        return "All Transactions", action
    if path.startswith("/api/payments"):
        if method == "POST":
            direction = str((_body().get("direction") or "in")).lower()
            return ("Payment-In" if direction == "in" else "Payment-Out"), "create"
        return "All Transactions", action
    if path.startswith("/api/parties"):
        return "Parties", action
    if path.startswith("/api/items"):
        return "Items", action
    if path.startswith("/api/expenses"):
        return "Expenses", action
    if path.startswith("/api/other-income"):
        return "Other Income", action
    if path.startswith("/api/stock-transfers"):
        return "Stock Transfer", action
    if path.startswith("/api/godowns"):
        return "Stock Transfer", action
    if path.startswith("/api/journal") or path.startswith("/api/accounts"):
        return "Journal Entries", action
    if path.startswith("/api/bank-accounts") or path.startswith("/api/cheques") \
            or path.startswith("/api/cheque-payments") \
            or path.startswith("/api/cash-adjustments") \
            or path.startswith("/api/loans"):
        return "Cash & Bank", action
    if path.startswith("/api/store"):
        return "Online Store", action
    if path.startswith("/api/reports") or path in ("/api/dashboard", "/api/pnl"):
        return "Reports", "view"
    if path.startswith("/api/settings") or path.startswith("/api/terms") \
            or path.startswith("/api/team") or path == "/api/business" \
            or path.startswith("/api/backups") \
            or path.startswith("/api/expense-categories") \
            or path.startswith("/api/expense-items"):
        return "Settings", action
    return None


@bp.before_app_request
def _enforce_member_permissions():
    """Runs for every request; only bites when the session is working
    inside a business it does not own (team-member mode)."""
    if not request.path.startswith("/api/"):
        return None
    if request.path.startswith("/api/auth") or request.path.startswith("/api/public"):
        return None
    # Read-only bootstrap data every signed-in user needs (app config,
    # business profile, terms picker, switching back to your own
    # company) is never role-blocked; only changes are.
    if request.path == "/api/team/switch":
        return None
    if request.method == "GET" and request.path in (
        "/api/settings", "/api/business", "/api/terms", "/api/team",
        "/api/team/roles",
    ):
        return None
    session = getattr(g, "session", None)
    if session is None:
        from .auth import load_session

        session = load_session()
        if session is None:
            return None  # the route's own guards answer 401
        g.session = session
    active = session.get("active_business_id")
    if not active:
        return None
    with db.transaction() as tx:
        own = tx.query_one(
            "SELECT id FROM businesses WHERE id = ? AND account_id = ?",
            (active, session["account_id"]),
        )
        if own is not None:
            return None  # own business: no role limits
        member = active_membership(tx, session["account_id"], active)
    if member is None:
        return None  # _business_id falls back to the own business
    target = _module_action_for_request()
    if target is None:
        return None
    module, action = target
    try:
        perms = json.loads(member["perms"] or "{}")
    except ValueError:
        perms = {}
    allowed = (perms.get(module) or {}).get(action, False)
    if not allowed and module != "All Transactions":
        allowed = (perms.get("All Transactions") or {}).get(action, False)
    if not allowed:
        return jsonify(
            ok=False, code="forbidden_role",
            error="Your role ({}) does not allow this action.".format(member["role"]),
        ), 403
    return None


# --------------------------------------------------------------- endpoints --
@bp.get("/team/roles")
@require_session
@require_approved
@require_unlocked
def team_roles():
    return jsonify(ok=True, roles=ROLES, modules=MODULES, presets=ROLE_PRESETS)


@bp.get("/team")
@require_session
@require_approved
@require_unlocked
def list_team():
    with db.transaction() as tx:
        biz = _business_id(tx)
        rows = tx.query_all(
            "SELECT * FROM team_members WHERE business_id = ? ORDER BY id",
            (biz,),
        )
    return jsonify(ok=True, members=[_member_dict(r) for r in rows])


@bp.post("/team")
@require_session
@require_approved
@require_unlocked
def add_member():
    data = _body()
    name = str(data.get("name") or "").strip()
    contact = str(data.get("contact") or "").strip().lower()
    role = str(data.get("role") or "").strip()
    if not name:
        raise ApiError("Full Name is required.")
    if not contact:
        raise ApiError("Phone / E-mail is required.")
    if role not in ROLE_PRESETS:
        raise ApiError("Role must be one of: {}.".format(", ".join(ROLES)))
    perms = data.get("perms")
    if not isinstance(perms, dict):
        perms = ROLE_PRESETS[role]
    with db.transaction() as tx:
        biz = _business_id(tx)
        clash = tx.query_one(
            "SELECT id FROM team_members WHERE business_id = ? AND contact = ? "
            "AND status = 'active'",
            (biz, contact),
        )
        if clash is not None:
            raise ApiError("This person is already on the team.")
        mid = tx.insert(
            "INSERT INTO team_members (business_id, name, contact, role, perms, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, 'active', ?) RETURNING id",
            (biz, name, contact, role, json.dumps(perms), now_iso()),
        )
        row = tx.query_one("SELECT * FROM team_members WHERE id = ?", (mid,))
    return jsonify(ok=True, member=_member_dict(row)), 201


@bp.put("/team/<int:member_id>")
@require_session
@require_approved
@require_unlocked
def update_member(member_id):
    data = _body()
    with db.transaction() as tx:
        biz = _business_id(tx)
        row = tx.query_one(
            "SELECT * FROM team_members WHERE id = ? AND business_id = ?",
            (member_id, biz),
        )
        if row is None:
            raise ApiError("Team member not found.", status=404)
        role = str(data.get("role", row["role"]) or "").strip()
        if role not in ROLE_PRESETS:
            raise ApiError("Role must be one of: {}.".format(", ".join(ROLES)))
        perms = data.get("perms")
        if not isinstance(perms, dict):
            try:
                perms = json.loads(row["perms"] or "{}")
            except ValueError:
                perms = ROLE_PRESETS[role]
        status = str(data.get("status", row["status"]) or "active")
        if status not in ("active", "removed"):
            raise ApiError("Status must be active or removed.")
        tx.execute(
            "UPDATE team_members SET name = ?, role = ?, perms = ?, status = ? "
            "WHERE id = ?",
            (str(data.get("name", row["name"]) or "").strip() or row["name"],
             role, json.dumps(perms), status, member_id),
        )
        row = tx.query_one("SELECT * FROM team_members WHERE id = ?", (member_id,))
    return jsonify(ok=True, member=_member_dict(row))


@bp.post("/team/switch")
@require_session
@require_approved
def switch_business():
    """Open a shared company (or switch back to your own business)."""
    business_id = _body().get("business_id")
    with db.transaction() as tx:
        own = tx.query_one(
            "SELECT id FROM businesses WHERE id = ? AND account_id = ?",
            (business_id, g.session["account_id"]),
        )
        member = None
        if own is None:
            member = active_membership(tx, g.session["account_id"], business_id)
            if member is None:
                raise ApiError("This business has not been shared with you.",
                               status=403)
        tx.execute(
            "UPDATE sessions SET active_business_id = ? WHERE id = ?",
            (business_id, g.session["session_id"]),
        )
        biz = tx.query_one("SELECT name FROM businesses WHERE id = ?", (business_id,))
    return jsonify(
        ok=True,
        activeBusinessId=business_id,
        businessName=(biz or {}).get("name") or "",
        role=(member or {}).get("role") if member else None,
    )


@bp.get("/team/activity")
@require_session
@require_approved
@require_unlocked
def user_activity():
    """See User Activity: recent sign-ins of accounts on this team."""
    with db.transaction() as tx:
        biz = _business_id(tx)
        contacts = [
            r["contact"]
            for r in tx.query_all(
                "SELECT contact FROM team_members WHERE business_id = ?", (biz,)
            )
        ]
        rows = []
        if contacts:
            placeholders = ", ".join("?" for _ in contacts)
            rows = tx.query_all(
                "SELECT email, ip, event, created_at FROM access_log "
                "WHERE email IN ({}) ORDER BY id DESC LIMIT 50".format(placeholders),
                tuple(contacts),
            )
    return jsonify(
        ok=True,
        activity=[
            {"email": r["email"], "ip": r["ip"], "event": r["event"],
             "createdAt": r["created_at"]}
            for r in rows
        ],
    )
