# 数字柜员「小微」· Fay 集成笔记

> 本文档是数字柜员集成的**唯一事实来源**：接口清单 / 启动命令 / 端口 / 配置项 / 定制清单。
> 集成方向：**本项目（bank-agent）为主、Fay 为渲染端**——Agent 与业务逻辑**零改动**，
> Fay 只负责「说话的皮」。Fay 与 Live2D 前端均以**独立进程 + 独立环境**运行，仅 **HTTP / WS** 通信。
>
> ★ 接口与端口**均已按源码逐一核对并在本机实跑验证**（喂话 → TTS → 口型/情绪帧 → Live2D 前端 全链路跑通）。
> 源码：Fay `D:\Users\Dwm\Desktop\agaent\fay\Fay`；Live2D 前端 `D:\Users\Dwm\Desktop\agaent\fay\mate-human`。

---

## 0. 状态总览（本机已全部实跑）

| 部分 | 状态 |
| --- | --- |
| 本项目侧：桥接层、SSE 联动、`.env`、状态接口、前端 iframe + 开关 | ✅ 已实现 + 自测（Mock Fay 契约核验） |
| Fay 运行（Python 3.12 venv + 依赖 + `system.conf` + 启动） | ✅ **已实跑**（端口 5000/10002/10003/5010 上线） |
| 喂话链路：`/transparent-pass` → TTS → WS 10002 口型/情绪帧 | ✅ **已实测**（`Key=audio` 带 `Text/Lips/Sentiment/Action`） |
| 闭环：bank-agent 对话 → 桥接层 → Fay 逐字念出 | ✅ **已实测**（LOOP_OK） |
| **Live2D 数字人前端**（mate-human，:5173） | ✅ **已接入并实跑**（对话页右侧栏常驻「诺亚」；口型实时驱动） |
| 全链路：bank 回复 → Fay → WS → Live2D 口型 | ✅ **已实测**（前端收到 `Key=audio` + `Lips` 20/40 段；嘴型参数更新 828 次） |
| 打断：`/to-stop-talking` | ✅ 已实测 |
| 降级：开关关 / Fay 关 | ✅ 开关关 = 零调用；Fay 关 = 面板显「数字柜员离线」，银行业务不受影响 |

---

## 1. 架构与目录关系

```
D:\Users\Dwm\Desktop\agaent\
├─ bank-agent\                          # ★ 本项目（为主）
│   ├─ run_server.py                    # :8178  SPA + Agent + 数字柜员状态接口 + /fay/ 代理
│   ├─ config.py                        # FAY_* 配置（默认值已按源码核对）
│   ├─ utils\digital_human.py           # ★ 桥接层（唯一与 Fay 通信的入口）
│   ├─ web\index.html                   # 数字柜员右侧栏 iframe + 用户菜单开关
│   └─ docs\fay_integration.md          # 本文档
└─ fay\
    ├─ Fay\                             # ★ Fay 核心（xszyou/Fay，MIT）+ .venv（Python 3.12）
    └─ mate-human\                      # ★ Live2D 数字人前端（gitee: garveyer/mate-human）
        └─ CubismSdkForWeb-5-r.4\Samples\TypeScript\Demo\   # 前端工程（★ 用其自带 dist/ 起服务）
```

**端口（源码核实 + 实跑）**

| 服务 | 端口 | 说明 |
| --- | --- | --- |
| MySQL | 3306 | 真值库 `bank_agent` |
| mock_bank | 8003 | 银行数据服务 |
| **bank-agent** | **8178** | SPA + Agent + 状态接口 + `/fay/` 代理 |
| **Fay HTTP（Flask）** | **5000** | 管理控制台 + HTTP API（同一端口） |
| **Live2D 前端** | **5173** | 数字人画面（mate-human，vite preview） |
| Fay 数字人驱动（WS） | 10002 | ★ 数字人前端连接处（按 `Username` 精确路由） |
| Fay UI 数据（WS） | 10003 | 控制台用 |
| Fay MCP 服务 | 5010 | faymcp 管理页 |
| Fay 远程音频（Socket） | 10001 | 远程音频通道 |

---

## 2. 启动流程（三条命令，已验证）

```bash
# ① bank-agent（8178）
cd D:\Users\Dwm\Desktop\agaent\bank-agent
.venv\Scripts\python.exe run_server.py

# ② Fay 核心（独立 venv，HTTP 5000 + WS 10002）
cd D:\Users\Dwm\Desktop\agaent\fay\Fay
.venv\Scripts\python.exe main.py start

# ③ Live2D 数字人前端（5173）
cd D:\Users\Dwm\Desktop\agaent\fay\mate-human\CubismSdkForWeb-5-r.4\Samples\TypeScript\Demo
node node_modules\vite\bin\vite.js preview --port 5173 --host 127.0.0.1
```

> Fay 重启前如需清理：删掉 `logs/*.log`（否则其 `__clear_logs()` 在本机可能因文件占用报错）。

### 2.1 Fay 首次部署（已完成，留档）

```bash
cd /d/Users/Dwm/Desktop/agaent/fay/Fay
# ① Python 3.12 独立 venv（★ 隔离，禁止装进 bank-agent 环境）
"C:/Users/Dwm/AppData/Local/Programs/Python/Python312/python.exe" -m venv .venv
./.venv/Scripts/python.exe -m ensurepip --upgrade          # venv 自带 pip 缺失时
# ② 装依赖（★ 见"依赖冲突处理"）
./.venv/Scripts/python.exe -m pip install -r _req_light2.txt
# ③ system.conf（由 system.conf.bak 生成）
#    gpt_api_key / gpt_base_url=https://api.deepseek.com/v1 / gpt_model_engine=deepseek-chat
#    start_mode=web ；fay_url=http://127.0.0.1:5000
#    tts_module=azure  ← 走 tts/ms_tts_sdk.py，用 edge_tts 免费 zh 女声「晓晓」
# ④ 启动
./.venv/Scripts/python.exe main.py start
```

**★ 依赖冲突处理（关键）**：`requirements.txt` 里 `langgraph`（经 `langgraph-sdk`）要求 `websockets>=14`，
而 Fay 的 `websockets~=10.4` 是写死的（代码用 legacy API）→ `ResolutionImpossible`；新版 `langchain` 也会拉 `langgraph`。
**解法**：安装时跳过 `langgraph / langchain / chromadb / sentence_transformers` 四行（`_req_light2.txt`）。
依据：`langgraph` 全仓**零 import**；`chromadb`/`sentence_transformers` 只在独立子服务用到；
`langchain_openai`/`langchain_core`（Fay 真正用的）保留即可。

**★ 本机 locale 修正（关键）**：本机 CRT locale 为 `Chinese (Simplified)_China.1252`（不含 CJK），
导致 `llm/nlp_cognitive_stream.py` 的 `strftime("%Y年%m月%d日 ...")` 抛 `UnicodeEncodeError`，
回复线程直接中断（表现为一直停在「思考中...」）。
**解法（不改 Fay 源码）**：venv 内放 `sitecustomize.py`（Python 启动自动加载）：
```python
import locale
try:
    locale.setlocale(locale.LC_CTYPE, "Chinese_China.936")
except Exception:
    pass
```
只改 `LC_CTYPE`（`getpreferredencoding()` 仍为 utf-8，不影响文件读写）。删除该文件即还原。

### 2.2 Live2D 前端来源与部署（已验证）

**来源**：Fay 官方飞书文档「数字人模型」→ 第 5 项 **live2d 网页数字人**（课程包 `fay-live2d-course.zip`），
课程讲义指明使用开源前端 **mate-human**：

```bash
cd /d/Users/Dwm/Desktop/agaent/fay
git clone --depth 1 https://gitee.com/garveyer/mate-human.git
```

**运行方式：用仓库自带的预构建 `dist/`**，不要跑 `npm run start`。
原因：`start`/`build` 都会先执行 `copy_resources.js`，其中的 `fs.rmSync` 被本机（WorkBuddy CLI 注入的）
safe-delete shim 拦截 → 脚本崩溃、vite 起不来。仓库已提交完整 `dist/`（含 `Core/`、`Resources/`、`assets/`、
以及 `127.0.0.1:10002` 的 Fay 客户端代码），故直接 `vite preview` 即可。

```
node node_modules\vite\bin\vite.js preview --port 5173 --host 127.0.0.1
```

**默认模型**：`src/lappdefine.ts::ModelDir = ['Haru']`（Live2D 官方样例模型，配自带「诺亚」前台背景图）。

---

## 3. ★ 接口契约（源码核实 + 实跑验证）

Flask 基址 `$FAY = http://127.0.0.1:5000`。桥接层 `utils/digital_human.py` 即按此实现。

| 动作 | 方法 | 路径 | 载荷 | 返回/效果 |
| --- | --- | --- | --- | --- |
| **喂话（逐字念出）** | POST | `$FAY/transparent-pass` | **form 字段 `data`** = JSON 字符串 `{"user": <str>, "text": <str>}` | `{"code":200,...}`；Fay 直接 TTS 播报该 text |
| **打断** | POST | `$FAY/to-stop-talking` | JSON `{"username": <str>}` | `{"status":"success","data":"interrupted"}` |
| **打断（本项目侧端点）** | POST | `:8178/api/digital-human/interrupt` | — | 桥接层 `stop()` → 转调上面的 Fay 接口；静默失败 |
| **探活** | GET | `$FAY/api/get-system-status` | 可选 `?username=` | `{"server":true,...}` |
| 管理控制台 | GET | `$FAY/` | — | Vue SPA（无 `verifier.json` 时免密） |
| 表情图 | GET | `$FAY/robot/<name>.gif` | — | Normal/Speaking/Thinking/Angry/Crying/Gentle/Listening |

> **喂话为什么用 `/transparent-pass` 而不是 `/api/send`？**
> `/api/send` 是「用户提问 → Fay 作答」：文本会被当成提问，Fay 再用自己的 LLM 答一遍（内容不同）。
> `/transparent-pass`（源码里的 `Interact('transparent_pass', 2, {...})` 透传播放）把 text **逐字**交给 TTS，
> 正是"虚拟人说**同一句话**"所需的通道（验收 #3）。两者差异已实测。

**★ 数字人驱动协议（WS 10002）——Live2D 前端消费的就是它**

外部前端连 `ws://127.0.0.1:10002`，先发 `{"Username":"<name>","Output":true}` 注册，随后收到：

```json
{"Topic":"human","Data":{"Key":"audio","Text":"转账已成功，收款方将在5分钟内到账。",
 "Value":"<wav路径>","HttpValue":"http://127.0.0.1:5000/audio/….wav","Time":2.76,
 "IsFirst":1,"IsEnd":0,
 "Lips":[{"Lip":"sil","Time":231},{"Lip":"nn","Time":33}, …],
 "Sentiment":2,
 "Action":{"code":"greeting.hello","behavior":"nod","affect":"smile","intensity":0.35,"priority":60,"matchedKeywords":["你好"],"sentimentHint":0.4}}}
```

- `Key=text` 帧只有 `Value`（字幕），**没有** `Lips`；`Key=audio` 帧才带 `Text/Lips/Sentiment/Action`
  **以及 `Time`（该段音频时长，秒）**。
- `Lips` = 口型数组（驱动 `ParamMouthOpenY`）；`Sentiment` = 情绪值；`Action.affect` = 表情风格（smile/serious/excited…）。
- **情绪/口型由 Fay → 前端这一跳给出，本项目桥接层不参与**（本项目只把文本喂进 `/transparent-pass`）。
- ★**本项目也旁听这条通道**（「与小微对话」模式）：Fay 按 `Username` **群发给所有同名客户端**，
  故 bank-agent 页面可另开一条 WS 当观察者，用 `Σ Time` 得到**真实语音时长**，据此决定何时重开麦克风
  （按文本估算实测偏差可达 40%）。地址由 `FAY_WS_URL` 配置、经 `GET /api/digital-human/status` 下发。

> ★★ **喂话必须"单次喂全文"**：实测一次性喂全文口播覆盖率 **95%**；
> 而按句切分再 `queue:true` 排队反而掉到 **35%**（`/transparent-pass` 的 queue 分支会合并/丢段）。
> 故长回复不要分块，改用下面的**口语化压缩**把文本变短。

**★ 喂话文本口语化（本项目侧，聊天区展示不受影响）**
`utils/digital_human.py::to_speakable()`：去 Markdown/emoji、`——` 转逗号、**表格与代码块丢弃**、
**连续列表只念前 N 项 + 「等」**、行内举例只念第一个。实测一条 466 字含 5 项列表的回复 → 口播 139 字（约 1/3）。
配置：`FAY_SPEAK_DIGEST`（总开关，默认 1）、`FAY_SPEAK_LIST_ITEMS`（默认 1；≤0 = 不折叠）。
口播时长估算 `estimate_speak_ms()` 会随 SSE `done` 帧以 `dh_speak_ms` 下发，作为**旁听不到帧时的兜底**。

> ★★ **`Username` 必须两端一致，否则前端"连上但永远收不到帧"。**
> Fay 的 WS 服务**按 username 精确过滤后定向推送**（`core/wsa_server.py::__producer_handler`：
> `target_clients = [c for c in clients if c.get("username") == username]`）。
> mate-human 前端在 `src/lappmodel.ts` 硬编码注册为 **`'User'`**，故本项目 `FAY_USERNAME=User`
> （`config.py` 默认值同此，并已写明原因）。

**桥接层已实现并自测**（`scripts/_dh_test.py`，Mock Fay 核验）：POST `/transparent-pass`
（`application/x-www-form-urlencoded`，`data={"user":…,"text":…}`）、POST `/to-stop-talking`、
GET `/api/get-system-status` 全命中。所有方法 **try/except 静默**，Fay 未启动时零异常。

---

## 4. `.env` 配置（本项目侧）

见 `.env.example` 末尾「数字柜员」。`FAY_API_URL` 留空 = 功能关闭（前端开关亦默认关）。

```dotenv
FAY_API_URL=http://127.0.0.1:5000      # Fay HTTP 基址（端口 5000，已核实）
FAY_FRONTEND_URL=http://127.0.0.1:5173 # ★ Live2D 前端地址（面板 iframe 直连此地址）
FAY_USERNAME=User                      # ★ 必须等于前端注册的 Username（mate-human 硬编码 'User'）
FAY_SPEAK_PATH=/transparent-pass       # 逐字念出（已核实并实测）
FAY_STOP_PATH=/to-stop-talking         # 打断（已核实）
FAY_HEALTH_PATH=/api/get-system-status # 探活（已核实）
FAY_API_TIMEOUT=2
```

**开关语义**：`FAY_API_URL` 为空 → 全 no-op；前端用户菜单「数字柜员 开/关」默认关，随请求以
`X-Digital-Human: 0/1` 头下发；两者同时满足才真正喂话。改 `.env` 后需**重启 8178**。

---

## 5. 已核实取值表

| # | 项 | 取值（源码核实 + 实跑） | 配置位 |
| --- | --- | --- | --- |
| 1 | 喂话（逐字） | `POST /transparent-pass`，form `data={"user","text"}` | `FAY_SPEAK_PATH` |
| 2 | 情绪 | 无入参；Fay 按文本自判 `Sentiment`/`Action`，经 WS 10002 下发 | 不配置 |
| 3 | 打断 | `POST /to-stop-talking`，JSON `{"username"}` | `FAY_STOP_PATH` |
| 4 | 探活 | `GET /api/get-system-status` | `FAY_HEALTH_PATH` |
| 5 | TTS | `system.conf` `tts_module=azure` → `edge_tts` 免费 zh 女声「晓晓」 | Fay 侧（§6-F2） |
| 6 | 端口 | HTTP **5000**；Live2D 前端 **5173**；WS 10002/10003；MCP 5010 | `FAY_API_URL` / `FAY_FRONTEND_URL` |
| 7 | 前端用户名 | `'User'`（`mate-human/src/lappmodel.ts` 硬编码） | `FAY_USERNAME` |
| 8 | 前端模型 | `Haru`（`mate-human/src/lappdefine.ts::ModelDir`） | 前端侧 |

---

## 6. 定制清单

> 本轮新增文件：Fay 内 `_req_light2.txt` / `system.conf` / `.venv/.../sitecustomize.py`；
> mate-human 使用其自带 `dist/`（**未改动其源码**）。

| # | 文件 / 位置 | 改动内容 | 目的 | 状态 |
| --- | --- | --- | --- | --- |
| F0 | Fay `system.conf` | 由 `system.conf.bak` 生成，填 DeepSeek LLM；`start_mode=web` | 让 Fay 能起、能答 | ✅ 已做 |
| F0b | Fay `.venv\...\sitecustomize.py` | 设 `LC_CTYPE=Chinese_China.936` | 修本机 locale 致 strftime 崩（§2.1） | ✅ 已做 |
| F0c | Fay `_req_light2.txt` | 剔除 `langgraph/langchain/chromadb/sentence_transformers` | 规避 websockets 冲突（§2.1） | ✅ 已做 |
| F2 | `system.conf` `tts_module` | 已设 `azure`（edge_tts 免费 zh 女声「晓晓」） | 银行柜员气质 | ✅ 已做（可再调语速） |
| F3 | Live2D 前端 | 接入 mate-human（:5173），默认模型 `Haru` | 会说话的数字人 | ✅ 已做 |
| F1 | 控制台「设置」/ `core/fay_core.py::can_auto_play` | 关闭**自动播报**，仅"被喂话才说话" | 防自说自话 | 待执行（可选） |
| F4 | — | 无需裁剪前端 UI（mate-human 已是纯渲染页） | — | ✅ 不需要 |
| F5 | — | 无需子路径代理（前端基路径为 `/`，面板 iframe 直连） | — | ✅ 不需要 |

### ★ Live2D 前端接入要点（已验证）

1. **面板 iframe 直连** `FAY_FRONTEND_URL`（跨源 iframe 本身可用，不需要 CORS）；
   `/fay/` 反向代理保留作兜底（可代理 Fay 控制台），但**不用于渲染 Live2D**——
   Vite 产物含大量**根绝对路径**（`/assets/...`、`/Core/...`），经子路径代理会 404。
2. **用户名对齐**（§3）：`FAY_USERNAME` = 前端注册名 = `User`。
3. **同源状态接口** `GET /api/digital-human/status` → `{enabled, ready, frontend_url}`，面板据此判断在线/离线。
   ★ **必须注册在 `/api/{path:path}` 兜底代理之前**，否则请求会被兜底转发给 mock_bank 并返回
   `404 {"detail":"Not Found"}`（本机已踩，已修）。
4. **浏览器自动播放策略**：前端有一次性「Enable Audio」门（需用户点一下页面），
   之后口型/语音正常（实测点击后嘴型参数更新 828 次）。属于浏览器策略，非缺陷。

---

## 7. 模型来源与许可

| 项 | 内容 |
| --- | --- |
| 前端工程 | **mate-human**（作者 郭泽斌），`https://gitee.com/garveyer/mate-human` |
| 来源文档 | Fay 官方飞书：`https://qqk9ntwbcit.feishu.cn/wiki/GHevwqxwfiX4hCk8yJCcoJ54nqg`（数字人模型总目录）→ `…/C5ORwSBGeiDibhkD4MdcPBHpnih`（live2d 网页数字人，附件 `fay-live2d-course.zip`） |
| 渲染库 | Live2D Cubism SDK for Web 5-r.4（`Core/live2dcubismcore.js`，Live2D Open Software License） |
| 默认模型 | `Haru`（Live2D 官方样例模型） |
| 放置路径 | `fay\mate-human\CubismSdkForWeb-5-r.4\Samples\TypeScript\Demo\` |

> Fay 本体 MIT；**Live2D Cubism SDK 与模型有独立许可**，对外分发/商用前请单独确认
> （模型规格见 Fay `docs/Live2D模型制作要求.md`）。
> 若需**职业感更强**的银行柜员形象，替换前端 `Resources/<Model>/` 并在 `lappdefine.ts::ModelDir` 指向它即可。

---

## 8. 本项目侧实现说明

- **桥接层 `utils/digital_human.py`**（唯一入口）：`speak(text,emotion)`→POST `/transparent-pass`；
  `stop()`→POST `/to-stop-talking`；`is_ready()`→GET `/api/get-system-status`；`proxy()`→`/fay/` 反代；
  `maybe_speak_event/maybe_speak_reply` 由 SSE `done.payload.reply` 触发；**全部静默失败**，喂话走后台线程。
- **触发点**：`run_server.py` — `_sse_response(dh_on=)` 出帧前触发；4 个对话端点读 `X-Digital-Human`
  → `dh_on` + 入口 `_dh_interrupt()`（打断上一句）。
- **`run_server.py` 新增**：`GET /api/digital-human/status`（★ 注册在 `/api/{path:path}` 兜底之前）、
  `/fay` + `/fay/{path}` 反向代理（HTML 注 `<base>`、改写根绝对路径、不可达 → 502 离线页）。
- **前端 `web/index.html`**：`DigitalHumanPanel`（右侧栏 240×320，iframe `src=FAY_FRONTEND_URL`，`allow=autoplay`）；
  关闭态 `return null`（零调用）；<1024px 隐藏；每 8s 问状态接口 → 离线显示「数字柜员离线」；
  用户菜单「数字柜员 开/关」= `localStorage.xb_digital_human`（默认关）。

---

## 9. 验收对照（本机实测）

| # | 验收项 | 结果 |
| --- | --- | --- |
| 1 | 三方独立启动正常 | ✅ bank-agent 8178 / Fay 5000+10002 / Live2D 5173 均上线 |
| 2 | 对话页 iframe 虚拟人常驻 | ✅ **实测**：右侧栏常驻 Live2D 人物（`_reports/dh_live2d.png`） |
| 3 | 联动闭环：发消息 → SSE → 虚拟人说**同一句** + 口型/情绪 | ✅ **实测**：`Key=audio Text=银行回复原文 Lips=54 Sentiment=2 Action=smile`；前端收到 20/40 段口型、嘴型参数更新 828 次 |
| 4 | 打断：新消息 → 停止播报 | ✅ `/to-stop-talking` 实测 `interrupted`；桥接层每轮入口调用 |
| 5 | 降级：Fay 杀掉 → 对话正常 + 「离线」；开关关 → 零调用 | ✅ 已实测 |
| 6 | 回归：SSE / 业务不受影响 | ✅ SSE 出 done 帧；realname 7/7；业务域零改动 |
| 7 | 前端可达 / 无 CORS | ✅ iframe 直连 5173；`/fay/` 代理保留（同源兜底） |
| 8 | 部署文档完整 | ✅ 本文档 + `fay_step1_checklist.md` |
| 9 | 前端构建校验 | ✅ `node scripts/_check_babel.js` → BABEL_OK |

---

## 10. 改动清单

**本项目侧（bank-agent）**

| 文件 | 类型 | 改动 |
| --- | --- | --- |
| `utils/digital_human.py` | 新增 | 桥接层（真实接口：transparent-pass / to-stop-talking / get-system-status） |
| `run_server.py` | 修改 | 桥接 import + `_dh_on/_dh_interrupt` + `_sse_response(dh_on)` + 4 端点头 + **状态接口** + `/fay` 代理 |
| `config.py` | 修改 | 新增 `FAY_*`（默认值按源码核实；`FAY_USERNAME` 默认 `User`） |
| `.env` / `.env.example` | 修改 | 新增「数字柜员」段 |
| `web/index.html` | 修改 | `DigitalHumanPanel` + 用户菜单开关 + 请求头 |
| `docs/fay_integration.md`、`docs/fay_step1_checklist.md` | 新增 | 文档 |
| `README.md` | 修改 | 数字柜员章节 |
| `scripts/_dh_test.py`、`scripts/_fay_verify.py`、`scripts/_fay_loop_test.py` | 新增 | 自测/验证脚本 |

**本项目外（不改 bank-agent 结构）**

- Fay 侧：`system.conf`（配置）、`_req_light2.txt`（依赖清单）、`.venv/.../sitecustomize.py`（locale 修正）。**Fay 源码未改动**。
- Live2D 前端：`fay/mate-human`（git clone，**源码未改动**，直接用其自带 `dist/`）。

> 业务域零改动：`agent/**`、`mock_bank/**`、`utils/` 除 `digital_human.py` 外、`sql/**`、`tests/**`。
> bank-agent **非 git 仓库**，无法 `git diff`，以上为逐文件清单。

---

## 11. 已知限制 & TODO

1. **面板有一次性「Enable Audio」门**（浏览器自动播放策略）——用户需在数字人区域点一下，之后口型/语音正常。
2. **情绪由 Fay 侧判定**（§3），本项目 `emotion` 不下发。
3. **`FAY_USERNAME` 必须与前端注册名一致**（§3）——换前端时务必同步，否则「连上但收不到帧」。
4. **`/api/digital-human/status` 的路由顺序**（§6.3）——新增 `/api/xxx` 直连路由时都要注册在兜底代理之前。
5. **embedding 404**（deepseek 无 `/v1/embeddings`）+ **uvicorn/websockets-10.4 不兼容**（MCP stdio）：
   均为**非致命**日志噪音，不影响喂话/TTS/驱动链路。
6. **Step 5 语音输入：已实现**（**不接 Fay**）—— 用**浏览器原生 Web Speech API**（纯前端、零 key、零依赖），
   点按切换、结果回填输入框不自动发送；详见 `README.md` §13。Fay 侧的 ASR 通道**未启用**（无需）。
7. 数字柜员**只说话**：不参与业务判断，全部 `try/except` 静默，Fay 生死不影响主流程。
