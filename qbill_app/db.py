"""Database layer: external Postgres in production, SQLite file for dev/test.

SQL is written once with `?` placeholders and translated to `%s` for
Postgres. Rows come back as plain dicts in both dialects. Money columns are
integers (paisa) in both dialects; timestamps are ISO-8601 UTC strings
written by the app (no DB-side defaults, so both dialects behave the same).

Schema creation is idempotent (CREATE TABLE IF NOT EXISTS) and runs at app
startup — safe to run on every boot.
"""
import sqlite3
from contextlib import contextmanager

_state = {}


def init_app(cfg):
    url = cfg.database_url
    if url:
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        _state.clear()
        _state.update(mode="postgres", url=url)
    else:
        _state.clear()
        _state.update(mode="sqlite", path=cfg.sqlite_path)
        print(
            "WARNING: DATABASE_URL is not set — QBill is using a local SQLite "
            "file ({}). This fallback is for DEV/TEST ONLY; production must "
            "set DATABASE_URL to an external Postgres.".format(cfg.sqlite_path),
            flush=True,
        )
    create_schema()


def dialect():
    return _state.get("mode", "sqlite")


def _connect_raw():
    if _state["mode"] == "postgres":
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(_state["url"], row_factory=dict_row)
    conn = sqlite3.connect(_state["path"])
    conn.row_factory = sqlite3.Row
    return conn


def _ph(sql):
    return sql.replace("?", "%s") if _state["mode"] == "postgres" else sql


class Tx:
    """One open transaction; all helpers run on the same connection."""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        return self.conn.execute(_ph(sql), tuple(params))

    def query_all(self, sql, params=()):
        cur = self.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def query_one(self, sql, params=()):
        rows = self.query_all(sql, params)
        return rows[0] if rows else None

    def insert(self, sql, params=()):
        """INSERT ... RETURNING id (supported by Postgres and SQLite >= 3.35)."""
        cur = self.execute(sql, params)
        row = cur.fetchone()
        return dict(row)["id"]


@contextmanager
def transaction():
    conn = _connect_raw()
    try:
        yield Tx(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query_all(sql, params=()):
    with transaction() as tx:
        return tx.query_all(sql, params)


def query_one(sql, params=()):
    with transaction() as tx:
        return tx.query_one(sql, params)


def run(sql, params=()):
    with transaction() as tx:
        tx.execute(sql, params)


def insert(sql, params=()):
    with transaction() as tx:
        return tx.insert(sql, params)


# --------------------------------------------------------------- schema ----
def _ddl():
    pg = _state["mode"] == "postgres"
    ID = "BIGSERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    MONEY = "BIGINT" if pg else "INTEGER"
    return [
        """CREATE TABLE IF NOT EXISTS site_accounts (
             id {ID},
             email TEXT NOT NULL UNIQUE,
             is_owner INTEGER NOT NULL DEFAULT 0,
             pw_salt TEXT,
             pw_hash TEXT,
             created_at TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS otp_codes (
             id {ID},
             email TEXT NOT NULL,
             code_hash TEXT NOT NULL,
             expires_at TEXT NOT NULL,
             used INTEGER NOT NULL DEFAULT 0,
             created_at TEXT NOT NULL
           )""",
        "CREATE INDEX IF NOT EXISTS idx_otp_codes_email ON otp_codes(email)",
        """CREATE TABLE IF NOT EXISTS sessions (
             id {ID},
             token_hash TEXT NOT NULL UNIQUE,
             account_id INTEGER NOT NULL REFERENCES site_accounts(id),
             unlocked INTEGER NOT NULL DEFAULT 0,
             otp_fresh INTEGER NOT NULL DEFAULT 0,
             expires_at TEXT NOT NULL,
             created_at TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS access_log (
             id {ID},
             account_id INTEGER,
             email TEXT,
             ip TEXT,
             event TEXT,
             created_at TEXT NOT NULL
           )""",
        "CREATE INDEX IF NOT EXISTS idx_access_log_account "
        "ON access_log(account_id, event)",
        """CREATE TABLE IF NOT EXISTS mail_config (
             key TEXT PRIMARY KEY,
             value TEXT,
             updated_at TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS businesses (
             id {ID},
             account_id INTEGER NOT NULL REFERENCES site_accounts(id),
             name TEXT NOT NULL,
             address TEXT,
             phone TEXT,
             email TEXT,
             created_at TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS parties (
             id {ID},
             business_id INTEGER NOT NULL REFERENCES businesses(id),
             name TEXT NOT NULL,
             phone TEXT,
             opening_balance_paisa {M} NOT NULL DEFAULT 0,
             client_ref TEXT,
             created_at TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS items (
             id {ID},
             business_id INTEGER NOT NULL REFERENCES businesses(id),
             name TEXT NOT NULL,
             sale_price_paisa {M} NOT NULL DEFAULT 0,
             purchase_price_paisa {M} NOT NULL DEFAULT 0,
             stock_qty INTEGER NOT NULL DEFAULT 0,
             client_ref TEXT,
             created_at TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS invoices (
             id {ID},
             business_id INTEGER NOT NULL REFERENCES businesses(id),
             party_id INTEGER REFERENCES parties(id),
             invoice_no TEXT NOT NULL,
             type TEXT NOT NULL DEFAULT 'sale',
             subtotal_paisa {M} NOT NULL,
             discount_paisa {M} NOT NULL DEFAULT 0,
             tax_paisa {M} NOT NULL DEFAULT 0,
             total_paisa {M} NOT NULL,
             paid_paisa {M} NOT NULL DEFAULT 0,
             balance_paisa {M} NOT NULL,
             client_ref TEXT,
             created_at TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS invoice_lines (
             id {ID},
             invoice_id INTEGER NOT NULL REFERENCES invoices(id),
             item_id INTEGER REFERENCES items(id),
             description TEXT NOT NULL,
             qty INTEGER NOT NULL,
             rate_paisa {M} NOT NULL,
             amount_paisa {M} NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS payments (
             id {ID},
             business_id INTEGER NOT NULL REFERENCES businesses(id),
             party_id INTEGER REFERENCES parties(id),
             invoice_id INTEGER REFERENCES invoices(id),
             direction TEXT NOT NULL DEFAULT 'in',
             amount_paisa {M} NOT NULL,
             client_ref TEXT,
             created_at TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS expenses (
             id {ID},
             business_id INTEGER NOT NULL REFERENCES businesses(id),
             title TEXT NOT NULL,
             category TEXT NOT NULL DEFAULT '',
             amount_paisa {M} NOT NULL,
             client_ref TEXT,
             created_at TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS auto_backups (
             id {ID},
             business_id INTEGER REFERENCES businesses(id),
             payload TEXT NOT NULL,
             emailed INTEGER NOT NULL DEFAULT 0,
             client_ref TEXT,
             created_at TEXT NOT NULL
           )""",
    ]


# Columns added after the foundation shipped (Phase 1 Step 2). Fresh
# databases get them from the DDL above; older databases get them from
# _ensure_column below — both paths end with the same schema.
_ADDED_COLUMNS = [
    ("businesses", "address", "TEXT"),
    ("businesses", "phone", "TEXT"),
    ("businesses", "email", "TEXT"),
    ("parties", "client_ref", "TEXT"),
    ("items", "client_ref", "TEXT"),
    ("invoices", "type", "TEXT NOT NULL DEFAULT 'sale'"),
    ("invoices", "client_ref", "TEXT"),
    ("payments", "client_ref", "TEXT"),
    ("expenses", "category", "TEXT NOT NULL DEFAULT ''"),
    ("expenses", "client_ref", "TEXT"),
    ("auto_backups", "emailed", "INTEGER NOT NULL DEFAULT 0"),
    ("auto_backups", "client_ref", "TEXT"),
    ("sessions", "otp_fresh", "INTEGER NOT NULL DEFAULT 0"),
    # Owner-approval gate (anti-sharing): accounts created BEFORE this
    # change keep approved=1 from the DEFAULT; the signup path sets 0
    # explicitly for new non-owner accounts.
    ("site_accounts", "approved", "INTEGER NOT NULL DEFAULT 1"),
    ("site_accounts", "signup_ip", "TEXT"),
    ("site_accounts", "last_ip", "TEXT"),
    # 'ip_match' when the signup was auto-rejected as a duplicate-IP
    # signup; NULL for normal accounts. The owner can clear it by
    # approving the account.
    ("site_accounts", "reject_reason", "TEXT"),
]

# One non-null client_ref per business per table: the offline outbox replay
# relies on this to turn a repeated create into a no-op. NULLs never clash
# (both dialects treat NULLs as distinct in unique indexes).
_CLIENT_REF_INDEXES = [
    ("uq_parties_client_ref", "parties"),
    ("uq_items_client_ref", "items"),
    ("uq_invoices_client_ref", "invoices"),
    ("uq_payments_client_ref", "payments"),
    ("uq_expenses_client_ref", "expenses"),
    ("uq_auto_backups_client_ref", "auto_backups"),
]


def _existing_columns(tx, table):
    if _state["mode"] == "postgres":
        rows = tx.query_all(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ?",
            (table,),
        )
        return {r["column_name"] for r in rows}
    rows = tx.query_all("PRAGMA table_info({})".format(table))
    return {r["name"] for r in rows}


def _ensure_column(tx, table, column, ddl):
    if column not in _existing_columns(tx, table):
        tx.execute(
            "ALTER TABLE {} ADD COLUMN {} {}".format(table, column, ddl)
        )


def create_schema():
    pg = _state["mode"] == "postgres"
    ID = "BIGSERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    MONEY = "BIGINT" if pg else "INTEGER"
    with transaction() as tx:
        for stmt in _ddl():
            tx.execute(stmt.format(ID=ID, M=MONEY))
        for table, column, ddl in _ADDED_COLUMNS:
            _ensure_column(tx, table, column, ddl)
        for index_name, table in _CLIENT_REF_INDEXES:
            tx.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS {} "
                "ON {}(business_id, client_ref)".format(index_name, table)
            )
