# 数字柜员 · 本机跑通实操清单（★ 已按实际跑通步骤整理）

> 配套：`docs/fay_integration.md`（唯一事实来源）。本清单是本机**实际跑通**的三段步骤（非推测）。
> 目标：Fay 起来 → 喂话链路通 → Live2D 前端显示并在对话页侧栏开口。

---

## 0. 接口（不用抓包，已源码核实 + 实测）

Flask 基址 `http://127.0.0.1:5000`：

| 动作 | 调用 |
| --- | --- |
| **喂话（逐字念出）** | `POST http://127.0.0.1:5000/transparent-pass`，**form** `data={"user":"User","text":"你好"}` |
| **打断** | `POST http://127.0.0.1:5000/to-stop-talking`，**JSON** `{"username":"User"}` |
| **探活** | `GET  http://127.0.0.1:5000/api/get-system-status` |

> 别用 `/api/send`：那是"用户提问→Fay 自己作答"，会二次生成，不是"同一句话"。
> `user`/`username` 必须等于**前端注册的 Username**（mate-human 硬编码 `'User'`），否则前端收不到帧。

---

## 1. Fay 环境与依赖（★ 已跑通）

```bash
cd /d/Users/Dwm/Desktop/agaent/fay/Fay

# Python 3.12 独立 venv
"C:/Users/Dwm/AppData/Local/Programs/Python/Python312/python.exe" -m venv .venv
./.venv/Scripts/python.exe -m ensurepip --upgrade          # venv 无 pip 时

# 装依赖：★ 用剔除冲突项的清单（见下）
./.venv/Scripts/python.exe -m pip install -r _req_light2.txt
```

**为什么用 `_req_light2.txt`（= requirements.txt 去掉 4 行）**：
`langgraph`（经 `langgraph-sdk`）与 Fay 写死的 `websockets~=10.4` 冲突；新版 `langchain` 又拉 `langgraph`；
`chromadb`/`sentence_transformers` 只在独立子服务用。故剔除：`langgraph / langchain / chromadb / sentence_transformers`。

```bash
grep -viE "^(langgraph|chromadb|sentence_transformers|langchain)$" requirements.txt > _req_light2.txt
```

---

## 2. 配置 `system.conf`

```bash
cp system.conf.bak system.conf
```
在 `[key]` 段填（Fay 走 OpenAI 兼容协议，可复用 bank-agent 的 DeepSeek key）：
```ini
gpt_api_key=<你的 key>
gpt_base_url=https://api.deepseek.com/v1
gpt_model_engine=deepseek-chat
start_mode=web
fay_url=http://127.0.0.1:5000
tts_module=azure          # ★ 走 tts/ms_tts_sdk.py → edge_tts 免费 zh 女声「晓晓」，无需 key
```

**★ locale 修正（本机必做）**：本机 CRT locale 为 cp1252，会让 Fay 的 `strftime("...年...")` 崩溃。
在 venv 放 `sitecustomize.py`（Python 自动加载，不改 Fay 源码）：
```python
import locale
try:
    locale.setlocale(locale.LC_CTYPE, "Chinese_China.936")
except Exception:
    pass
```

---

## 3. 启动 Fay

```bash
./.venv/Scripts/python.exe main.py start
```
上线端口：HTTP **5000**、WS **10002/10003**、MCP **5010**。
> 重启前若 `__clear_logs()` 报错：先删 `logs/*.log`（本机可能因文件占用失败）。

---

## 4. Live2D 数字人前端（★ 已跑通）

**来源**：Fay 官方飞书「数字人模型」→ 第 5 项 live2d 网页数字人（课程包）→ 讲义指向开源前端 **mate-human**。

```bash
cd /d/Users/Dwm/Desktop/agaent/fay
git clone --depth 1 https://gitee.com/garveyer/mate-human.git
cd mate-human/CubismSdkForWeb-5-r.4/Samples/TypeScript/Demo

# npm install（首次约 1 分钟，244 个包）
npm install

# ★ 直接起服务：用仓库自带的预构建 dist/
node node_modules/vite/bin/vite.js preview --port 5173 --host 127.0.0.1
```

**★ 不要跑 `npm run start` / `npm run build`**：它们会先执行 `copy_resources.js`，
其中的 `fs.rmSync` 被本机（WorkBuddy CLI 注入的）safe-delete shim 拦截 → 脚本崩溃、vite 起不来。
仓库已提交完整 `dist/`（含 `Core/`、`Resources/`、`assets/`、以及连 `127.0.0.1:10002` 的 Fay 客户端代码），
所以 `vite preview` 就够。验证：

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5173/
# 200
```

> 默认模型 `Haru`（`src/lappdefine.ts::ModelDir`）。

---

## 5. 本项目 `.env`

```dotenv
FAY_API_URL=http://127.0.0.1:5000
FAY_FRONTEND_URL=http://127.0.0.1:5173   # ★ Live2D 前端地址
FAY_USERNAME=User                        # ★ 必须等于前端注册的 Username（mate-human 硬编码 'User'）
FAY_SPEAK_PATH=/transparent-pass
FAY_STOP_PATH=/to-stop-talking
FAY_HEALTH_PATH=/api/get-system-status
FAY_API_TIMEOUT=2
```
改完**重启 8178**。验证状态接口：
```bash
curl -s http://127.0.0.1:8178/api/digital-human/status
# {"enabled":true,"ready":true,"frontend_url":"http://127.0.0.1:5173"}
```

---

## 6. 联动 + 闭环验证

1. 起 bank-agent：`cd bank-agent && .venv/Scripts/python.exe run_server.py`
2. 浏览器 `http://127.0.0.1:8178` → 登录 → 对话页 → 右上「数字柜员 开」。
3. （首次）在数字人区域**点一下**，过掉浏览器「Enable Audio」门。
4. 发消息 → 本项目 SSE `done` → 桥接层把该句 POST 到 Fay `/transparent-pass` → 虚拟人**逐字念出**（带口型/表情）。

**自动闭环验证脚本**（用 Fay venv 跑）：
```bash
cd bank-agent
FAY_TEST_TOKEN=<bank token> ../fay/Fay/.venv/Scripts/python.exe scripts/_fay_loop_test.py
# 期望：LOOP_OK ✅（Key=audio 的 Text 即 bank-agent 回复原文，含 Lips/Sentiment/Action）
```

**前端侧确认**（浏览器 F12 控制台）：
```
[FayClient] Connected to Fay server
[LAppModel] ✓ Fay WebSocket连接成功
[FayClient] Received message with Lips data: 40
```

---

## 7. 常见坑

| 症状 | 处理 |
| --- | --- |
| `ResolutionImpossible`（websockets/langgraph） | 用 `_req_light2.txt`（剔除 langgraph/langchain/chromadb/sentence_transformers） |
| Fay 回复一直停在「思考中...」 | 本机 locale 问题 → 放 `sitecustomize.py`（§2） |
| 启动即退出、日志报 safe-delete/删日志失败 | 先删 `logs/*.log` |
| 虚拟人念的是「我是 Feifei」而非银行回复 | 用 `/transparent-pass`（不是 `/api/send`） |
| `npm run start` 崩溃（copy_resources.js / safe-delete） | 跳过该脚本，直接 `vite preview`（§4） |
| **前端 WS 已连接但完全没反应** | ★ `FAY_USERNAME` 必须 = 前端注册名（`User`）；Fay 按 username 精确路由 |
| 侧栏「数字柜员离线」 | ① `FAY_FRONTEND_URL` 是否填 ② 5173 是否在跑 ③ Ctrl+F5 硬刷新 ④ 重启 8178 后看 `/api/digital-human/status` |
| `/api/digital-human/status` 返回 404 | ★ 路由顺序：必须注册在 `/api/{path:path}` 兜底代理**之前** |
| 数字人不播放声音 | 浏览器自动播放策略 → 在数字人区域点一下（一次性） |
| 非致命日志：embedding 404 / uvicorn 与 websockets-10.4 不兼容 | 忽略，不影响链路 |

---

## 8. 完成后

把 §6 闭环脚本输出发我即可；如需我按真实返回微调桥接层（只动集成层）。
