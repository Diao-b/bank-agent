import requests, json, time
BASE="http://127.0.0.1:8178"

def chat(thread, msg, token=None):
    h={}
    if token: h["Authorization"]=f"Bearer {token}"
    r=requests.post(f"{BASE}/api/chat",json={"thread_id":thread,"message":msg},headers=h,timeout=90)
    try: return r.status_code, r.json()
    except Exception: return r.status_code, {"raw":r.text[:300]}

# ---- item 4: transfer 2000 to 妈妈 (multi-turn) ----
print("==== item4 转2000给妈妈 ====")
sc, b = chat("audit-t4", "转2000给妈妈")
print(sc, json.dumps(b, ensure_ascii=False)[:500])

# ---- item 8: 茅台股价 (MCP) ----
print("\n==== item8 查茅台股价 ====")
sc, b = chat("audit-t8", "查茅台股价")
print(sc, json.dumps(b, ensure_ascii=False)[:600])

# ---- item 20: register new user -> provision ----
print("\n==== item20 register+provision ====")
import random
uname="aud_b%d"%random.randint(100000,999999)
r=requests.post(f"{BASE}/api/auth/register",json={"username":uname,"password":"123456"},timeout=15)
print("register", r.status_code, r.text[:400])
if r.ok:
    d=r.json(); tok=d.get("token"); uid=d.get("user",{}).get("user_id")
    print("new uid=",uid,"provision=",json.dumps(d.get("provision"),ensure_ascii=False)[:300])
    h={"Authorization":f"Bearer {tok}"}
    for ep in ["/api/accounts","/api/cards","/api/subscriptions","/api/transactions","/api/holdings"]:
        rr=requests.get(f"{BASE}{ep}",headers=h,timeout=8)
        try: j=rr.json()
        except Exception: j={"raw":rr.text[:80]}
        print(f"  {ep} ->", rr.status_code, "count=", j.get("count"), "ok=", j.get("ok"))
