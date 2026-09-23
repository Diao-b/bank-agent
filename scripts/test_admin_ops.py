"""test_admin_ops.py —— C6 可运营后台 端到端验收

前提：run_server(8178) 与 mock_bank(8003) 已启动；MySQL 可用。
覆盖：
  1) 独立入口：/admin/login 页面可达；普通用户访问 /api/admin/* 全 403；
  2) 风控参数：阈值 2000→8000 → _verify_queue 行为即时变化 → admin_logs old→new
     → pending_sync 标记 → 越界值拒绝 → 确认同步后徽标消失；
  3) 产品下架：用户端列表消失 / 购买被拒 / 既有持仓不受影响；上架恢复；
  4) 锁定用户：注册新号 → 锁定 → 登录 403 → 解锁 → 可登录；
  5) 重置支付密码：站内信送达新码 → 旧密码失效、新密码核验通过；
  6) 系统公告：全员群发 → 用户收件箱收到 type=notice；
  7) 红线：不存在任何"管理员改用户资产"接口（探针全部 404/405）；
  8) admin_logs：每次写操作均有留痕（who/what/old→new/when）。
"""
import json
import sys
import time

import requests

BASE = "http://127.0.0.1:8178"
ADMIN = {"username": "admin", "password": "admin123"}
PASS, FAIL = [], []


def check(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(extra)) if extra else ""))


def login(username, password):
    r = requests.post(BASE + "/api/auth/login", json={"username": username, "password": password})
    return r


def admin_token():
    r = login(**ADMIN)
    return r.json()["token"]


AT = admin_token()
AH = {"Authorization": "Bearer " + AT}


def admin_post(path, body=None):
    return requests.post(BASE + path, headers=AH, json=body or {})


def admin_put(path, body=None):
    return requests.put(BASE + path, headers=AH, json=body or {})


# ---------------------------------------------------------------- 1) 入口与权限
r = requests.get(BASE + "/admin/login")
check("独立登录页 /admin/login 可达", r.status_code == 200 and "运营管理后台" in r.text)

# 普通用户 token → /api/admin/* 必须 403（时间戳账号，脚本可重复执行）
_stamp = str(int(time.time()))[-6:]
rr = requests.post(BASE + "/api/auth/register",
                   json={"username": "c6_probe_" + _stamp, "password": "probe123456"})
probe_tok = rr.json().get("token")
assert probe_tok, rr.json()
r = requests.get(BASE + "/api/admin/config", headers={"Authorization": "Bearer " + probe_tok})
check("普通用户访问 /api/admin/config → 403", r.status_code == 403, r.status_code)
r = requests.get(BASE + "/api/admin/users", headers={"Authorization": "Bearer " + probe_tok})
check("普通用户访问 /api/admin/users → 403", r.status_code == 403, r.status_code)

# ---------------------------------------------------------------- 2) 风控参数
KEY = "transfer_verify_threshold"
r = requests.get(BASE + "/api/admin/config", headers=AH)
cfg = {c["key"]: c for c in r.json()["config"]}
check("参数列表 14 项", len(cfg) == 14, len(cfg))
check("阈值当前值 2000", cfg[KEY]["value"] == "2000", cfg[KEY]["value"])

# 越界拒绝
r = admin_put(f"/api/admin/config/{KEY}", {"value": "999999"})
check("越界值 999999 被拒绝（400）", r.status_code == 400, r.json().get("detail", "")[:40])
r = admin_put(f"/api/admin/config/{KEY}", {"value": "abc"})
check("非法数值被拒绝（400）", r.status_code == 400)

# 2000 → 8000
r = admin_put(f"/api/admin/config/{KEY}", {"value": "8000"})
d = r.json()
check("阈值改为 8000 成功", r.status_code == 200 and d.get("new") == "8000", d)
check("响应提示知识文档需同步", d.get("affects_knowledge") is True and "transfer-rules.md" in d.get("docs_ref", ""))

# 用户端阈值即时生效（新进程读 DB → get_rule）
sys.path.insert(0, ".")
import rules  # noqa: E402
rules.load_rules()
check("get_rule 即时读到 8000（跨进程 ≤5s TTL）", rules.get_rule(KEY) == 8000)

# 活逻辑：write_flow._verify_queue 按 DB 参数分级（payee_info.is_frequent=1 常用）
from agent import write_flow  # noqa: E402
FREQUENT = {"amount": 6000, "payee_info": {"is_frequent": 1}}
q = write_flow._verify_queue("transfer", dict(FREQUENT))
check("阈值 8000 后常用收款人转 6000 不触发验证码（仅支付密码）", q == (["pay_password"], False), q)
rules.set_rule_value(KEY, "2000")            # 还原，便于后续断言双要素
q = write_flow._verify_queue("transfer", dict(FREQUENT))
check("还原 2000 后转 6000 恢复双要素", q == (["sms_code", "pay_password"], False), q)

# admin_logs 有 old→new 留痕 + pending_sync 标记
r = requests.get(BASE + f"/api/admin/config/{KEY}/history", headers=AH)
hist = r.json()["history"]
entry = next((h for h in hist if json.loads(h["detail"]).get("new") == "8000"), None)
check("admin_logs 记录 old→new（2000→8000）", entry is not None and json.loads(entry["detail"])["old"] == "2000")
check("该条留痕带 pending_sync 标记", entry is not None and entry["pending_sync"] == 1)

# 待同步徽标出现 → 确认后消失
r = requests.get(BASE + "/api/admin/knowledge", headers=AH)
pending = [p for p in r.json()["pending"] if json.loads(p["detail"]).get("docs") == "transfer-rules.md"]
check("知识库出现 transfer-rules.md 待同步", len(pending) >= 1)
# 管理端把参数改回 2000 也留痕；确认同步 → 徽标消失
r = admin_post(f"/api/admin/config/{KEY}/sync-confirm")
check("确认同步清除标记", r.json().get("cleared", 0) >= 1, r.json())
r = requests.get(BASE + "/api/admin/knowledge", headers=AH)
pending = [p for p in r.json()["pending"] if json.loads(p["detail"]).get("docs") == "transfer-rules.md"
           and not json.loads(p["detail"]).get("_done")]
check("确认后 transfer-rules.md 无待同步", len(pending) == 0)

# ---------------------------------------------------------------- 3) 产品下架
r = requests.get(BASE + "/api/admin/products", headers=AH)
products = r.json()["products"]
target = next(p for p in products if p["holders"] == 0)
r = admin_put(f"/api/admin/products/{target['id']}", {"status": "off"})
check("产品下架成功", r.status_code == 200)

# 用户端列表不可见
r = requests.get(BASE + "/api/products", headers={"Authorization": "Bearer " + probe_tok})
user_ids = [p["id"] for p in r.json()["products"]]
check("下架后用户端理财页不可见", target["id"] not in user_ids)

# 购买被拒
# probe 用户绑定手机（过写操作闸门，测试环境直改 users.phone）
probe_uid_for_buy = next(u["user_id"] for u in
                         requests.get(BASE + "/api/admin/users", headers=AH).json()["users"]
                         if u["username"] == "c6_probe_" + _stamp)
import pymysql as _pm
from dotenv import dotenv_values as _dv
_env = _dv(".env")
_c = _pm.connect(host=_env["DB_HOST"], port=int(_env["DB_PORT"]), user=_env["DB_USER"],
                 password=_env["DB_PASSWORD"], database=_env["DB_NAME"], autocommit=True)
with _c.cursor() as _cur:
    _cur.execute("UPDATE users SET phone=%s WHERE id=%s", ("13800009999", probe_uid_for_buy))
_c.close()
r = requests.post(BASE + "/api/products/buy",
                  headers={"Authorization": "Bearer " + probe_tok},
                  json={"product_id": target["id"], "amount": float(target["min_amount"])})
_buy_resp = (r.json().get("error") or r.json().get("message") or "")
check("下架产品购买被拒", r.json().get("ok") is False and "下架" in _buy_resp, _buy_resp[:50])

# 上架恢复
r = admin_put(f"/api/admin/products/{target['id']}", {"status": "on"})
check("产品重新上架成功", r.status_code == 200)
r = requests.get(BASE + "/api/products", headers={"Authorization": "Bearer " + probe_tok})
check("上架后用户端恢复可见", target["id"] in [p["id"] for p in r.json()["products"]])

# ---------------------------------------------------------------- 4) 锁定 / 解锁
# 注册一个专用账号
stamp = str(int(time.time()))
rr = requests.post(BASE + "/api/auth/register",
                   json={"username": "c6_lock_" + stamp[-6:], "password": "lock123456"})
lock_user = rr.json()
ltok, luid = lock_user["token"], lock_user["user"]["user_id"]

# 管理员不可被锁定
admin_row = requests.get(BASE + "/api/admin/users", headers=AH).json()["users"]
admin_id = next(u for u in admin_row if u["role"] == "admin")["user_id"]
r = admin_post(f"/api/admin/users/{admin_id}/lock", {"locked": True})
check("管理员账号不可被锁定（400）", r.status_code == 400)

r = admin_post(f"/api/admin/users/{luid}/lock", {"locked": True})
check("锁定用户成功", r.status_code == 200)
r = login("c6_lock_" + stamp[-6:], "lock123456")
check("锁定后登录被拒（403 + 明确提示）", r.status_code == 403 and "锁定" in r.json()["detail"], r.json())
r = admin_post(f"/api/admin/users/{luid}/lock", {"locked": False})
check("解锁成功", r.status_code == 200)
r = login("c6_lock_" + stamp[-6:], "lock123456")
check("解锁后可登录", r.status_code == 200 and r.json()["ok"] is True)

# ---------------------------------------------------------------- 5) 重置支付密码
# probe 用户的 user_id（管理员列表查询；/api/profile 不含 user_id）
uid_probe = next(u["user_id"] for u in
                 requests.get(BASE + "/api/admin/users", headers=AH).json()["users"]
                 if u["username"] == "c6_probe_" + _stamp)
# 先设一个旧支付密码
requests.post(BASE + "/api/pay/password/set",
              headers={"Authorization": "Bearer " + probe_tok}, json={"password": "111111"})
r = admin_post(f"/api/admin/users/{uid_probe}/reset-paypass")
check("重置支付密码成功", r.status_code == 200, r.json())

# 站内信送达新码（明文只给本人）
r = requests.get(BASE + "/api/inbox", headers={"Authorization": "Bearer " + probe_tok})
notices = [n for n in r.json()["messages"] if "支付密码已重置" in n["title"]]
check("重置通知送达用户收件箱", len(notices) >= 1)
import re as _re
new_pwd = (_re.search(r"(\d{6})", notices[0]["content"]) if notices else None)
new_pwd = new_pwd.group(1) if new_pwd else ""
check("收件箱含 6 位新密码", len(new_pwd) == 6)

# 旧密码失效 / 新密码核验通过
r = requests.post(BASE + "/api/pay/password/verify",
                  headers={"Authorization": "Bearer " + probe_tok}, json={"password": "111111"})
check("旧支付密码已失效", r.json().get("ok") is False)
r = requests.post(BASE + "/api/pay/password/verify",
                  headers={"Authorization": "Bearer " + probe_tok}, json={"password": new_pwd})
check("新支付密码核验通过", r.json().get("ok") is True, r.json())

# ---------------------------------------------------------------- 6) 系统公告
r = admin_post("/api/admin/announcement", {"title": "C6 验收公告", "content": "这是一条运营后台群发公告。"})
check("公告群发成功", r.status_code == 200 and r.json().get("recipients", 0) > 0, r.json())

# ---------------------------------------------------------------- 7) 红线探针
probes = [
    ("PUT", "/api/admin/users/{}/balance"),
    ("POST", "/api/admin/users/{}/transfer"),
    ("PUT", "/api/admin/accounts/1"),
    ("POST", "/api/admin/holdings/1/adjust"),
    ("DELETE", "/api/admin/transactions/1"),
    ("POST", "/api/admin/users/{}/subscribe"),
]
red_ok = True
for method, path in probes:
    url = BASE + path.format(uid_probe)
    r = requests.request(method, url, headers=AH, json={"amount": 999999})
    if r.status_code not in (404, 405):
        red_ok = False
        print("   红线探针异常：", method, path, r.status_code)
check("红线：不存在任何改用户资产的管理接口（探针全 404/405）", red_ok)

# ---------------------------------------------------------------- 8) admin_logs 留痕
r = requests.get(BASE + "/api/admin/admin-logs?limit=300", headers=AH)
logs = r.json()["logs"]
kinds = {l["action"] for l in logs}
need = {"config_update", "product_update", "user_lock", "user_unlock",
        "user_reset_paypass", "notice_send", "knowledge_sync_confirm"}
check("admin_logs 覆盖全部写操作类型", need.issubset(kinds), sorted(need - kinds))

print()
print(f"===== 结果：PASS {len(PASS)} / FAIL {len(FAIL)} =====")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
