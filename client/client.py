import requests, ctypes, time

# UPDATE THIS URL
SERVER_URL = "https://your-app.onrender.com"
DEVICE_ID = "PC-001"
LICENSE_KEY = "KEY-123"

def register():
    try:
        resp = requests.post(f"{SERVER_URL}/register", json={"device_id": DEVICE_ID, "license_key": LICENSE_KEY})
        return resp.json().get("success", False)
    except: return False

if __name__ == "__main__":
    if register():
        while True:
            try:
                if requests.get(f"{SERVER_URL}/status/{DEVICE_ID}").json().get("action") == "locked":
                    ctypes.windll.user32.LockWorkStation()
            except: pass
            time.sleep(10)
