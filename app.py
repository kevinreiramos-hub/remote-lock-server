from flask import Flask, request, jsonify
from functools import wraps
from datetime import datetime, timedelta, timezone
import sqlite3, os, json, secrets

app = Flask(__name__)
DB = "database.db"

# ---------------------------------------------------------------------------
# MULTI-TENANCY + DEMO TRIAL
# COMPANIES_JSON (Render env) example:
# {
#   "device_keys": { "ACME-DEVICE-9f3a..": "acme" },
#   "admin_keys":  { "ACME-ADMIN-1b2c..":  "acme" },
#   "licensed":    [ "bigcorp" ]        # orgs here never expire (paying customers)
# }
# TRIAL_DAYS env controls the demo length (default 1). Use a fraction to test,
# e.g. TRIAL_DAYS=0.001 expires in ~90 seconds.
# --------------------------------------------------------------------------
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

_COMPANIES  = _load_companies()
DEVICE_KEYS = _COMPANIES.get("device_keys", {})
ADMIN_KEYS  = _COMPANIES.get("admin_keys", {})
LICENSED    = set(_COMPANIES.get("licensed", []))
# Optional dashboard logins: { "<org>": {"username": "...", "password": "..."} }
ADMIN_LOGINS = _COMPANIES.get("admin_logins", {})
# TRIAL_MINUTES env controls the demo length. Default 1440 = 1 day.
# For quick testing you can set TRIAL_MINUTES=5 (on Render or your shell).
TRIAL_MINUTES = float(os.environ.get("TRIAL_MINUTES", "1440"))


def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS devices
                    (hw_id TEXT, org TEXT, name TEXT, status TEXT, token TEXT,
                     PRIMARY KEY (hw_id, org))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS binding_credential
                    (org TEXT PRIMARY KEY, username TEXT, password TEXT, version TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS device_trials
                    (org TEXT, device_id TEXT, trial_start TEXT,
                     PRIMARY KEY (org, device_id))''')
    try:
        conn.execute("ALTER TABLE binding_credential ADD COLUMN version TEXT")
    except Exception:
        pass
    conn.commit()
    conn.close()
init_db()


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
    row = conn.execute('SELECT version FROM binding_credential WHERE org = ?',
                       (org,)).fetchone()
    return row["version"] if row else None


def _trial_info(conn, org, device_id):
    """Return demo-trial status for a single device. Starts its clock on first
    contact. Each device (by hardware id) gets its own independent trial."""
    if org in LICENSED:
        return {"expired": False, "licensed": True, "seconds_left": None, "expires_at": None}
    if not device_id:
        return {"expired": False, "licensed": False, "seconds_left": None, "expires_at": None}
    now = datetime.now(timezone.utc)
    row = conn.execute('SELECT trial_start FROM device_trials WHERE org = ? AND device_id = ?',
                       (org, device_id)).fetchone()
    if row and row["trial_start"]:
        start = datetime.fromisoformat(row["trial_start"])
    else:
        start = now
        conn.execute('INSERT OR REPLACE INTO device_trials (org, device_id, trial_start) '
                     'VALUES (?, ?, ?)', (org, device_id, start.isoformat()))
        conn.commit()
    expires = start + timedelta(minutes=TRIAL_MINUTES)
    left = (expires - now).total_seconds()
    return {"expired": left <= 0, "licensed": False,
            "seconds_left": int(max(0, left)), "expires_at": expires.isoformat()}


@app.route('/', methods=['GET'])
def health():
    return jsonify({"status": "ok", "service": "remote-lock-server"})


@app.route('/admin/login', methods=['GET', 'POST'])
@require_admin
def admin_login(org):
    """Dashboard sign-in. GET reports whether a login is configured; POST validates."""
    cred = ADMIN_LOGINS.get(org)
    if request.method == 'GET':
        return jsonify({"required": bool(cred)})
    if not cred:
        return jsonify({"success": True})        # no login configured -> allow
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
    conn.close()
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
    row = conn.execute('SELECT username, password, version FROM binding_credential WHERE org = ?',
                       (org,)).fetchone()
    conn.close()
    if row and row["username"] == username and row["password"] == password:
        return jsonify({"success": True, "version": row["version"]})
    return jsonify({"error": "invalid credentials"}), 401


@app.route('/version', methods=['GET'])
@require_device
def get_version(org):
    conn = get_db()
    version = _current_version(conn, org)
    conn.close()
    return jsonify({"version": version})


@app.route('/admin/credentials', methods=['GET'])
@require_admin
def get_credential(org):
    conn = get_db()
    row = conn.execute('SELECT username, password FROM binding_credential WHERE org = ?',
                       (org,)).fetchone()
    conn.close()
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
    conn.execute('INSERT OR REPLACE INTO binding_credential (org, username, password, version) '
                 'VALUES (?, ?, ?, ?)', (org, username, password, version))
    conn.execute('DELETE FROM devices WHERE org = ?', (org,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


# ---------------- DEVICES ----------------
@app.route('/register', methods=['POST'])
@require_device
def register(org):
    data = request.get_json(silent=True) or {}
    hw_id, name = data.get('hw_id'), data.get('name')
    if not hw_id or not name:
        return jsonify({"error": "hw_id and name are required"}), 400
    conn = get_db()
    conn.execute('INSERT OR IGNORE INTO devices VALUES (?, ?, ?, ?, ?)',
                 (hw_id, org, name, 'unlocked', ''))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route('/status/<hw_id>', methods=['GET'])
@require_device
def get_status(org, hw_id):
    conn = get_db()
    row = conn.execute('SELECT status, token FROM devices WHERE hw_id = ? AND org = ?',
                       (hw_id, org)).fetchone()
    cred_version = _current_version(conn, org)
    info = _trial_info(conn, org, hw_id)
    conn.close()
    base = {"cred_version": cred_version, "expired": info["expired"],
            "seconds_left": info["seconds_left"], "licensed": info["licensed"]}
    if row:
        base.update({"status": row["status"], "token": row["token"], "found": True})
    else:
        base.update({"status": "unlocked", "token": "", "found": False})
    return jsonify(base)


@app.route('/status/all', methods=['GET'])
@require_admin
def get_all_status(org):
    conn = get_db()
    rows = conn.execute('SELECT hw_id, name, status, token FROM devices WHERE org = ?',
                        (org,)).fetchall()
    conn.close()
    return jsonify({r["hw_id"]: {"name": r["name"], "status": r["status"],
                                 "token": r["token"]} for r in rows})


@app.route('/lock/<hw_id>', methods=['POST'])
@require_admin
def lock_device(org, hw_id):
    data = request.get_json(silent=True) or {}
    token = data.get("token", "")
    conn = get_db()
    cur = conn.execute('UPDATE devices SET status = ?, token = ? WHERE hw_id = ? AND org = ?',
                       ('locked', token, hw_id, org))
    conn.commit()
    affected = cur.rowcount
    conn.close()
    if affected == 0:
        return jsonify({"error": "device not found"}), 404
    return jsonify({"success": True})


@app.route('/unlock/<hw_id>', methods=['POST'])
@require_device
def unlock_device(org, hw_id):
    conn = get_db()
    cur = conn.execute('UPDATE devices SET status = ?, token = ? WHERE hw_id = ? AND org = ?',
                       ('unlocked', '', hw_id, org))
    conn.commit()
    affected = cur.rowcount
    conn.close()
    if affected == 0:
        return jsonify({"error": "device not found"}), 404
    return jsonify({"success": True})


if __name__ == '__main__':
    app.run()
