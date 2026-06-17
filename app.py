from flask import Flask, request, jsonify
from functools import wraps
from datetime import datetime, timedelta, timezone
import os, json, secrets

app = Flask(__name__)

# ---------------------------------------------------------------------------
# DATABASE
# If DATABASE_URL is set (e.g. a free Neon/Supabase/Render Postgres), data is
# stored in Postgres and SURVIVES Render free-tier spin-downs and redeploys.
# If it is not set, we fall back to a local SQLite file (data is NOT durable
# on Render free tier -- only use SQLite for local testing).
# ---------------------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_PG = DATABASE_URL.startswith("postgres://") or DATABASE_URL.startswith("postgresql://")
PH = "%s" if USE_PG else "?"          # SQL parameter placeholder per backend

if USE_PG:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from psycopg2 import pool as _pgpool
    _POOL = _pgpool.ThreadedConnectionPool(1, 8, DATABASE_URL,
                                           cursor_factory=RealDictCursor)

    def get_db():
        conn = _POOL.getconn()
        conn.autocommit = True            # each statement commits; no stale transactions
        return conn

    def put_db(conn):
        try:
            _POOL.putconn(conn)
        except Exception:
            pass
else:
    import sqlite3
    DB = "database.db"

    def get_db():
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        return conn

    def put_db(conn):
        conn.close()


# ---- tiny query helpers that work on both backends ----
def q1(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    row = cur.fetchone()
    cur.close()
    return row

def qall(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    return rows

def ex(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    rc = cur.rowcount
    if not USE_PG:
        conn.commit()
    cur.close()
    return rc

def insert_ignore(conn, table, cols, vals, conflict):
    ph = ",".join([PH] * len(vals))
    cl = ",".join(cols)
    if USE_PG:
        ex(conn, f"INSERT INTO {table} ({cl}) VALUES ({ph}) "
                 f"ON CONFLICT ({conflict}) DO NOTHING", vals)
    else:
        ex(conn, f"INSERT OR IGNORE INTO {table} ({cl}) VALUES ({ph})", vals)

def upsert(conn, table, cols, vals, conflict, update_cols):
    ph = ",".join([PH] * len(vals))
    cl = ",".join(cols)
    if USE_PG:
        sets = ",".join([f"{c}=EXCLUDED.{c}" for c in update_cols])
        ex(conn, f"INSERT INTO {table} ({cl}) VALUES ({ph}) "
                 f"ON CONFLICT ({conflict}) DO UPDATE SET {sets}", vals)
    else:
        ex(conn, f"INSERT OR REPLACE INTO {table} ({cl}) VALUES ({ph})", vals)


# ---------------------------------------------------------------------------
# MULTI-TENANCY + DEMO TRIAL  (configured via COMPANIES_JSON env)
# ---------------------------------------------------------------------------
def _load_companies():
    raw = os.environ.get("COMPANIES_JSON")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            print("WARNING: COMPANIES_JSON is not valid JSON; using demo keys.")
    return {
        "device_keys": {"DEMO-DEVICE-KEY": "demo"},
        "admin_keys":  {"DEMO-ADMIN-KEY": "demo"},
        "licensed": [],
        "admin_logins": {"demo": {"username": "admin", "password": "admin"}},
    }

_COMPANIES   = _load_companies()
DEVICE_KEYS  = _COMPANIES.get("device_keys", {})
ADMIN_KEYS   = _COMPANIES.get("admin_keys", {})
LICENSED     = set(_COMPANIES.get("licensed", []))
ADMIN_LOGINS = _COMPANIES.get("admin_logins", {})
TRIAL_MINUTES = float(os.environ.get("TRIAL_MINUTES", "1440"))   # default 1 day


def init_db():
    conn = get_db()
    ex(conn, '''CREATE TABLE IF NOT EXISTS devices
                (hw_id TEXT, org TEXT, name TEXT, status TEXT, token TEXT,
                 command TEXT DEFAULT '',
                 brand TEXT DEFAULT '', ip TEXT DEFAULT '',
                 last_login TEXT DEFAULT '', uptime TEXT DEFAULT '',
                 rep_user TEXT DEFAULT '', grp TEXT DEFAULT '',
                 user_override TEXT DEFAULT '', last_seen TEXT DEFAULT '',
                 PRIMARY KEY (hw_id, org))''')
    ex(conn, '''CREATE TABLE IF NOT EXISTS binding_credential
                (org TEXT PRIMARY KEY, username TEXT, password TEXT, version TEXT)''')
    ex(conn, '''CREATE TABLE IF NOT EXISTS org_trials
                (org TEXT PRIMARY KEY, trial_start TEXT)''')
    # Migrations for databases created before these columns existed.
    _new_cols = ["command TEXT DEFAULT ''", "brand TEXT DEFAULT ''", "ip TEXT DEFAULT ''",
                 "last_login TEXT DEFAULT ''", "uptime TEXT DEFAULT ''",
                 "rep_user TEXT DEFAULT ''", "grp TEXT DEFAULT ''",
                 "user_override TEXT DEFAULT ''", "last_seen TEXT DEFAULT ''"]
    if USE_PG:
        for col in _new_cols:
            ex(conn, f"ALTER TABLE devices ADD COLUMN IF NOT EXISTS {col}")
        ex(conn, "ALTER TABLE binding_credential ADD COLUMN IF NOT EXISTS version TEXT")
    else:
        for col in _new_cols:
            try:
                ex(conn, f"ALTER TABLE devices ADD COLUMN {col}")
            except Exception:
                pass
        try:
            ex(conn, "ALTER TABLE binding_credential ADD COLUMN version TEXT")
        except Exception:
            pass
    put_db(conn)
init_db()

# How long after the last check-in a device is still considered "online".
ONLINE_WINDOW = 45         # seconds (grace so a brief network blip won't flicker)
HEARTBEAT_THROTTLE = 8     # only rewrite last_seen at most this often (per device)

def _iso_now():
    return datetime.now(timezone.utc).isoformat()

def _age_seconds(iso_str):
    """Seconds since the given ISO timestamp, or a large number if unparseable."""
    if not iso_str:
        return 10 ** 9
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso_str)).total_seconds()
    except Exception:
        return 10 ** 9

def rget(row, key, default=""):
    """Read a column from a row on either backend (sqlite3.Row has no .get)."""
    try:
        v = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if v is None else v


def require_device(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        key = request.headers.get("X-Company-Key", "")
        org = DEVICE_KEYS.get(key) or ADMIN_KEYS.get(key)
        if not org:
            return jsonify({"error": "unauthorized"}), 401
        return f(org, *args, **kwargs)
    return wrapper


def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        key = request.headers.get("X-Company-Key", "")
        org = ADMIN_KEYS.get(key)
        if not org:
            return jsonify({"error": "unauthorized"}), 401
        return f(org, *args, **kwargs)
    return wrapper


def _current_version(conn, org):
    row = q1(conn, f'SELECT version FROM binding_credential WHERE org = {PH}', (org,))
    return row["version"] if row else None


def _trial_info(conn, org, device_id=None):
    """Demo-trial status for a COMPANY. One clock per company: starts on first
    contact and is shared by the dashboard and all the company's devices."""
    if org in LICENSED:
        return {"expired": False, "licensed": True, "seconds_left": None, "expires_at": None}
    now = datetime.now(timezone.utc)
    row = q1(conn, f'SELECT trial_start FROM org_trials WHERE org = {PH}', (org,))
    if row and row["trial_start"]:
        start = datetime.fromisoformat(row["trial_start"])
    else:
        start = now
        insert_ignore(conn, "org_trials", ["org", "trial_start"],
                      [org, start.isoformat()], "org")
    expires = start + timedelta(minutes=TRIAL_MINUTES)
    left = (expires - now).total_seconds()
    return {"expired": left <= 0, "licensed": False,
            "seconds_left": int(max(0, left)), "expires_at": expires.isoformat()}


@app.route('/', methods=['GET'])
def health():
    return jsonify({"status": "ok", "service": "remote-lock-server",
                    "storage": "postgres" if USE_PG else "sqlite"})


@app.route('/admin/login', methods=['GET', 'POST'])
@require_admin
def admin_login(org):
    cred = ADMIN_LOGINS.get(org)
    if request.method == 'GET':
        return jsonify({"required": bool(cred)})
    if not cred:
        return jsonify({"success": True})
    data = request.get_json(silent=True) or {}
    u = (data.get("username") or "").strip()
    p = data.get("password") or ""
    if cred.get("username") == u and cred.get("password") == p:
        return jsonify({"success": True})
    return jsonify({"error": "invalid credentials"}), 401


@app.route('/trial', methods=['GET'])
@require_device
def trial(org):
    device_id = request.args.get("device_id") or request.headers.get("X-Device-Id") or ""
    conn = get_db()
    info = _trial_info(conn, org, device_id)
    put_db(conn)
    return jsonify(info)


# ---------------- BINDING CREDENTIAL ----------------
@app.route('/login', methods=['POST'])
@require_device
def login(org):
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400
    conn = get_db()
    row = q1(conn, f'SELECT username, password, version FROM binding_credential WHERE org = {PH}',
             (org,))
    put_db(conn)
    if row and row["username"] == username and row["password"] == password:
        return jsonify({"success": True, "version": row["version"]})
    return jsonify({"error": "invalid credentials"}), 401


@app.route('/version', methods=['GET'])
@require_device
def get_version(org):
    conn = get_db()
    version = _current_version(conn, org)
    put_db(conn)
    return jsonify({"version": version})


@app.route('/admin/credentials', methods=['GET'])
@require_admin
def get_credential(org):
    conn = get_db()
    row = q1(conn, f'SELECT username, password FROM binding_credential WHERE org = {PH}', (org,))
    put_db(conn)
    if row:
        return jsonify({"set": True, "username": row["username"], "password": row["password"]})
    return jsonify({"set": False})


@app.route('/admin/credentials', methods=['POST'])
@require_admin
def set_credential(org):
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400
    version = secrets.token_hex(8)
    conn = get_db()
    upsert(conn, "binding_credential",
           ["org", "username", "password", "version"],
           [org, username, password, version],
           "org", ["username", "password", "version"])
    ex(conn, f'DELETE FROM devices WHERE org = {PH}', (org,))
    put_db(conn)
    return jsonify({"success": True})


# ---------------- DEVICES ----------------
@app.route('/register', methods=['POST'])
@require_device
def register(org):
    data = request.get_json(silent=True) or {}
    hw_id, name = data.get('hw_id'), data.get('name')
    if not hw_id or not name:
        return jsonify({"error": "hw_id and name are required"}), 400
    brand = (data.get('brand') or '').strip()
    ip = (data.get('ip') or '').strip()
    last_login = (data.get('last_login') or '').strip()
    uptime = (data.get('uptime') or '').strip()
    rep_user = (data.get('user') or '').strip()
    conn = get_db()
    insert_ignore(conn, "devices",
                  ["hw_id", "org", "name", "status", "token", "command"],
                  [hw_id, org, name, 'unlocked', '', ''],
                  "hw_id, org")
    # always refresh the reported fields + heartbeat (does not touch lock state)
    ex(conn, f'UPDATE devices SET name = {PH}, brand = {PH}, ip = {PH}, '
             f'last_login = {PH}, uptime = {PH}, rep_user = {PH}, last_seen = {PH} '
             f'WHERE hw_id = {PH} AND org = {PH}',
       (name, brand, ip, last_login, uptime, rep_user, _iso_now(), hw_id, org))
    put_db(conn)
    return jsonify({"success": True})


@app.route('/status/<hw_id>', methods=['GET'])
@require_device
def get_status(org, hw_id):
    conn = get_db()
    row = q1(conn, f'SELECT status, token, command, last_seen FROM devices '
                   f'WHERE hw_id = {PH} AND org = {PH}', (hw_id, org))
    command = ""
    if row and row["command"]:
        command = row["command"]
        ex(conn, f'UPDATE devices SET command = {PH} WHERE hw_id = {PH} AND org = {PH}',
           ('', hw_id, org))
    # Heartbeat (throttled) + optional metadata refresh sent as query params.
    if row and _age_seconds(rget(row, "last_seen")) >= HEARTBEAT_THROTTLE:
        sets = ["last_seen = " + PH]
        vals = [_iso_now()]
        for col, arg in (("brand", "brand"), ("ip", "ip"), ("last_login", "last_login"),
                         ("uptime", "uptime"), ("rep_user", "user")):
            v = request.args.get(arg)
            if v is not None and v != "":
                sets.append(f"{col} = {PH}")
                vals.append(v)
        vals += [hw_id, org]
        ex(conn, f'UPDATE devices SET {", ".join(sets)} WHERE hw_id = {PH} AND org = {PH}', vals)
    cred_version = _current_version(conn, org)
    info = _trial_info(conn, org, hw_id)
    put_db(conn)
    base = {"cred_version": cred_version, "expired": info["expired"],
            "seconds_left": info["seconds_left"], "licensed": info["licensed"],
            "command": command}
    if row:
        base.update({"status": row["status"], "token": row["token"], "found": True})
    else:
        base.update({"status": "unlocked", "token": "", "found": False})
    return jsonify(base)


@app.route('/status/all', methods=['GET'])
@require_admin
def get_all_status(org):
    conn = get_db()
    rows = qall(conn, f'SELECT hw_id, name, status, token, brand, ip, last_login, '
                      f'uptime, rep_user, grp, user_override, last_seen '
                      f'FROM devices WHERE org = {PH}', (org,))
    put_db(conn)
    out = {}
    for r in rows:
        out[r["hw_id"]] = {
            "name": r["name"], "status": r["status"], "token": r["token"],
            "brand": rget(r, "brand"),
            "ip": rget(r, "ip"),
            "last_login": rget(r, "last_login"),
            "uptime": rget(r, "uptime"),
            "user": (rget(r, "user_override") or rget(r, "rep_user")),
            "group": rget(r, "grp"),
            "online": _age_seconds(rget(r, "last_seen")) < ONLINE_WINDOW,
        }
    return jsonify(out)


@app.route('/admin/set-meta', methods=['POST'])
@require_admin
def set_meta(org):
    data = request.get_json(silent=True) or {}
    hw_id = data.get("hw_id")
    if not hw_id:
        return jsonify({"error": "hw_id required"}), 400
    group = (data.get("group") or "").strip()
    user = (data.get("user") or "").strip()
    conn = get_db()
    affected = ex(conn, f'UPDATE devices SET grp = {PH}, user_override = {PH} '
                        f'WHERE hw_id = {PH} AND org = {PH}',
                  (group, user, hw_id, org))
    put_db(conn)
    if affected == 0:
        return jsonify({"error": "device not found"}), 404
    return jsonify({"success": True})


@app.route('/lock/<hw_id>', methods=['POST'])
@require_admin
def lock_device(org, hw_id):
    data = request.get_json(silent=True) or {}
    token = data.get("token", "")
    conn = get_db()
    affected = ex(conn, f'UPDATE devices SET status = {PH}, token = {PH} '
                        f'WHERE hw_id = {PH} AND org = {PH}',
                  ('locked', token, hw_id, org))
    put_db(conn)
    if affected == 0:
        return jsonify({"error": "device not found"}), 404
    return jsonify({"success": True})


@app.route('/unlock/<hw_id>', methods=['POST'])
@require_device
def unlock_device(org, hw_id):
    conn = get_db()
    affected = ex(conn, f'UPDATE devices SET status = {PH}, token = {PH} '
                        f'WHERE hw_id = {PH} AND org = {PH}',
                  ('unlocked', '', hw_id, org))
    put_db(conn)
    if affected == 0:
        return jsonify({"error": "device not found"}), 404
    return jsonify({"success": True})


def _queue_command(org, hw_id, command):
    conn = get_db()
    affected = ex(conn, f'UPDATE devices SET command = {PH} WHERE hw_id = {PH} AND org = {PH}',
                  (command, hw_id, org))
    put_db(conn)
    if affected == 0:
        return jsonify({"error": "device not found"}), 404
    return jsonify({"success": True})


@app.route('/restart/<hw_id>', methods=['POST'])
@require_admin
def restart_device(org, hw_id):
    return _queue_command(org, hw_id, 'restart')


@app.route('/shutdown/<hw_id>', methods=['POST'])
@require_admin
def shutdown_device(org, hw_id):
    return _queue_command(org, hw_id, 'shutdown')


if __name__ == '__main__':
    app.run()
