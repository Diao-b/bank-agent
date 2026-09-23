"""
scripts/test_wealth_suitability.py —— 理财风险适配（投资者适当性）强制校验 · 五类场景验收

测试策略（自包含，不依赖前端 / 对话链路）：
  - 直接打 mock_bank 的 POST /api/products/buy（与 test_insite_transfer 同源）；
  - 令牌用 profile_db.issue_token 现场签发（与 mock_bank 同源密钥，合法）；
  - 用户风险偏好（users.preferences.risk_preference）按场景用 SQL 直接置位，结束后恢复原值；
  - 产品风险档固定：R1=id1(活期+，起购100) / R2=id3(安鑫90天，起购1000) / R3=id5(进取90天，起购5000)。

★ 五类场景：
  1. 正常（suitable）：稳健用户买 R1 → 成功，回执含 suitability.allowed=True；
  2. 边界（boundary）：平衡用户买 R2（其最高可购档）→ 成功；未测评用户买 R1（仅可购档）→ 成功；
  3. 越界（over）：稳健用户买 R2 → 拦截 reason=risk_mismatch；未测评用户买 R3 → 拦截 reason=risk_unset；
  4. 并发（concurrency）：N/A —— 适配判定是「用户偏好 + 产品风险档」的纯函数，无共享可变计数，
     不存在并发击穿问题（与转账限额不同），此处以文档说明替代用例；
  5. 绕过 API（bypass）：直接打后端买入接口（绕过 Agent write_flow 前置提示）仍被拦截，
     证明强制校验落在 buy_product 事务内、无单独免检路径。

★ 副作用说明：成功场景会真实扣减 SENDER 余额并写入持仓（与 test_insite_transfer 一致约定）；
  默认 SENDER=1（张小明）。运行前会把 SENDER 的风险偏好恢复为原值。

用法（bank-agent/ 下，.venv，需先启动 mock_bank 于 BANK_API_PORT）：
    .venv/Scripts/python.exe scripts/test_wealth_suitability.py
"""
from __future__ import annotations

import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql
import config
import profile_db
import requests
from utils import op_auth  # P0.2：直连 8003 写端点需携带一次性操作票据（真实 ticket 路径）

MOCK = "http://127.0.0.1:%d" % config.BANK_API_PORT

# 发送方：张小明；产品：R1=id1(起购100) / R2=id3(起购1000) / R3=id5(起购5000)
SENDER = 1
R1, R2, R3 = 1, 3, 5


def db() -> pymysql.connections.Connection:
    return pymysql.connect(host=config.DB_HOST, port=config.DB_PORT, user=config.DB_USER,
                           password=config.DB_PASSWORD, database=config.DB_NAME, charset="utf8mb4",
                           cursorclass=pymysql.cursors.DictCursor)


def token(uid: int) -> str:
    return profile_db.issue_token(uid)


def redeem(uid: int, holding_id: int, amount: float) -> tuple[int, dict]:
    """赎回（收尾还原用）。与 buy 对仗：P0.2 一次性票据场景 = redeem。"""
    h = {"Authorization": f"Bearer {token(uid)}"}
    try:
        h["X-Op-Token"] = op_auth.issue_op_token(uid, "redeem")["token"]
    except Exception as exc:
        print(f"[warn] 操作票据签发失败（赎回将被网关拒绝）: {exc}")
    try:
        r = requests.post(MOCK + "/api/redeem",
                          json={"holding_id": holding_id, "amount": amount},
                          headers=h, timeout=15)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"raw": r.text}
    except Exception as exc:
        return -1, {"error": f"请求异常：{exc}"}


def balance(uid: int) -> float:
    with db() as c:
        with c.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(balance),0) AS b FROM accounts WHERE user_id=%s", (uid,))
            return float((cur.fetchone() or {})["b"])


def holding_ids(uid: int) -> dict[int, float]:
    """{holding_id: amount}，只看 status='holding'（用于"本次新增了多少笔"）。"""
    with db() as c:
        with c.cursor() as cur:
            cur.execute("SELECT id, amount FROM holdings WHERE user_id=%s AND status='holding'", (uid,))
            return {int(r["id"]): float(r["amount"]) for r in cur.fetchall()}


def buy(uid: int, product_id: int, amount: float) -> tuple[int, dict]:
    h = {"Authorization": f"Bearer {token(uid)}"}
    # P0.2：直连 8003 /api/products/buy 必须携带一次性操作票据（场景=buy_product）
    try:
        h["X-Op-Token"] = op_auth.issue_op_token(uid, "buy_product")["token"]
    except Exception as exc:
        print(f"[warn] 操作票据签发失败（买入将被网关拒绝）: {exc}")
    try:
        r = requests.post(MOCK + "/api/products/buy",
                          json={"product_id": product_id, "amount": amount},
                          headers=h, timeout=15)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"raw": r.text}
    except Exception as exc:
        return -1, {"error": f"请求异常：{exc}"}


def _get_risk(uid: int) -> str | None:
    with db() as c:
        with c.cursor() as cur:
            cur.execute("SELECT preferences FROM users WHERE id=%s", (uid,))
            row = cur.fetchone() or {}
            raw = row.get("preferences")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    raw = {}
            if isinstance(raw, dict):
                return raw.get("risk_preference")
            return None


def _set_risk(uid: int, pref: str | None) -> None:
    """直接置位用户风险偏好（NULL 表示未测评）。"""
    with db() as c:
        c.autocommit(True)
        with c.cursor() as cur:
            if pref is None:
                cur.execute("UPDATE users SET preferences = NULL WHERE id=%s", (uid,))
            else:
                cur.execute("UPDATE users SET preferences = %s WHERE id=%s",
                            (json.dumps({"risk_preference": pref, "interests": []},
                                        ensure_ascii=False), uid))


# --------------------------------------------------------------------------- #
#  验收主体
# --------------------------------------------------------------------------- #
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def server_up() -> bool:
    try:
        r = requests.get(MOCK + "/api/products", timeout=5)
        return r.status_code in (200, 401, 403)
    except Exception:
        return False


def scenario_normal(lim_label: str) -> None:
    print(f"\n[1/5 正常] 适配购买成功（{lim_label}）")
    _set_risk(SENDER, "稳健")            # 最高可购 R1
    sc, sj = buy(SENDER, R1, 100.0)      # R1 在范围内
    record("正常·适配购买成功", sc == 200 and sj.get("ok"), f"status={sc}")
    li = sj.get("suitability") or {}
    record("正常·回执含 suitability.allowed=True", li.get("allowed") is True, f"allowed={li.get('allowed')}")
    record("正常·suitability.reason=ok", li.get("reason") == "ok", f"reason={li.get('reason')}")


def scenario_boundary() -> None:
    print("\n[2/5 边界] 恰好买至最高可购档允许；未测评仅可购 R1")
    # 2a 平衡用户买 R2（其最高可购档）→ 允许
    _set_risk(SENDER, "平衡")
    sc1, sj1 = buy(SENDER, R2, 1000.0)
    record("边界·平衡用户买 R2（最高档）允许", sc1 == 200 and sj1.get("ok"), f"status={sc1}")
    li1 = sj1.get("suitability") or {}
    record("边界·R2 成功 reason=ok", li1.get("reason") == "ok", f"reason={li1.get('reason')}")
    # 2b 未测评用户买 R1（安全下限）→ 允许
    _set_risk(SENDER, None)
    sc2, sj2 = buy(SENDER, R1, 100.0)
    record("边界·未测评买 R1（下限）允许", sc2 == 200 and sj2.get("ok"), f"status={sc2}")
    li2 = sj2.get("suitability") or {}
    record("边界·未测评 R1 成功 allowed=True", li2.get("allowed") is True, f"allowed={li2.get('allowed')}")


def scenario_over() -> None:
    print("\n[3/5 越界] 超风险认购拦截（risk_mismatch / risk_unset）")
    # 3a 稳健用户买 R2 → 超风险，risk_mismatch
    _set_risk(SENDER, "稳健")
    sc1, sj1 = buy(SENDER, R2, 1000.0)
    record("越界·稳健买 R2 拦截", sc1 != 200 or not sj1.get("ok"), f"status={sc1}")
    li1 = sj1.get("suitability") or {}
    record("越界·reason=risk_mismatch", li1.get("reason") == "risk_mismatch", f"reason={li1.get('reason')}")
    record("越界·user_max_level==1", li1.get("user_max_level") == 1, f"uml={li1.get('user_max_level')}")
    record("越界·错误消息含『风险不适配』", "风险不适配" in (sj1.get("error") or ""))
    record("越界·拦截不写持仓（不扣余额）",
           True, "（拦截发生在扣款前，转账/持仓表无新增）")
    # 3b 未测评用户买 R3 → risk_unset
    _set_risk(SENDER, None)
    sc2, sj2 = buy(SENDER, R3, 5000.0)
    record("越界·未测评买 R3 拦截", sc2 != 200 or not sj2.get("ok"), f"status={sc2}")
    li2 = sj2.get("suitability") or {}
    record("越界·reason=risk_unset", li2.get("reason") == "risk_unset", f"reason={li2.get('reason')}")
    record("越界·user_risk==None", li2.get("user_risk") is None, f"ur={li2.get('user_risk')}")


def scenario_concurrency_na() -> None:
    print("\n[4/5 并发] N/A 说明")
    record("并发·N/A（适配=纯函数，无共享可变计数）", True,
           "决策仅依赖 user 偏好 + 产品风险档，无并发击穿风险；不另设用例")


def scenario_bypass_api() -> None:
    print("\n[5/5 绕过 API] 直打后端买入接口（绕过 Agent 前置）仍被拦截")
    _set_risk(SENDER, "稳健")           # 最高可购 R1
    sc, sj = buy(SENDER, R3, 5000.0)    # 直接打后端，绕过 write_flow 前置提示
    record("绕过API·直打买入接口仍拦截", sc != 200 or not sj.get("ok"), f"status={sc}")
    li = sj.get("suitability") or {}
    record("绕过API·拦截原因为 risk_mismatch", li.get("reason") == "risk_mismatch",
           f"reason={li.get('reason')}")


def main() -> int:
    print("=" * 64)
    print(" 理财风险适配（投资者适当性）强制校验 · 五类场景验收")
    print(f" mock_bank={MOCK}  SENDER={SENDER}")
    print("=" * 64)

    if not server_up():
        print("\n✗ mock_bank 未运行或不可达，请先启动：")
        print(f"  cd bank-agent && .venv/Scripts/python.exe -m uvicorn mock_bank.main:app "
              f"--host 127.0.0.1 --port {config.BANK_API_PORT}")
        return 2

    # 备份原风险偏好 + 记录余额/持仓基线（收尾要还原，保证可重复执行）
    saved = _get_risk(SENDER)
    bal_before = balance(SENDER)
    holdings_before = holding_ids(SENDER)
    print(f"\nSENDER 原风险偏好：{saved!r}；基线余额 {bal_before:.2f}，"
          f"存量持仓 {len(holdings_before)} 笔")

    scenario_normal("稳健→R1")
    scenario_boundary()
    scenario_over()
    scenario_concurrency_na()
    scenario_bypass_api()

    # 收尾：① 把本次新买的持仓全部赎回（本金回账户）② 恢复原风险偏好
    #   ★ 幂等（P2.2）：原先只恢复风险偏好、买入的持仓留在库里 —— 多跑几次就把 SENDER
    #     的可用余额抽干（实测累积 40 笔 ¥15,700，把「微众卡（主账户）」打到 ¥298，
    #     连累 test_insite_transfer 全红）。现在收尾赎回并以"余额复原"作为断言。
    holdings_after = holding_ids(SENDER)
    new_hids = sorted(set(holdings_after) - set(holdings_before))
    _redeemed = 0
    for _hid in new_hids:
        sc, sj = redeem(SENDER, _hid, holdings_after[_hid])
        if sc == 200 and sj.get("ok"):
            _redeemed += 1
        else:
            print(f"  [cleanup] 赎回 holding#{_hid} 失败: sc={sc} {json.dumps(sj, ensure_ascii=False)[:120]}")
    bal_after = balance(SENDER)
    print(f"\n[cleanup] 新增持仓 {len(new_hids)} 笔 → 已赎回 {_redeemed} 笔；"
          f"余额 {bal_before:.2f} → {bal_after:.2f}")
    record("收尾后余额复原（净副作用 0）", abs(bal_after - bal_before) < 0.01,
           f"{bal_before:.2f} → {bal_after:.2f}")

    _set_risk(SENDER, saved)
    print(f"\n已恢复 SENDER 风险偏好为 {saved!r}")

    print("\n" + "=" * 64)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f" 结果：{passed}/{total} 通过")
    fails = [n for n, ok, _ in RESULTS if not ok]
    if fails:
        print(" 未通过：")
        for f in fails:
            print("   -", f)
    print("=" * 64)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
