"""
scripts/test_op_auth_negative.py —— P0.2 一次性操作票据（X-Op-Token）反向验收

证明「直连 8003 写端点」在缺失 / 场景不匹配 / 复用 / 过期 / 签名伪造 时一律 4xx，
且生产代码不存在任何绕过分支（合法票据才放行到业务层）。

覆盖用例：
  1) 无票据（X-Op-Token 缺失）                       → 401
  2) 场景不匹配（为 buy_product 签发的票据用于 transfer） → 401
  3) 签名伪造（篡改 uid 但不重签）                    → 401
  4) 过期（exp 已过、签名仍合法）                     → 401
  5) 复用（同一票据第二次消费，DB 层）                → OP_TOKEN_USED
  5b) 复用（HTTP 层：经 HTTP 消费后同票据再打）        → 401
  6) 正向对照：全新合法票据                           → 非 401（走到业务层）

用法（bank-agent/ 下，.venv，需要 MySQL；不需要 8178）：
    .venv/Scripts/python.exe scripts/test_op_auth_negative.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import profile_db  # noqa: E402
from mock_bank.main import app  # noqa: E402
from utils import op_auth  # noqa: E402

# 关掉"服务端异常向上抛"：业务侧异常应表现为 5xx，而不是让测试进程崩溃
client = TestClient(app, raise_server_exceptions=False)

UID = 1          # 张小明
SCENE = "transfer"   # POST /api/transfer 绑定的场景

PASS, FAIL = [], []


def check(name: str, ok: bool, extra: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(extra)) if extra else ""))


def auth() -> dict:
    return {"Authorization": f"Bearer {profile_db.issue_token(UID)}"}


def post_transfer(extra_headers: dict | None = None) -> "object":
    """直连 8003 /api/transfer。用「合法收款人 + 超单笔限额金额」作为业务参数：
    网关放行后必然被限额逻辑干净拒绝（ok=false），从而不影响真实资金；
    若票据无效，则网关在进业务前即 401。"""
    h = auth()
    if extra_headers:
        h.update(extra_headers)
    return client.post("/api/transfer", headers=h,
                       json={"payee_id": 1, "amount": 99999999.0})


def main() -> int:
    print("=" * 68)
    print(" P0.2 操作票据（X-Op-Token）反向验收")
    print("=" * 68)

    # 1) 无票据
    r = post_transfer()
    check("无票据 → 401", r.status_code == 401, f"status={r.status_code}")

    # 2) 场景不匹配
    tok_buy = op_auth.issue_op_token(UID, "buy_product")["token"]
    r = post_transfer({"X-Op-Token": tok_buy})
    check("场景不匹配（buy_product 用于 transfer）→ 401", r.status_code == 401, f"status={r.status_code}")

    # 3) 签名伪造：篡改 uid 段，不重签
    tok = op_auth.issue_op_token(UID, SCENE)["token"]
    parts = tok.split(".")
    parts[0] = "999"                       # 把 uid 改成别人
    r = post_transfer({"X-Op-Token": ".".join(parts)})
    check("签名伪造（篡改 uid）→ 401", r.status_code == 401, f"status={r.status_code}")

    # 4) 过期：exp 已过、签名合法（用 op_auth 内部签名器构造）
    nonce = os.urandom(16).hex()
    exp = int(time.time()) - 10
    payload = f"{UID}.{SCENE}.{nonce}.{exp}"
    expired = f"{payload}.{op_auth._sign(payload)}"
    r = post_transfer({"X-Op-Token": expired})
    check("过期票据 → 401", r.status_code == 401, f"status={r.status_code}")

    # 5) 复用（DB 层）：首次消费成功，第二次 USED
    tok2 = op_auth.issue_op_token(UID, SCENE)["token"]
    res1 = op_auth.consume_op_token(UID, SCENE, tok2)
    check("合法票据首次消费 → ok", res1.get("ok") is True, str(res1))
    res2 = op_auth.consume_op_token(UID, SCENE, tok2)
    check("同一票据复用 → OP_TOKEN_USED",
          res2.get("ok") is False and res2.get("code") == "OP_TOKEN_USED", str(res2))

    # 5b) 复用（HTTP 层）：合法票据经 HTTP 被消费后，同票据再打 → 401
    tok3 = op_auth.issue_op_token(UID, SCENE)["token"]
    r_first = post_transfer({"X-Op-Token": tok3})
    check("合法票据 → 网关放行（非 401，走到业务层）", r_first.status_code != 401,
          f"status={r_first.status_code}")
    r_reuse = post_transfer({"X-Op-Token": tok3})
    check("复用（HTTP）→ 401", r_reuse.status_code == 401, f"status={r_reuse.status_code}")

    # 6) 正向对照：全新合法票据 → 非 401
    tok4 = op_auth.issue_op_token(UID, SCENE)["token"]
    r = post_transfer({"X-Op-Token": tok4})
    check("全新合法票据 → 非 401", r.status_code != 401, f"status={r.status_code}")

    print()
    print(f"===== 结果：PASS {len(PASS)} / FAIL {len(FAIL)} =====")
    for f in FAIL:
        print("  FAILED:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
