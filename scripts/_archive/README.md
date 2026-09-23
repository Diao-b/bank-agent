# scripts/_archive —— 一次性脚本归档（P2.2）

这里放的是**历史上只用过一次、已经把事情做完**的脚本，保留只为可追溯（`git mv` 保留了历史）。
新代码**不应**引用它们；也不要在这里加新脚本。

## `_patch_*.py`（15 个）—— 组 A–T 重构期的一次性改写脚本
在"抽域/换实现"那几轮里，用来批量改写既有文件的临时脚本（如 `_patch_c6_admin_api.py` 生成管理端 API、
`_patch_cards_page.py` 改写卡片页面）。**改完即失效**：它们依赖当时的文件内容，现在再跑只会破坏代码。

## `_extract_*.py`（4 个）—— 一次性抽取脚本
把某个大文件里的路由/实现抽到独立模块的脚本（`_extract_transfer.py` / `_extract_subscription.py` /
`_extract_card_routes.py` / `_extract_wealth_routes.py`）。
与 P2.1 的 `run_server.py` 路由分域同性质：都是"跑一次就完成使命"的搬运脚本。

## `cleanup_test_data.py`（1 个，P2.3 归档）—— C6 验收期的数据清理
它是 C6 验收那轮的清理脚本，有两个问题：
1. 自己手写 `DELETE FROM users ...`，违反「删用户统一走 `profile_db.delete_user_cascade`」的铁律；
2. 与 `cleanup_test_users.py` 职责重叠 —— 两套删用户入口（CONTRIBUTING 的「唯一入口」原则）。

现在的归属：
- **测试账号清理** → `scripts/cleanup_test_users.py`（默认 dry-run）。
- **「C6 验收公告」污染** → 改由 `test_admin_ops.py` **收尾自清**（根治，不再需要事后清理）。
- **`scene='x'` 非法验证码** → 如今为 0 条，无需清理。

## 判断标准（下次要归档什么）
- 一次性：跑完即失效，重跑会出错或造成破坏。
- 不在任何地方被 import / 调用（`grep -rn "<name>" --include=*.py --include=*.md --include=*.js .`）。
- 不被 README / CONTRIBUTING / 回归骨架（`tests/baseline/run_regression.py` 的 `SUITES`）引用。

## 仍在顶层的"活"脚本
`test_*.py`（验收）、`check_*.py`（只读体检）、`seed_*.py` / `provision_user.py`（数据准备）、
`cleanup_*.py` / `purge_*.py` / `repair_*.py`（运维，**默认 dry-run**）、`_op_test_auth.py`（测试共用签发器）、
`_*_check.js`（前端/UI 探针）。
