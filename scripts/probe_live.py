import requests, json

BASE = "http://127.0.0.1:8178"

def show(label, r):
    try:
        body = r.json()
    except Exception:
        body = r.text[:200]
    print(f"\n### {label}  [HTTP {r.status_code}]")
    print(json.dumps(body, ensure_ascii=False)[:700])

# 1) anonymous state
show("GET /api/state (anon)", requests.get(f"{BASE}/api/state", timeout=8))
# 2) anonymous cards
show("GET /api/cards (anon)", requests.get(f"{BASE}/api/cards", timeout=8))
# 3) anonymous operation_logs
show("GET /api/operation_logs?limit=50 (anon)", requests.get(f"{BASE}/api/operation_logs?limit=50", timeout=8))

# 4) login user1
r = requests.post(f"{BASE}/api/auth/login", json={"username":"user1","password":"123456"}, timeout=8)
print("\n### login user1 ->", r.status_code, r.text[:200])
tok1 = r.json().get("token")

# 5) user1 cards + logs
h1 = {"Authorization": f"Bearer {tok1}"}
show("GET /api/cards (user1)", requests.get(f"{BASE}/api/cards", headers=h1, timeout=8))
show("GET /api/operation_logs (user1)", requests.get(f"{BASE}/api/operation_logs?limit=50", headers=h1, timeout=8))

# 6) login user2
r2 = requests.post(f"{BASE}/api/auth/login", json={"username":"user2","password":"123456"}, timeout=8)
print("\n### login user2 ->", r2.status_code, r2.text[:200])
tok2 = r2.json().get("token")
h2 = {"Authorization": f"Bearer {tok2}"}

# 7) user2 cards (expect 0) + logs
show("GET /api/cards (user2)", requests.get(f"{BASE}/api/cards", headers=h2, timeout=8))
show("GET /api/operation_logs (user2)", requests.get(f"{BASE}/api/operation_logs?limit=50", headers=h2, timeout=8))

# 8) cross-user: user2 token hits user1's card id=1
show("GET /api/cards/1 (user2 token -> expect 403)", requests.get(f"{BASE}/api/cards/1", headers=h2, timeout=8))
