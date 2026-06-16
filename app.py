from flask import Flask, request, jsonify
from functools import wraps
import sqlite3, os

app = Flask(__name__)
DB = "database.db"

# Set this in Render → Environment.  While it is unset, auth is OFF so your
# existing client/dashboard keep working.  Once you set it, every request must
# send the matching  X-API-Key  header.
API_KEY = os.environ.get("API_KEY")
if not API_KEY:
    print("WARNING: API_KEY not set — endpoints are UNAUTHENTICATED.")


def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS devices
                    (hw_id TEXT PRIMARY KEY, name TEXT, status TEXT, token TEXT)''')
    conn.commit()
    conn.close()
init_db()


def require_api_key(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if API_KEY and request.headers.get("X-API-Key") != API_KEY:
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


@app.route('/', methods=['GET'])
def health():
    return jsonify({"status": "ok", "service": "remote-lock-server"})


@app.route('/register', methods=['POST'])
@require_api_key
def register():
    data = request.get_json(silent=True) or {}
    hw_id, name = data.get('hw_id'), data.get('name')
    if not hw_id or not name:
        return jsonify({"error": "hw_id and name are required"}), 400
    conn = get_db()
    conn.execute('INSERT OR IGNORE INTO devices VALUES (?, ?, ?, ?)',
                 (hw_id, name, 'unlocked', ''))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route('/status/<hw_id>', methods=['GET'])
@require_api_key
def get_status(hw_id):
    conn = get_db()
    row = conn.execute('SELECT status, token FROM devices WHERE hw_id = ?',
                       (hw_id,)).fetchone()
    conn.close()
    if row:
        return jsonify({"status": row["status"], "token": row["token"]})
    return jsonify({"status": "unlocked", "token": ""})


@app.route('/status/all', methods=['GET'])
@require_api_key
def get_all_status():
    conn = get_db()
    rows = conn.execute('SELECT hw_id, name, status, token FROM devices').fetchall()
    conn.close()
    return jsonify({r["hw_id"]: {"name": r["name"], "status": r["status"],
                                 "token": r["token"]} for r in rows})


@app.route('/lock/<hw_id>', methods=['POST'])
@require_api_key
def lock_device(hw_id):
    data = request.get_json(silent=True) or {}
    token = data.get("token", "")
    conn = get_db()
    cur = conn.execute('UPDATE devices SET status = ?, token = ? WHERE hw_id = ?',
                       ('locked', token, hw_id))
    conn.commit()
    affected = cur.rowcount
    conn.close()
    if affected == 0:
        return jsonify({"error": "device not found"}), 404
    return jsonify({"success": True})


@app.route('/unlock/<hw_id>', methods=['POST'])
@require_api_key
def unlock_device(hw_id):
    conn = get_db()
    cur = conn.execute('UPDATE devices SET status = ?, token = ? WHERE hw_id = ?',
                       ('unlocked', '', hw_id))
    conn.commit()
    affected = cur.rowcount
    conn.close()
    if affected == 0:
        return jsonify({"error": "device not found"}), 404
    return jsonify({"success": True})


if __name__ == '__main__':
    app.run()
