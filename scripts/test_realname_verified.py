"""scripts/test_realname_verified.py —— 实名认证完成后禁止在线修改（验收）"""
from __future__ import annotations
import os, sys, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pymysql, config, profile_db, requests

R = "http://127.0.0.1:8178"
RES = []
def ok(name, cond, detail=""):
    RES.append((name, cond, detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

# 1) user1（已认证，迁移时回填 realname_verified=1）
t = profile_db.issue_token(1); h = {"Authorization": f"Bearer {t}"}
prof = requests.get(R+"/api/profile", headers=h, timeout=8).json()
ok("user1 profile.realname_verified = true", prof.get("realname_verified") is True,
   f"real_name={prof.get('real_name')}")

r = requests.post(R+"/api/realname/confirm",
                  json={"real_name":"试图改名字","code":"000000"}, headers=h, timeout=8)
ok("已认证用户 confirm 被拦截（HTTP 200 + ok:false + 柜台提示）",
   r.status_code == 200 and not r.json().get("ok") and "柜台" in (r.json().get("error") or ""),
   f"status={r.status_code} {r.json().get('error','')[:60]}")

# 2) 新用户走正常流程：发码 → confirm → 二次 confirm 应被拦截
uname = f"rn_t_{random.randint(10**6,10**7-1)}"
r = requests.post(R+"/api/auth/register", json={"username":uname,"password":"123456"}, timeout=15)
t2 = r.json()["token"]; h2 = {"Authorization": f"Bearer {t2}"}
prof2 = requests.get(R+"/api/profile", headers=h2, timeout=8).json()
ok("新用户 realname_verified = false（初始）",
   prof2.get("realname_verified") is False, f"real_name={prof2.get('real_name')!r}")

sr = requests.post(R+"/api/realname/send_code", headers=h2, timeout=8)
ok("新用户 send_code 成功", sr.status_code == 200 and sr.json().get("ok"))

c = pymysql.connect(host=config.DB_HOST, port=config.DB_PORT, user=config.DB_USER,
                    password=config.DB_PASSWORD, database=config.DB_NAME, charset="utf8mb4",
                    cursorclass=pymysql.cursors.DictCursor)
with c.cursor() as cur:
    cur.execute("SELECT id FROM users WHERE username=%s", (uname,))
    uid = cur.fetchone()["id"]
    cur.execute("""SELECT code_plain FROM verification_codes
                   WHERE user_id=%s AND scene='realname' AND status='unused'
                   ORDER BY id DESC LIMIT 1""", (uid,))
    code = cur.fetchone()["code_plain"]
c.close()
cr = requests.post(R+"/api/realname/confirm",
                   json={"real_name":"测试姓名","code":code}, headers=h2, timeout=8)
ok("新用户首次 confirm 成功", cr.status_code == 200 and cr.json().get("ok"),
   f"real_name={cr.json().get('real_name')}")
prof2b = requests.get(R+"/api/profile", headers=h2, timeout=8).json()
ok("新用户核验后 realname_verified = true", prof2b.get("realname_verified") is True)

# 二次 confirm（重新发码 → 拿到 code → 试图改名）应被拦截
requests.post(R+"/api/realname/send_code", headers=h2, timeout=8)
c = pymysql.connect(host=config.DB_HOST, port=config.DB_PORT, user=config.DB_USER,
                    password=config.DB_PASSWORD, database=config.DB_NAME, charset="utf8mb4",
                    cursorclass=pymysql.cursors.DictCursor)
with c.cursor() as cur:
    cur.execute("""SELECT code_plain FROM verification_codes
                   WHERE user_id=%s AND scene='realname' AND status='unused'
                   ORDER BY id DESC LIMIT 1""", (uid,))
    code2 = cur.fetchone()["code_plain"]
c.close()
cr2 = requests.post(R+"/api/realname/confirm",
                    json={"real_name":"想改成别的","code":code2}, headers=h2, timeout=8)
ok("已认证用户二次 confirm 被拦截（即使验证码正确）",
   cr2.status_code == 200 and not cr2.json().get("ok") and "柜台" in (cr2.json().get("error") or ""),
   f"status={cr2.status_code} {cr2.json().get('error','')[:60]}")

# 3) 汇总
print("\n" + "="*60)
p = sum(1 for _,c,_ in RES if c); t = len(RES)
print(f" 结果：{p}/{t} 通过")
print("="*60)
sys.exit(0 if p == t else 1)
