"""数字柜员桥接层自测（跑完可删）。

覆盖：
1) 未配置 Fay 时全部静默 no-op（不抛异常）；
2) 情绪启发式映射；
3) ★ 对着 Mock Fay 核验真实接口契约（方法/路径/载荷/Content-Type）。
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from utils.digital_human import digital_human, EMOTION_MAP  # noqa: E402

print("=" * 60)
print("[1] 关闭态静默")
print("enabled:", digital_human.enabled, "| FAY_API_URL:", repr(config.FAY_API_URL))
digital_human.speak("测试", "happy")
digital_human.stop()
digital_human.maybe_speak_event({"type": "done", "payload": {"reply": "转账成功"}}, True)
digital_human.maybe_speak_reply("好的", True)
print("is_ready():", digital_human.is_ready(), "| proxy():", digital_human.proxy("x", "GET"))
if not config.FAY_API_URL:
    assert digital_human.enabled is False
    assert digital_human.is_ready() is False
    assert digital_human.proxy("x", "GET") is None
    print("  [OK] 未配置 Fay：全部静默 no-op，无异常")

print("=" * 60)
print("[2] 情绪映射")
d = digital_human.derive_emotion
cases = {
    "转账金额 ¥25000 超过风控限额，已被拦截": "serious",
    "转账成功，收款方已到账": "happy",
    "抱歉，本次操作未能完成": "sorry",
    "您当前账户余额为 ¥1200.00": "normal",
    "支付密码错误，账户已被锁定": "serious",
}
for text, want in cases.items():
    got = d(text)
    print(f"  {want:8s} <- {got:8s} | {text}")
    assert got == want, f"emotion mismatch: {got} != {want}"

print("=" * 60)
print("[3] 对 Mock Fay 核验真实契约")
captured = {}


class _MockFay(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静音
        pass

    def do_GET(self):
        captured["GET"] = self.path
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"server":true,"digital_human":false}')

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        captured[self.path] = {"ctype": self.headers.get("Content-Type", ""), "body": raw}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"result":"successful"}')


srv = HTTPServer(("127.0.0.1", 0), _MockFay)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

# 把单例指向 Mock（模拟 Fay 已启动）
digital_human.enabled = True
digital_human.api_url = f"http://127.0.0.1:{port}"

digital_human.speak("转账成功，已到账", "happy")
digital_human.stop()
ready = digital_human.is_ready()
srv.shutdown()

send = captured.get("/transparent-pass")
assert send is not None, "未收到 POST /transparent-pass"
assert send["ctype"].startswith("application/x-www-form-urlencoded"), send["ctype"]
data_str = parse_qs(send["body"]).get("data", [None])[0]
obj = json.loads(data_str)
print("  POST /transparent-pass  form.data =", obj)
assert obj["user"] == digital_human.username, obj
assert obj["text"].startswith("转账成功"), obj

stop = captured.get("/to-stop-talking")
assert stop is not None, "未收到 POST /to-stop-talking"
stop_obj = json.loads(stop["body"])
print("  POST /to-stop-talking  json =", stop_obj)
assert stop_obj["username"] == digital_human.username, stop_obj

print("  GET ", captured.get("GET"), "→ is_ready() =", ready)
assert captured.get("GET") == "/api/get-system-status"
assert ready is True

print("=" * 60)
print("ALL_BRIDGE_TESTS_OK  (EMOTION_MAP=%s)" % EMOTION_MAP)
