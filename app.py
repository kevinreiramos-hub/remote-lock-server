from flask import Flask, request, jsonify

app = Flask(__name__)

# Replace with a real DB (PostgreSQL) for production
database = {
    "PC-001": {"key": "KEY-123", "status": "unlocked"},
    "PC-002": {"key": "KEY-456", "status": "unlocked"}
}

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    if database.get(data['device_id'], {}).get('key') == data['license_key']:
        return jsonify({"success": True})
    return jsonify({"success": False}), 403

@app.route('/status/<device_id>', methods=['GET'])
def get_status(device_id):
    return jsonify({"action": database.get(device_id, {}).get("status", "unlocked")})

@app.route('/lock/<device_id>', methods=['POST'])
def lock_device(device_id):
    if device_id in database:
        database[device_id]["status"] = "locked"
        return jsonify({"success": True})
    return jsonify({"success": False}), 404

# ADD THIS NEW ROUTE
@app.route('/unlock/<device_id>', methods=['POST'])
def unlock_device(device_id):
    if device_id in database:
        database[device_id]["status"] = "unlocked"
        return jsonify({"success": True})
    return jsonify({"success": False}), 404

if __name__ == '__main__':
    app.run()
