from flask import Flask, request, jsonify
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3, os, json

app = Flask(__name__)
DB = "database.db"

# ---------------------------------------------------------------------------
# MULTI-TENANCY
# Each company has its own secret keys. A DEVICE key lets a client register and
# read its own status; an ADMIN key (held only by the company's dashboard) can
# list all devices, lock/unlock them, and manage connection credentials. Both
# keys map to the same company "org".
#
# Configure on Render -> Environment with COMPANIES_JSON, e.g.:
# {
#   "device_keys": { "ACME-DEVICE-9f3a..": "acme" },
#   "admin_keys":  { "ACME-ADMIN-1b2c..":  "acme" }
# }
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
    }

_COMPANIES  = _load_companies()
DEVICE_KEYS = _COMPANIES.get("device_keys", {})
ADMIN_KEYS  = _COMPANIES.get("admin_keys", {})


def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS devices
                    (hw_id TEXT, org TEXT, name TEXT, status TEXT, token TEXT,
                     PRIMARY KEY (hw_id, org))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS credentials
                    (org TEXT, username TEXT, pw_hash TEXT,
                     PRIMARY KEY (org, username))''')
    conn.commit()
    conn.close()
init_db()


def require_device(f):
    """Client-level access: device key OR admin key, scoped to that org."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        key = request.headers.get("X-Company-Key", "")
        org = DEVICE_KEYS.get(key) or ADMIN_KEYS.get(key)
        if not org:
            return jsonify({"error": "unauthorized"}), 401
        return f(org, *args, **kwargs)
    return wrapper


def require_admin(f):
    """Dashboard-level access: admin key only."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        key = request.headers.get("X-Company-Key", "")
        org = ADMIN_KEYS.get(key)
        if not org:
            return jsonify({"error": "unauthorized"}), 401
        return f(org, *args, **kwargs)
    return wrapper


@app.route('/', methods=['GET'])
def health():
    return jsonify({"status": "ok", "service": "remote-lock-server"})


# ---------------- CLIENT ENROLLMENT (username / password) ----------------
@app.route('/login', methods=['POST'])
@require_device
def login(org):
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400
    conn = get_db()
    row = conn.execute('SELECT pw_hash FROM credentials WHERE org = ? AND username = ?',
                       (org, username)).fetchone()
    conn.close()
    if row and check_password_hash(row["pw_hash"], password):
        return jsonify({"success": True})
    return jsonify({"error": "invalid credentials"}), 401


# ---------------- ADMIN: manage connection credentials ----------------
@app.route('/admin/credentials', methods=['GET'])
@require_admin
def list_credentials(org):
    conn = get_db()
    rows = conn.execute('SELECT username FROM credentials WHERE org = ? ORDER BY username',
                        (org,)).fetchall()
    conn.close()
    return jsonify({"usernames": [r["username"] for r in rows]})


@app.route('/admin/credentials', methods=['POST'])
@require_admin
def set_credential(org):
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400
    conn = get_db()
    conn.execute('INSERT OR REPLACE INTO credentials (org, username, pw_hash) VALUES (?, ?, ?)',
                 (org, username, generate_password_hash(password)))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route('/admin/credentials/<username>', methods=['DELETE'])
@require_admin
def delete_credential(org, username):
    conn = get_db()
    cur = conn.execute('DELETE FROM credentials WHERE org = ? AND username = ?',
                       (org, username))
    conn.commit()
    affected = cur.rowcount
    conn.close()
    if affected == 0:
        return jsonify({"error": "not found"}), 404
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
    conn.close()
    if row:
        return jsonify({"status": row["status"], "token": row["token"], "found": True})
    return jsonify({"status": "unlocked", "token": "", "found": False})


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
