"""
scripts/test_account_system.py —— 账户体系重构 · 验收自测（后端，直连 mock_bank:8003）

覆盖验收标准中的后端可测项：
  #1 漏洞封堵：转给收款人簿无此人 → not_found + 引导新增
  #2 随意填卡号被封：微众+虚构(Luhn合法但不存在)卡号 → 拒绝"该微众卡号不存在"
  #3 Luhn 非法卡号 → "卡号有误"
  #4 BIN 与所选银行不符：选招行填 6222 开头 → "卡号与所选银行不符"
  #5 微众+李哥真实卡号 → 站内收款人创建成功 + 自动绑定 + 实名"李*"
  #6 站内转账全链路回归：转账→李哥入账→余额守恒（直连 /api/transfer）
  #7 跨行转账：妈妈(招行) → A 扣款 + 通知"2小时内"，妈妈侧无变化
  #8 姓名不符：站内卡实名为李*，输入王五 → name_mismatch=True（确认卡机制在对话流验证）
  #9 银行目录 19 家；收款人列表含 card_no/real_name/bank_id
  #10 双妈妈候选流程（ambiguous）
"""
from __future__ import annotations

import json
import sys
import os
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from utils import card_validator

BASE = config.BANK_API_BASE.rstrip("/")
LIGE_CARD = "6102262016810343"   # 李哥主账户卡号（seed 生成）


def call(method, path, json_body=None):
    r = requests.request(method, BASE + path, json=json_body, timeout=10)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"raw": r.text}


def resolve(payload):
    return call("POST", "/api/payees/resolve", json_body=payload)


def banner(n, name):
    print(f"\n=== #{n} {name} ===")


results = []


def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not cond else ""))


# --------------------------------------------------------------------------- #
banner(9, "银行目录 & 收款人列表字段")
st, bd = call("GET", "/api/bank-directory")
check("bank-directory ok", bd.get("ok") and bd.get("count") == 19, f"count={bd.get('count')}")
web = next((b for b in bd.get("banks", []) if b.get("bank_code") == "WEBANK"), None)
check("微众银行 is_local=1", web and web.get("is_local") == 1)
st, pl = call("GET", "/api/payees")
payees = pl.get("payees", [])
sample = next((p for p in payees if p.get("nickname") == "李雷"), None)
check("收款人含 card_no", sample and sample.get("card_no") == LIGE_CARD, str(sample.get("card_no")))
# 列表返回全名（库里存全名），脱敏在展示层统一做（resolve 输出层已脱敏为"李*"）
check("收款人含 real_name(全名，前端脱敏展示)", sample and sample.get("real_name") == "李哥",
      str(sample.get("real_name")))
check("收款人含 bank_id", sample and isinstance(sample.get("bank_id"), int))

# --------------------------------------------------------------------------- #
banner(1, "漏洞封堵：收款人簿无此人")
st, r = resolve({"nickname": "张三丰"})
check("not_found", r.get("ok") is False and r.get("status") == "not_found", json.dumps(r, ensure_ascii=False))
check("引导新增", r.get("suggest") == "add_payee")

# --------------------------------------------------------------------------- #
banner(10, "双妈妈候选流程（ambiguous）")
st, r = resolve({"nickname": "妈妈"})
check("ambiguous", r.get("ok") is True and r.get("status") == "ambiguous", json.dumps(r, ensure_ascii=False)[:120])
check("两个候选", len(r.get("candidates", [])) == 2)

# --------------------------------------------------------------------------- #
banner(3, "Luhn 非法卡号 → 卡号有误")
fake_bad = "6225888811112222"   # 长度够但校验位错
st, r = call("POST", "/api/payees/create", json_body={
    "payee_name": "测试坏卡", "bank_id": 6, "card_no": fake_bad})
check("Luhn 拦截", r.get("ok") is False and "卡号有误" in (r.get("error") or ""), str(r.get("error")))

# --------------------------------------------------------------------------- #
banner(4, "BIN 与所选银行不符：选招行填 6222 开头")
st, r = call("POST", "/api/payees/create", json_body={
    "payee_name": "测试错行", "bank_id": 6,
    "card_no": card_validator.generate_card_no("62220000", 12345)})
check("卡号与所选银行不符", r.get("ok") is False and "卡号与所选银行不符" in (r.get("error") or ""), str(r.get("error")))

# --------------------------------------------------------------------------- #
banner(2, "随意填卡号被封：微众+虚构卡号(Luhn合法但不存在)")
st, r = call("POST", "/api/payees/create", json_body={
    "payee_name": "测试虚空", "bank_id": 19,
    "card_no": card_validator.generate_card_no("61022620", 999888777)})
check("该微众卡号不存在", r.get("ok") is False and "该微众卡号不存在" in (r.get("error") or ""), str(r.get("error")))

# --------------------------------------------------------------------------- #
banner(5, "微众+李哥真实卡号 → 站内收款人创建成功 + 绑定 + 实名李*")
# 先验证重复检测：李雷已占用李哥主卡 → 再建同卡号应被拦
st, r = call("POST", "/api/payees/create", json_body={
    "payee_name": "李哥小微", "bank_id": 19, "card_no": LIGE_CARD})
check("重复卡号拦截(李雷已占用李哥主卡)",
      r.get("ok") is False and "已存在" in (r.get("error") or ""), str(r.get("error")))

# 再换一张"未被本用户使用过的微众真实卡"来验证新建站内收款人
import pymysql
_conn = pymysql.connect(**config.db_config())
try:
    with _conn.cursor() as _cur:
        _cur.execute(
            "SELECT a.card_no, a.user_id, u.name FROM accounts a JOIN users u ON u.id=a.user_id "
            "WHERE a.card_no IS NOT NULL AND a.card_no<>'' "
            "  AND a.card_no NOT IN (SELECT card_no FROM payees WHERE user_id=1 AND card_no IS NOT NULL) "
            "  AND a.user_id <> 1 LIMIT 1")
        _t = _cur.fetchone()
finally:
    _conn.close()
target_card, target_uid, target_name = _t["card_no"], _t["user_id"], _t["name"]
expected_masked = card_validator.mask_real_name(target_name)

st, r = call("POST", "/api/payees/create", json_body={
    "payee_name": "测试站内", "bank_id": 19, "card_no": target_card})
check("创建成功 insite", r.get("ok") is True and r.get("track") == "insite", json.dumps(r, ensure_ascii=False)[:160])
check(f"自动绑定 linked_user_id={target_uid}", r.get("payee", {}).get("linked_user_id") == target_uid)
check(f"实名脱敏(系统实名覆盖) {expected_masked}",
      r.get("payee", {}).get("real_name") == expected_masked, str(r.get("payee", {}).get("real_name")))
new_pid = r.get("payee", {}).get("id")
# 清理：删除本用例创建的测试收款人，避免污染演示数据
if new_pid:
    _c2 = pymysql.connect(**config.db_config())
    try:
        with _c2.cursor() as _cur:
            _cur.execute("DELETE FROM payees WHERE id=%s", (new_pid,))
        _c2.commit()
    finally:
        _c2.close()

# --------------------------------------------------------------------------- #
banner(8, "姓名不符：站内卡实名李*，输入王五 → name_mismatch")
st, r = resolve({"nickname": "李雷", "real_name": "王五"})
check("insite 解析成功", r.get("ok") is True and r.get("track") == "insite")
check("name_mismatch=True", r.get("name_mismatch") is True, str(r.get("name_mismatch")))
st, r2 = resolve({"nickname": "李雷", "real_name": "李哥"})
check("同名无不符", r2.get("name_mismatch") is False)

# --------------------------------------------------------------------------- #
banner(6, "站内转账全链路回归：转账→李哥入账→余额守恒")
# 取指定用户主账户余额（HTTP /api/balance 只认 token 用户，这里直接读库更可靠）
def bal_of(uid):
    import pymysql
    conn = pymysql.connect(**config.db_config())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT balance FROM accounts WHERE user_id=%s ORDER BY id ASC LIMIT 1",
                (uid,))
            row = cur.fetchone()
            return float(row["balance"]) if row else None
    finally:
        conn.close()
sender_before = bal_of(1)
recv_before = bal_of(58)
st, r = call("POST", "/api/transfer", json_body={"payee_id": 3, "amount": 100.0})
check("转账成功", r.get("ok") is True and r.get("track") == "insite", json.dumps(r, ensure_ascii=False)[:160])
sender_after = bal_of(1)
recv_after = bal_of(58)
check("发送方扣款 -100", abs((sender_before - sender_after) - 100) < 0.01, f"{sender_before}->{sender_after}")
check("收款方入账 +100", abs((recv_after - recv_before) - 100) < 0.01, f"{recv_before}->{recv_after}")
check("余额守恒", abs((sender_before + recv_before) - (sender_after + recv_after)) < 0.01)

# --------------------------------------------------------------------------- #
banner(7, "跨行转账：妈妈(招行) → A 扣款 + 受理通知(清算中)，妈妈侧无变化")
mom = next((p for p in payees if p.get("nickname") == "妈妈" and p.get("bank") == "招商银行"), None)
mom_pid = mom["id"] if mom else None
sender_before2 = bal_of(1)
st, r = call("POST", "/api/transfer", json_body={"payee_id": mom_pid, "amount": 50.0})
check("跨行转账成功(offsite轨)", r.get("ok") is True and r.get("track") == "offsite", json.dumps(r, ensure_ascii=False)[:160])
sender_after2 = bal_of(1)
check("发送方扣款 -50", abs((sender_before2 - sender_after2) - 50) < 0.01)
# ★ 跨行转账改为「在途清算」模型：不再立即承诺"2小时内到账"，
#   而是受理通知（资金清算中），成功后到账、异常则全额退回。
check("受理通知含清算中", "清算中" in (r.get("message") or ""), str(r.get("message")))

# --------------------------------------------------------------------------- #
print("\n================ 汇总 ================")
passed = sum(1 for _, c in results if c)
print(f"PASS {passed}/{len(results)}")
fails = [n for n, c in results if not c]
if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("ALL GREEN")
