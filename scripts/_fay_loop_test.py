"""闭环验证：bank-agent 对话 → 桥接层 → Fay → 数字人驱动帧（跑完可删）。

链路：POST /api/chat/stream(带 X-Digital-Human:1) → bank-agent 生成回复 →
      桥接层 maybe_speak_event → POST Fay /api/send → Fay TTS → WS 10002 推 audio 帧(Text/Lips/Sentiment/Action)。
用 Fay 的 venv 运行（需 websockets + requests）。
"""
import asyncio
import json
import threading
import time

import requests
import websockets

BANK = "http://127.0.0.1:8178"
FAY_WS = "ws://127.0.0.1:10002"
USER = "xiaowei"


def login():
    import os
    tok = os.getenv("FAY_TEST_TOKEN")
    if tok:
        return tok
    r = requests.post(BANK + "/api/auth/login",
                      json={"username": "lige", "password": "lige123"}, timeout=10)
    return r.json()["token"]


def send_chat(token, out):
    r = requests.post(
        BANK + "/api/chat/stream",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                 "X-Digital-Human": "1"},
        json={"thread_id": "dh-loop-test", "message": "你好，请用一句话介绍你自己"},
        timeout=120, stream=True,
    )
    n = 0
    for line in r.iter_lines(decode_unicode=True):
        if line and line.startswith("data:"):
            n += 1
    out["sse"] = n
    print(f"[BANK] SSE done 帧数={n}")


async def main():
    token = login()
    print("[BANK] 登录成功")
    out = {}
    async with websockets.connect(FAY_WS) as ws:
        await ws.send(json.dumps({"Username": USER}))
        print(f"[FAY]  WS 10002 已注册 {USER}")
        threading.Thread(target=send_chat, args=(token, out), daemon=True).start()
        frames = []
        t0 = time.time()
        while time.time() - t0 < 90:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=90)
            except asyncio.TimeoutError:
                break
            d = json.loads(msg)
            frames.append(d)
            if d.get("Topic") == "human":
                data = d.get("Data") or {}
                k = data.get("Key")
                if k in ("audio", "log", "question", "text"):
                    print(f"[FAY]  Key={k} Text={str(data.get('Text'))[:34]!r} "
                          f"Lips={len(data.get('Lips') or [])} Sentiment={data.get('Sentiment')} "
                          f"Action={(data.get('Action') or {}).get('affect')}")
                if k == "audio" and data.get("IsEnd") == 1:
                    break
    human = [f for f in frames if f.get("Topic") == "human"]
    audio = [f for f in human if (f.get("Data") or {}).get("Key") == "audio"]
    print("=" * 56)
    print(f"SSE帧={out.get('sse')} | human帧={len(human)} | audio帧={len(audio)}")
    print("LOOP_OK ✅ —— bank-agent 回复已驱动 Fay 开口（含口型/情绪/动作）"
          if audio else "LOOP_NO_AUDIO ❌")


if __name__ == "__main__":
    asyncio.run(main())
