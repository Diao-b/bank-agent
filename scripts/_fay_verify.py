"""数字柜员 Fay 端到端验证（跑完可删）。

验证链路（对应验收 #3）：HTTP 喂话 → Fay 生成 → WS 10002 数字人驱动端收到
{"Topic":"human","Data":{Key:"audio",Text,Lips,Sentiment,Action}} 帧（Text=同句, Lips=口型, Sentiment/Action=情绪）。

用 **Fay 的 venv** 运行（需要 websockets + requests）：
    cd fay/Fay && .venv/Scripts/python.exe <此文件路径>

前置：Fay 已启动（python main.py start），HTTP 5000 / WS 10002 在线。
"""
import asyncio
import json
import sys
import threading
import time

import requests

BASE = "http://127.0.0.1:5000"
WS = "ws://127.0.0.1:10002"
USER = "xiaowei"
FEED_TEXT = "你好，请用一句话介绍你自己"


def http_probe():
    try:
        r = requests.get(BASE + "/api/get-system-status", timeout=5)
        print(f"[HTTP] GET /api/get-system-status -> {r.status_code} {r.text[:180]}")
        return r.status_code == 200
    except Exception as e:
        print(f"[HTTP] get-system-status 失败: {e}")
        return False


def _feed():
    time.sleep(1.2)  # 等 WS 注册完成
    try:
        r = requests.post(
            BASE + "/api/send",
            data={"data": json.dumps({"username": USER, "msg": FEED_TEXT})},
            timeout=10,
        )
        print(f"[HTTP] POST /api/send -> {r.status_code} {r.text[:180]}")
    except Exception as e:
        print(f"[HTTP] /api/send 失败: {e}")


async def ws_capture(seconds=20):
    frames = []
    try:
        async with __import__("websockets").connect(WS) as ws:
            await ws.send(json.dumps({"Username": USER}))
            print(f"[WS]  已连接 {WS} 并注册 Username={USER}")
            threading.Thread(target=_feed, daemon=True).start()
            t0 = time.time()
            while time.time() - t0 < seconds:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=seconds)
                except asyncio.TimeoutError:
                    break
                try:
                    d = json.loads(msg)
                except Exception:
                    print(f"[WS]  非JSON: {str(msg)[:100]}")
                    continue
                frames.append(d)
                if d.get("Topic") == "human":
                    data = d.get("Data") or {}
                    print(
                        f"[WS]  Topic=human Key={data.get('Key')} "
                        f"IsFirst={data.get('IsFirst')} IsEnd={data.get('IsEnd')} "
                        f"Text={str(data.get('Text'))[:50]!r} "
                        f"Lips={len(data.get('Lips') or [])} "
                        f"Sentiment={data.get('Sentiment')} "
                        f"Action={data.get('Action')}"
                    )
                    print("       raw:", json.dumps(d, ensure_ascii=False)[:600])
                    if data.get("IsEnd") == 1:
                        break
                else:
                    print(f"[WS]  {json.dumps(d, ensure_ascii=False)[:140]}")
    except Exception as e:
        print(f"[WS]  连接/接收失败: {type(e).__name__}: {e}")
    return frames


async def main():
    print("=" * 64)
    ok_http = http_probe()
    frames = await ws_capture(seconds=60)
    try:
        r = requests.post(BASE + "/to-stop-talking", json={"username": USER}, timeout=5)
        print(f"[HTTP] POST /to-stop-talking -> {r.status_code} {r.text[:120]}")
    except Exception as e:
        print(f"[HTTP] /to-stop-talking 失败: {e}")
    print("=" * 64)
    human = [f for f in frames if f.get("Topic") == "human"]
    print(f"HTTP 探活: {ok_http} | WS 帧总数: {len(frames)} | human 帧: {len(human)}")
    if human:
        sample = (human[0].get("Data") or {})
        print("RESULT: VERIFY_OK —— 喂话已触发数字人驱动输出")
        print(f"        示例: Text={str(sample.get('Text'))[:40]!r} Sentiment={sample.get('Sentiment')} Action={sample.get('Action')}")
    else:
        print("RESULT: VERIFY_NO_HUMAN_FRAMES —— 未收到 human 帧（看上面日志定位）")


if __name__ == "__main__":
    asyncio.run(main())
