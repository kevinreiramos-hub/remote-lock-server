from flask import Flask, request, jsonify

app = Flask(__name__)

# This acts as your in-memory database
# Format: { "HW_ID": {"name": "User-PC", "status": "unlocked", "token": ""} }
database = {}

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    hw_id = data['hw_id']
    name = data['name']
    # If device isn't in DB, add it. If it is, update the name.
    database[hw_id] = {"name": name, "status": "unlocked", "token": ""}
    return jsonify({"success": True})

@app.route('/status/<hw_id>', methods=['GET'])
def get_status(hw_id):
    # Returns the current status of a specific device
    return jsonify(database.get(hw_id, {"status": "unlocked", "token": ""}))

@app.route('/status/all', methods=['GET'])
def get_all_status():
    # This route allows your dashboard to see all connected laptops
    return jsonify(database)

@app.route('/lock/<hw_id>', methods=['POST'])
def lock_device(hw_id):
    token = request.json.get("token")
    if hw_id in database:
        database[hw_id].update({"status": "locked", "token": token})
        return jsonify({"success": True})
    return jsonify({"success": False}), 404

@app.route('/unlock/<hw_id>', methods=['POST'])
def unlock_device(hw_id):
    if hw_id in database:
        database[hw_id].update({"status": "unlocked", "token": ""})
        return jsonify({"success": True})
    return jsonify({"success": False}), 404

if __name__ == '__main__':
    app.run()
