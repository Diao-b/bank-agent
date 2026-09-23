# 行为基线（tests/baseline）

第二阶段「合并式重构」的**行为零变化**证据源。重构前录制，每组改完重录并 diff，全一致即说明没改坏行为。

## 基线由三部分组成

| 文件 | 内容 | 是否硬门槛 | 说明 |
|---|---|---|---|
| `bank_agent.snapshot.json` | MySQL `bank_agent` 全量数据快照（16 表 / 4915 行） | — | 重录前 `restore` 回此刻状态，保证可复现 |
| `captures/baseline_readonly.json` | 只读接口形态 + 关键表 count + SSE 事件序列 | 硬 | token/时间戳已脱敏，重构前后应 **IDENTICAL** |
| `captures/baseline_tests/*.log` | 8 套回归脚本 stdout（权威行为证据） | 硬 | LLM 无关，直接打 mock_bank；重构前后应逐字一致 |

## 每套回归套件结果（golden）

| 套件 | 结果 | 依赖 |
|---|---|---|
| test_crossbank_settlement | 41/41 PASS | mock_bank 直连，无 LLM |
| test_insite_transfer | 27/27 PASS | mock_bank 直连，无 LLM |
| test_account_system | ALL GREEN | mock_bank 直连，无 LLM |
| test_card_apply | 66/66 PASS | mock_bank 直连，无 LLM |
| test_internal_transfer_flow | 26/26 PASS（环境偶发，见下） | **走 chat/agent 管线** |
| test_subscription_flow | 29/29 PASS | mock_bank 直连，无 LLM |
| test_realname_verified | 7/7 PASS | mock_bank 直连，无 LLM |
| tests/test_card | ALL GREEN | 纯单测，无 LLM |

> ⚠️ `test_internal_transfer_flow` 走 LangGraph 对话管线，当前环境 `bank.db` checkpoint 偶发报
> `attempt to write a readonly database`（SQLite 文件只读），对话可能落库失败 → 该套件**偶发**失败。
> **此为预存环境问题，不在解耦重构域内**（R~W 只动 mock_bank + utils，不动 agent 驱动 / checkpoint）。
> 该套件在「代码未改」时也会因环境时好时坏（同会话内 preR 失败/postR 通过均出现过），
> 以 rc 是否 0 + 通过数是否一致判定，环境失败不计为回归。

## 使用流程

### 1. 录制黄金基线（已执行一次，产物如上）
```bash
.venv/Scripts/python.exe tests/baseline/db_snapshot.py save
.venv/Scripts/python.exe tests/baseline/capture_readonly.py
.venv/Scripts/python.exe tests/baseline/run_regression.py
```

### 2. 每组改完后，重录并比对
```bash
# 改完代码、重启 8003 / 8178 服务后：
.venv/Scripts/python.exe tests/baseline/db_snapshot.py restore
.venv/Scripts/python.exe tests/baseline/capture_readonly.py
.venv/Scripts/python.exe tests/baseline/diff_captures.py \
    tests/baseline/captures/baseline_readonly.json \
    tests/baseline/captures/golden_readonly.json \
    --ignore sse_chat_stream,_meta,/api/operation_logs,/api/inbox,/api/admin/logs

.venv/Scripts/python.exe tests/baseline/run_regression.py   # 每套前自动 restore
# 比 tests/baseline/captures/baseline_tests/*.log 与 golden 的 *.log 是否逐字一致
```

### 3. 判定
- `diff_captures.py` 输出 `IDENTICAL` → 只读面行为零变化。
- 8 套回归 log 与 golden 逐字一致（internal 同为环境失败）→ 语义行为零变化。
- 任一不同 → 停下报告，不得自行「修」成一致。

## 注意
- 无 `mysqldump`/`mysql` 客户端，故 `db_snapshot.py` 用纯 Python 快照/恢复（SET FOREIGN_KEY_CHECKS=0）。
- 只读录制**不发出任何写接口**；转账/订阅变更类行为由回归脚本覆盖。
- SSE（chat/stream）属环境相关面，仅作记录，不计入硬门槛。
- `diff_captures.py --ignore` 支持「顶层键 + 路径子串」两种忽略。下列端点的响应被**捕获过程自身的
  SSE·agent 动作**污染（append-only 日志/通知表被追加行，自增 id 与时间戳无关、不可复现），与 SSE 同列
  为环境相关面，比对时按子串忽略：`/api/operation_logs`、`/api/inbox`、`/api/admin/logs`。
  除这 3 个端点外，其余只读端点在「同 DB 状态」下应逐字节一致。
