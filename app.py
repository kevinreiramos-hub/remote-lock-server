from flask import Flask, request, jsonify
import sqlite3

app = Flask(__name__)

def init_db():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS devices 
                 (hw_id TEXT PRIMARY KEY, name TEXT, status TEXT, token TEXT)''')
    conn.commit()
    conn.close()

init_db()

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    hw_id, name = data['hw_id'], data['name']
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO devices VALUES (?, ?, ?, ?)', (hw_id, name, 'unlocked', ''))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/status/<hw_id>', methods=['GET'])
def get_status(hw_id):
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('SELECT status, token FROM devices WHERE hw_id = ?', (hw_id,))
    row = c.fetchone()
    conn.close()
    return jsonify({"status": row[0], "token": row[1]}) if row else jsonify({"status": "unlocked", "token": ""})

@app.route('/status/all', methods=['GET'])
def get_all_status():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('SELECT hw_id, name, status, token FROM devices')
    rows = c.fetchall()
    conn.close()
    return jsonify({row[0]: {"name": row[1], "status": row[2], "token": row[3]} for row in rows})

@app.route('/lock/<hw_id>', methods=['POST'])
def lock_device(hw_id):
    token = request.json.get("token")
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('UPDATE devices SET status = "locked", token = ? WHERE hw_id = ?', (token, hw_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/unlock/<hw_id>', methods=['POST'])
def unlock_device(hw_id):
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('UPDATE devices SET status = "unlocked", token = "" WHERE hw_id = ?', (hw_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

if __name__ == '__main__':
    app.run()
