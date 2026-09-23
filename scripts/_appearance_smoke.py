import requests, json

BASE = "http://127.0.0.1:8178"

# 1) 管理员登录（种子 admin/admin123）
r = requests.post(f"{BASE}/api/auth/login", json={"username": "admin", "password": "admin123"}, timeout=5)
print("login status", r.status_code)
tok = r.json().get("token") or r.headers.get("Authorization", "").replace("Bearer ", "")
print("token?", bool(tok))
h = {"Authorization": f"Bearer {tok}"}

# 2) 管理员身份确认
r = requests.get(f"{BASE}/api/admin/overview", headers=h, timeout=5)
print("admin/overview status", r.status_code, "ok=", r.json().get("ok"))

# 3) PUT 修改形象
r = requests.put(f"{BASE}/api/appearance", headers=h,
                 json={"model": "Mark", "background": "back_class_normal.png"}, timeout=5)
print("PUT status", r.status_code, r.text[:200])

# 4) GET 确认已持久化
r = requests.get(f"{BASE}/api/appearance", timeout=5)
d = r.json()
print("after PUT -> model=", d["model"], "background=", d["background"])

# 5) digital-human/status 是否带 model/background
r = requests.get(f"{BASE}/api/digital-human/status", headers=h, timeout=5)
s = r.json()
print("status model=", s.get("model"), "background=", s.get("background"), "frontend_url=", s.get("frontend_url"))

# 6) 非法值应被拒
r = requests.put(f"{BASE}/api/appearance", headers=h, json={"model": "Nope", "background": "x.png"}, timeout=5)
print("invalid PUT status", r.status_code, r.json().get("detail"))

# 7) 还原默认（保持演示干净）
r = requests.put(f"{BASE}/api/appearance", headers=h, json={"model": "Haru", "background": "nokia_reception_desk.png"}, timeout=5)
print("restore status", r.status_code, r.json().get("model"), r.json().get("background"))
