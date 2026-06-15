import customtkinter as ctk, requests

# UPDATE THIS URL
SERVER_URL = "https://your-app.onrender.com"

def send_lock(device_id):
    requests.post(f"{SERVER_URL}/lock/{device_id}")

app = ctk.CTk()
app.geometry("300x200")
app.title("Admin Dashboard")

btn = ctk.CTkButton(app, text="LOCK PC-001", command=lambda: send_lock("PC-001"))
btn.pack(pady=50)

app.mainloop()
