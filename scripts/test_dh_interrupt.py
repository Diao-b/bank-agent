"""验收：数字人「打断 = 真的闭嘴 + 不把自己的声音录进去」。

场景（用户反馈）：点「打断」后继续说话，AI 还在念上一句 →
把自己的声音识别成用户的话，串进输入框 / 直接被发送。

三道防线，缺一件就会复发：
  ① 后端：打断端点置位「闭嘴」（_muted），在途的 done 帧到达也不再开口
  ② 后端：新用户消息到达时解除闭嘴（否则打断一次就永远不说话了）
  ③ 前端：打断后开一道「余音衰减期」，期内识别结果一律丢弃
     + 普通模式（非语音对话）也必须过自听过滤 —— 此前那里**完全没有过滤**

本脚本只测①②（可确定性验证）；③ 做静态断言（前端是内联 JSX，无法在此执行）。

用法：
    .venv/Scripts/python.exe scripts/test_dh_interrupt.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def record(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"   {extra}" if extra else ""))


def main() -> int:
    from utils.digital_human import digital_human as dh

    print("=" * 68)
    print("数字人打断契约验收")
    print("=" * 68)

    # ---------- 1) 未启用时 stop() 明确返回 False（不再静默吞异常） ----------
    print("\n[1] 未启用（FAY_API_URL 为空）时的行为")
    saved_enabled = dh.enabled
    dh.enabled = False
    record("未启用 → stop() 返回 False（不再假装成功）", dh.stop() is False)
    dh.enabled = saved_enabled

    # ---------- 2) 启用态：stop 送达 + 重试 ----------
    print("\n[2] 启用态：stop() 送达判定与重试")
    dh._muted = False
    with mock.patch.object(dh, "enabled", True), \
         mock.patch("utils.digital_human.requests.post") as post:
        post.return_value = mock.Mock(status_code=200)
        ok = dh.stop()
        record("送达成功 → 返回 True", ok is True, f"calls={post.call_count}")
        record("成功时不重试（只发 1 次）", post.call_count == 1, f"calls={post.call_count}")

        post.reset_mock()
        post.return_value = mock.Mock(status_code=503)
        ok2 = dh.stop()
        record("5xx → 重试一次后返回 False", ok2 is False, f"calls={post.call_count}")
        record("确实重试了（2 次）", post.call_count == 2, f"calls={post.call_count}")

        post.reset_mock()
        post.side_effect = OSError("Fay 挂了")
        ok3 = dh.stop()
        record("网络异常 → 重试后返回 False（不静默吞）", ok3 is False, f"calls={post.call_count}")

    # ---------- 3) ★ 打断 = 闭嘴 ----------
    print("\n[3] ★ interrupt() 置位闭嘴：在途 done 帧到达也不再开口")
    dh._muted = False
    with mock.patch.object(dh, "enabled", True), \
         mock.patch("utils.digital_human.requests.post") as post:
        post.return_value = mock.Mock(status_code=200)

        delivered = dh.interrupt()
        record("interrupt() 返回送达结果", delivered is True)
        record("interrupt() 后置位 _muted（闭嘴）", dh._muted is True)

        post.reset_mock()
        dh.speak("这笔转账已经完成了，请注意查收")
        record("★ 闭嘴期间 speak() 不发任何请求（打断后不再念）",
               post.call_count == 0, f"calls={post.call_count}")

        # maybe_speak_event / maybe_speak_reply 走同一出口，也必须被拦下
        post.reset_mock()
        dh.maybe_speak_event({"type": "done", "payload": {"reply": "你好呀"}}, True)
        dh.maybe_speak_reply("你好呀", True)
        record("★ 闭嘴期间 SSE done 帧 / 非流式回复也都不开口",
               post.call_count == 0, f"calls={post.call_count}")

    # ---------- 4) ★ 新用户消息解除闭嘴 ----------
    print("\n[4] ★ 下一条用户消息到达 → 解除闭嘴（否则打断一次就永远哑了）")
    with mock.patch.object(dh, "enabled", True), \
         mock.patch("utils.digital_human.requests.post") as post:
        post.return_value = mock.Mock(status_code=200)
        record("解除前仍是闭嘴态", dh._muted is True)
        dh.unmute()
        record("unmute() 后 _muted=False", dh._muted is False)

        post.reset_mock()
        dh.speak("好的，我来帮您查一下")
        record("解除后 speak() 恢复开口", post.call_count == 1, f"calls={post.call_count}")

        # 再打断一次 → 又闭嘴（可重复）
        dh.interrupt()
        post.reset_mock()
        dh.speak("又来一句")
        record("可重复打断：再次 interrupt 后又闭嘴", post.call_count == 0, f"calls={post.call_count}")

    # ---------- 5) run_server._dh_interrupt 的语义 ----------
    print("\n[5] run_server._dh_interrupt()：新消息 = stop + unmute（新一轮允许开口）")
    dh._muted = True
    with mock.patch.object(dh, "enabled", True), \
         mock.patch.object(dh, "stop", return_value=True) as st:
        import run_server
        run_server._dh_interrupt()
        record("_dh_interrupt 调用了 stop()", st.call_count == 1, f"calls={st.call_count}")
        record("★ _dh_interrupt 解除了闭嘴（新回复照常念）", dh._muted is False)

    # ---------- 6) 端点契约：返回 delivered ----------
    print("\n[6] 端点契约 POST /api/digital-human/interrupt")
    from fastapi.testclient import TestClient
    import run_server
    client = TestClient(run_server.app)
    with mock.patch.object(run_server.digital_human, "interrupt", return_value=True) as mi:
        r = client.post("/api/digital-human/interrupt")
        body = r.json()
        record("端点可达（200）", r.status_code == 200, str(r.status_code))
        record("调用的是 interrupt()（带闭嘴语义）而非 stop()", mi.call_count == 1)
        record("★ 响应带 delivered 字段（前端据此判断有没有真停住）",
               "delivered" in body, str(body))
        record("delivered=True 时值为 true", body.get("delivered") is True, str(body))

    with mock.patch.object(run_server.digital_human, "interrupt", return_value=False):
        r = client.post("/api/digital-human/interrupt")
        record("★ 未送达时 delivered=false（前端据此延长丢弃窗口）",
               r.json().get("delivered") is False, str(r.json()))

    # ---------- 7) 前端静态断言 ----------
    print("\n[7] 前端三道防线（静态断言，内联 JSX 无法在此执行）")
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    checks = [
        ("打断后设置余音衰减期", "vdMuteUntilRef.current = Date.now() + VD_ECHO_GUARD_MS"),
        ("余音衰减期内识别结果一律丢弃", "if (Date.now() < vdMuteUntilRef.current)"),
        ("未送达时延长丢弃窗口", "d.delivered === false"),
        ("★ 普通模式也过自听过滤（此前完全没有）",
         "      if (vdLooksLikeEcho(t)) {\n        setVdEcho((n) => n + 1);\n        if (speechRef.current)"),
        ("普通模式丢弃时给可见提示", "speechRef.current.flash("),
        ("自听窗口按播报预计结束起算", "vdEchoUntilRef.current = Date.now()"),
        ("数字人关闭时不设自听窗口（防误伤用户原话）", "localStorage.getItem('xb_digital_human') === '1'"),
    ]
    for name, needle in checks:
        record(name, needle in html, "" if needle in html else f"缺少：{needle}")

    print("\n" + "=" * 68)
    print(f"结果：PASS {PASS} / {PASS + FAIL}" + ("   ALL GREEN" if not FAIL else "   有失败项"))
    print("=" * 68)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
