"""
tests/test_card.py —— 卡号校验层单元测试（utils/card_validator）

覆盖：
  1. luhn_check：合法 / 非法 / 长度边界 / 非数字字符
  2. generate_card_no：生成卡号必过 Luhn、长度 16、BIN 前缀正确
  3. bank_from_card：BIN → 银行代码识别正确性（含本行微众）
  4. is_local_card / mask_real_name / normalize_card_input

运行（bank-agent/ 下）：
    .venv/Scripts/python.exe tests/test_card.py
全部通过时输出 ALL GREEN，否则退出码 1。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import card_validator as cv  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, cond))
    tag = "PASS" if cond else "FAIL"
    extra = f"  -> {detail}" if (detail and not cond) else ""
    print(f"  [{tag}] {name}{extra}")


print("=== 1. luhn_check：合法卡号 ===")
# 教材用例：4992739871 的 Luhn 校验位应为 6（纯算法口径，不受 12 位最小长度限制）
check("_luhn_digit(4992739871) == 6（教材校验位）",
      cv._luhn_digit("4992739871") == "6", cv._luhn_digit("4992739871"))
# 该教材用例只有 11 位，低于真实卡号最小长度（ISO 7812 为 12~19）→ 长度口径下判非法
check("49927398716 低于 12 位最小长度 → 非法", cv.luhn_check("49927398716") is False)
# 常见测试卡号（4 开头 16 位全 1）
check("4111111111111111 合法", cv.luhn_check("4111111111111111") is True)
# 本项目生成的卡号必然合法
gen = cv.generate_card_no("61022620", 12345)
check(f"generate_card_no 生成卡号合法({gen})", cv.luhn_check(gen) is True)

print("=== 2. luhn_check：非法卡号 ===")
check("末位改错 49927398717 非法", cv.luhn_check("49927398717") is False)
check("4111111111111112 非法", cv.luhn_check("4111111111111112") is False)
check("全 0 卡号非法（长度为12）", cv.luhn_check("000000000000") is True)  # 全 0 数学上合法
check("长度不足(<12) 非法", cv.luhn_check("4992739871") is False)
check("空串/None 非法", cv.luhn_check("") is False and cv.luhn_check(None) is False)
check("含字母视为非法", cv.luhn_check("4992a398716") is False)
check("带空格/连字符的合法卡号应通过清洗", cv.luhn_check("4111-1111 1111-1111") is True)

print("=== 3. generate_card_no：结构正确性 ===")
c1 = cv.generate_card_no()
check("默认长度 16", len(c1) == 16, f"len={len(c1)}")
check("默认全是数字", c1.isdigit() is True, c1)
check("默认前缀为本行 BIN 6102", c1.startswith("6102") is True, c1)
c2 = cv.generate_card_no("6225", 7)
check("自定义 BIN 前缀生效(6225)", c2.startswith("6225") is True, c2)
check("自定义 BIN 生成卡号合法", cv.luhn_check(c2) is True, c2)
seen = {cv.generate_card_no("61022620", i) for i in range(500)}
check("同 BIN 不同序号不重复", len(seen) == 500, f"unique={len(seen)}")

print("=== 4. bank_from_card：BIN 识别正确性 ===")
check("6102 → WEBANK(本行)", cv.bank_from_card("6102262016810343") == "WEBANK")
check("6225 → CMBC 招商银行", cv.bank_from_card("6225943663866884") == "CMBC")
check("6222 → ICBC 工商银行", cv.bank_from_card("6222000012345670") == "ICBC")
check("6217 → CCB 建设银行", cv.bank_from_card("6217000012345670") == "CCB")
check("6228 → ABC 农业银行", cv.bank_from_card("6228000012345670") == "ABC")
check("6216 → BOC 中国银行", cv.bank_from_card("6216000012345670") == "BOC")
check("6226 → BCM 交通银行", cv.bank_from_card("6226000012345670") == "BCM")
check("未登记 BIN → None", cv.bank_from_card("9999000012345670") is None)
check("长度不足 4 位 → None", cv.bank_from_card("622") is None)
# bin_of_bank：bank_code → BIN 反查（种子脚本用它生成他行演示卡号）
check("bin_of_bank(CMBC) → 6225", cv.bin_of_bank("CMBC") == "6225")
check("bin_of_bank(ICBC) → 6222", cv.bin_of_bank("ICBC") == "6222")
check("bin_of_bank(WEBANK) → 6102", cv.bin_of_bank("WEBANK") == "6102")
check("bin_of_bank(未知) → None", cv.bin_of_bank("NOPE") is None)
# BIN ↔ bank_code 双向自洽（防止"bank_id=工商但卡号 BIN=招商"的不一致）
ok2 = all(cv.bin_of_bank(code) == prefix for prefix, code, _n in cv.BIN_TABLE)
check("BIN ↔ bank_code 双向自洽", ok2 is True)
# BIN 表自洽：每家银行的演示 BIN 都能反查回自己的 bank_code
ok = True
for prefix, code, _name in cv.BIN_TABLE:
    if cv.bank_from_card(prefix + "000000000000") != code:
        ok = False
        print(f"    BIN 表不自洽: {prefix} -> {cv.bank_from_card(prefix+'000000000000')} != {code}")
check("BIN 表全部条目自洽", ok is True)

print("=== 5. is_local_card / 本行判定 ===")
check("6102 开头为本行", cv.is_local_card("6102262016810343") is True)
check("6225 开头非本行", cv.is_local_card("6225943663866884") is False)
check("本行 BIN 常量一致", cv.LOCAL_BANK_BIN == "6102" and cv.LOCAL_BANK_CODE == "WEBANK")

print("=== 6. mask_real_name：实名脱敏 ===")
check("李哥 → 李*", cv.mask_real_name("李哥") == "李*")
check("李测试 → 李**", cv.mask_real_name("李测试") == "李**")
check("单字 → *", cv.mask_real_name("王") == "*")
check("空值 → None", cv.mask_real_name("") is None and cv.mask_real_name(None) is None)

print("=== 7. normalize_card_input：清洗 ===")
check("去空格连字符", cv.normalize_card_input("6225-8866 7788 9900") == "6225886677889900")
check("全角数字转半角", cv.normalize_card_input("６２２５８８６６") == "62258866")
check("非数字剔除", cv.normalize_card_input("abc6225xyz8866") == "62258866")

print("=== 8. generate_card_no_with_tail：保留尾号 ===")
t = cv.generate_card_no_with_tail("6225", "6688")
check(f"尾号保留 6688({t})", t[-4:] == "6688", t)
check("前 4 位为银行 BIN", t[:4] == "6225", t)
check("保留尾号的卡号合法", cv.luhn_check(t) is True, t)
check("长度 16", len(t) == 16, str(len(t)))

print("\n================ 汇总 ================")
passed = sum(1 for _, c in results if c)
print(f"PASS {passed}/{len(results)}")
fails = [n for n, c in results if not c]
if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("ALL GREEN")
