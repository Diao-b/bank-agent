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

## 判断标准（下次要归档什么）
- 一次性：跑完即失效，重跑会出错或造成破坏。
- 不在任何地方被 import / 调用（`grep -rn "<name>" --include=*.py --include=*.md --include=*.js .`）。
- 不被 README / CONTRIBUTING / 回归骨架（`tests/baseline/run_regression.py` 的 `SUITES`）引用。

## 仍在顶层的"活"脚本
`test_*.py`（验收）、`check_*.py`（只读体检）、`seed_*.py` / `provision_user.py`（数据准备）、
`cleanup_*.py` / `purge_*.py` / `repair_*.py`（运维，**默认 dry-run**）、`_op_test_auth.py`（测试共用签发器）、
`_*_check.js`（前端/UI 探针）。
