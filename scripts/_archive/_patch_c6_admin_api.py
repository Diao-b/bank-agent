# -*- coding: utf-8 -*-
# C6 可运营后台：run_server.py 注入管理端写 API（全部 _admin_guard + admin_logs）
import io

p = 'run_server.py'
s = io.open(p, encoding='utf-8').read()

# 追加 import：rules / inbox / paypass（admin 写操作需要）
s = s.replace(
    "from agent.driver import MainDriver\nfrom utils import event_bus, mcp_client, security",
    "import rules  # C6：风控参数单源（system_config）\n"
    "from agent.driver import MainDriver\n"
    "from utils import event_bus, inbox, mcp_client, paypass, security")

anchor = ('# --------------------------------------------------------------------------- #\n'
          '# C5. /admin 独立页面（独立路由 + 独立布局；与用户端 SPA 分离）\n'
          '# --------------------------------------------------------------------------- #')
assert anchor in s, 'anchor missing'

new_block = '''# --------------------------------------------------------------------------- #
# C6. 可运营后台：管理端写操作 API
#  —— 三条铁律：
#     1) 每一次写操作都写 admin_logs（who / what / old→new / when）；
#     2) 可改范围严格限定在白名单（风控参数 / 用户登录与角色 / 产品目录 /
#        知识库同步 / 公告）—— 用户资产（余额 / 流水 / 持仓 / 订阅）
#        不设任何管理端修改接口，金融系统的管理员也不能动客户的钱；
#     3) 涉及 RAG 知识文档数字的风控参数，修改即打 pending_sync 标记，
#        知识文档人工确认更新后徽标才消失。
# --------------------------------------------------------------------------- #
import secrets as _secrets

from fastapi import Body

KNOWLEDGE_DIR = Path(config.BASE_DIR).parent / "docs" / "knowledge"


class _ConfigUpdate(BaseModel):
    value: str


class _UserLockReq(BaseModel):
    locked: bool


class _UserRoleReq(BaseModel):
    role: str


class _ProductUpdate(BaseModel):
    name: str | None = None
    term: str | None = None
    risk_level: int | None = None
    annual_rate: float | None = None
    min_amount: float | None = None
    status: str | None = None      # on | off


class _AnnouncementReq(BaseModel):
    title: str
    content: str


def _log_admin(admin_id: int, action: str, target_type: str = "",
               target_id: str = "", detail: dict | None = None,
               pending_sync: bool = False) -> int:
    """管理员操作留痕（admin_logs）。所有管理端写操作必经此口。"""
    db.execute(
        "INSERT INTO admin_logs (admin_id, action, target_type, target_id, detail, pending_sync) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (int(admin_id), action, target_type, str(target_id),
         json.dumps(detail or {}, ensure_ascii=False, default=str),
         1 if pending_sync else 0),
    )
    row = db.fetch_one("SELECT LAST_INSERT_ID() AS id")
    return int(row["id"])


def _pending_sync_rows():
    """未确认的 RAG 同步义务（知识库"待同步"徽标数据源）。"""
    return db.fetch_all(
        "SELECT l.id, l.action, l.target_id, l.detail, l.created_at, l.admin_id, "
        "       u.username AS admin_name "
        "FROM admin_logs l LEFT JOIN users u ON u.id = l.admin_id "
        "WHERE l.pending_sync = 1 AND l.sync_done_at IS NULL "
        "ORDER BY l.id DESC LIMIT 200"
    )


# ---- 1) 风控参数（system_config 单源） ------------------------------------- #
@app.get("/api/admin/config")
def admin_config_list(request: Request = None):
    admin_uid = _admin_guard(request)
    items = rules.all_rules()
    for it in items:
        it["pending_sync"] = bool(db.fetch_one(
            "SELECT id FROM admin_logs WHERE target_type='config' AND target_id=%s "
            "AND pending_sync=1 AND sync_done_at IS NULL LIMIT 1", (it["key"],)))
    return {"ok": True, "config": items, "pending": _pending_sync_rows(),
            "actor": admin_uid}


@app.put("/api/admin/config/{key}")
def admin_config_update(key: str, req: _ConfigUpdate, request: Request = None):
    admin_uid = _admin_guard(request)
    spec = rules.RULE_SPECS.get(key)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"未知参数：{key}")
    try:
        result = rules.set_rule_value(key, req.value, updated_by=admin_uid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未知参数：{key}")
    # ★ 同步义务强制化：涉及知识文档数字的参数 → pending_sync 红标
    _log_admin(admin_uid, "config_update", "config", key,
               detail={"old": str(result["old"]), "new": str(result["new"]),
                       "label": spec["label"]},
               pending_sync=bool(spec["knowledge"]))
    return {"ok": True, "key": key, "old": str(result["old"]), "new": str(result["new"]),
            "affects_knowledge": bool(spec["knowledge"]),
            "docs_ref": spec["docs"],
            "message": ("参数已生效。注意：" + spec["docs"] + " 知识文档需同步更新"
                        if spec["knowledge"] else "参数已生效。")}


@app.get("/api/admin/config/{key}/history")
def admin_config_history(key: str, limit: int = 50, request: Request = None):
    _admin_guard(request)
    rows = db.fetch_all(
        "SELECT l.id, l.detail, l.pending_sync, l.sync_done_at, l.created_at, "
        "       u.username AS admin_name "
        "FROM admin_logs l LEFT JOIN users u ON u.id = l.admin_id "
        "WHERE l.action='config_update' AND l.target_id=%s "
        "ORDER BY l.id DESC LIMIT %s", (key, min(int(limit), 200)))
    return {"ok": True, "history": rows}


@app.post("/api/admin/config/{key}/sync-confirm")
def admin_config_sync_confirm(key: str, request: Request = None):
    """知识文档人工更新完成后确认 → 清"待同步"标记（记录确认动作本身）。"""
    admin_uid = _admin_guard(request)
    rows = db.fetch_all(
        "SELECT id FROM admin_logs WHERE target_type='config' AND target_id=%s "
        "AND pending_sync=1 AND sync_done_at IS NULL", (key,))
    if not rows:
        return {"ok": True, "cleared": 0, "message": "该参数没有待同步标记"}
    db.execute(
        "UPDATE admin_logs SET sync_done_at=NOW() WHERE target_type='config' "
        "AND target_id=%s AND pending_sync=1 AND sync_done_at IS NULL", (key,))
    _log_admin(admin_uid, "knowledge_sync_confirm", "config", key,
               detail={"cleared": len(rows), "docs": rules.RULE_SPECS[key]["docs"]})
    return {"ok": True, "cleared": len(rows)}


# ---- 2) 用户管理（白名单：锁定 / 重置支付密码 / 角色。禁改资产） ---------- #
@app.post("/api/admin/users/{user_id}/lock")
def admin_user_lock(user_id: int, req: _UserLockReq, request: Request = None):
    admin_uid = _admin_guard(request)
    row = db.fetch_one("SELECT id, username, role FROM users WHERE id=%s", (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    if row["role"] == "admin" and req.locked:
        raise HTTPException(status_code=400, detail="不允许锁定管理员账号")
    db.execute("UPDATE users SET locked=%s WHERE id=%s",
               (1 if req.locked else 0, int(user_id)))
    _log_admin(admin_uid, "user_lock" if req.locked else "user_unlock", "user", user_id,
               detail={"username": row["username"]})
    return {"ok": True, "locked": bool(req.locked),
            "message": (f"已锁定 {row['username']}（登录将被拒绝）"
                        if req.locked else f"已解锁 {row['username']}")}


@app.post("/api/admin/users/{user_id}/reset-paypass")
def admin_user_reset_paypass(user_id: int, request: Request = None):
    """重置支付密码为随机 6 位码 —— 明文只通过站内信送达本人（不落库、不回显管理端）。"""
    admin_uid = _admin_guard(request)
    row = db.fetch_one("SELECT id, username FROM users WHERE id=%s", (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    new_pwd = "".join(_secrets.choice("0123456789") for _ in range(6))
    try:
        paypass.set_password(int(user_id), new_pwd)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"重置失败：{exc}")
    inbox.push_notification(
        int(user_id), "notice", "【小微银行】支付密码已重置",
        "管理员已应您的要求重置支付密码。新支付密码为：" + new_pwd +
        "（6 位数字）。请登录后立即在「安全中心」修改为你自己的密码，切勿告知他人。")
    # ★ 重置码明文绝不落审计 / 日志：只送达本人收件箱
    _log_admin(admin_uid, "user_reset_paypass", "user", user_id,
               detail={"username": row["username"], "notification": "sent"})
    return {"ok": True,
            "message": f"已重置 {row['username']} 的支付密码，新密码已通过站内信送达本人"}


@app.post("/api/admin/users/{user_id}/role")
def admin_user_role(user_id: int, req: _UserRoleReq, request: Request = None):
    admin_uid = _admin_guard(request)
    if req.role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="role 只能是 user 或 admin")
    row = db.fetch_one("SELECT id, username, role FROM users WHERE id=%s", (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    if row["id"] == admin_uid:
        raise HTTPException(status_code=400, detail="不能修改自己的角色")
    if row["role"] == req.role:
        return {"ok": True, "message": "角色未变化"}
    db.execute("UPDATE users SET role=%s WHERE id=%s", (req.role, int(user_id)))
    _log_admin(admin_uid, "user_role", "user", user_id,
               detail={"username": row["username"], "old": row["role"], "new": req.role})
    return {"ok": True, "message": f"已将 {row['username']} 的角色从 {row['role']} 调整为 {req.role}"}


# ---- 3) 理财产品管理（上架 / 下架 / 编辑；下架 → 用户端不可见不可买） ------ #
@app.get("/api/admin/products")
def admin_products(request: Request = None):
    _admin_guard(request)
    rows = db.fetch_all(
        "SELECT p.id, p.name, p.term, p.risk_level, p.annual_rate, p.min_amount, p.status, "
        "       (SELECT COUNT(*) FROM holdings h WHERE h.product_id=p.id) AS holders "
        "FROM products p ORDER BY p.id ASC")
    return {"ok": True, "products": rows}


@app.put("/api/admin/products/{product_id}")
def admin_product_update(product_id: int, req: _ProductUpdate, request: Request = None):
    admin_uid = _admin_guard(request)
    row = db.fetch_one("SELECT * FROM products WHERE id=%s", (product_id,))
    if not row:
        raise HTTPException(status_code=404, detail="产品不存在")
    changes: dict = {}
    if req.name is not None and req.name.strip():
        changes["name"] = req.name.strip()
    if req.term is not None and req.term.strip():
        changes["term"] = req.term.strip()
    if req.risk_level is not None:
        if req.risk_level not in (1, 2, 3):
            raise HTTPException(status_code=400, detail="风险等级只能为 1/2/3")
        changes["risk_level"] = int(req.risk_level)
    if req.annual_rate is not None:
        if not (0 <= float(req.annual_rate) <= 20):
            raise HTTPException(status_code=400, detail="年化收益率需在 0 ~ 20% 之间")
        changes["annual_rate"] = float(req.annual_rate)
    if req.min_amount is not None:
        if float(req.min_amount) < 0:
            raise HTTPException(status_code=400, detail="起购金额不能为负")
        changes["min_amount"] = float(req.min_amount)
    if req.status is not None:
        if req.status not in ("on", "off"):
            raise HTTPException(status_code=400, detail="status 只能是 on（上架）/ off（下架）")
        changes["status"] = req.status
    if not changes:
        return {"ok": True, "message": "没有需要修改的字段"}
    sets = ", ".join(f"{k}=%s" for k in changes)
    db.execute(f"UPDATE products SET {sets} WHERE id=%s",
               (*changes.values(), int(product_id)))
    _log_admin(admin_uid, "product_update", "product", product_id,
               detail={"name": row["name"],
                       "old": {k: str(row[k]) for k in changes},
                       "new": {k: str(v) for k, v in changes.items()}})
    return {"ok": True, "message": f"产品「{row['name']}」已更新", "changes": changes}


# ---- 4) 知识库（docs/knowledge：列表 / 查看 / 待同步 / 重建索引） ---------- #
@app.get("/api/admin/knowledge")
def admin_knowledge(request: Request = None):
    _admin_guard(request)
    docs = []
    if KNOWLEDGE_DIR.exists():
        for f in sorted(KNOWLEDGE_DIR.glob("*.md")):
            docs.append({
                "name": f.name, "size": f.stat().st_size,
                "updated_at": datetime.fromtimestamp(
                    f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
    return {"ok": True, "docs": docs, "pending": _pending_sync_rows(),
            "dir": str(KNOWLEDGE_DIR)}


@app.get("/api/admin/knowledge/{doc_name}")
def admin_knowledge_doc(doc_name: str, request: Request = None):
    _admin_guard(request)
    if "/" in doc_name or "\\\\" in doc_name or not doc_name.endswith(".md"):
        raise HTTPException(status_code=400, detail="非法文档名")
    f = KNOWLEDGE_DIR / doc_name
    if not f.exists():
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"ok": True, "name": doc_name, "content": f.read_text(encoding="utf-8")}


@app.post("/api/admin/knowledge/{doc_name}/sync-confirm")
def admin_knowledge_sync_confirm(doc_name: str, request: Request = None):
    """人工更新文档后点「已同步」→ 清掉指向该文档的 pending_sync 标记。"""
    admin_uid = _admin_guard(request)
    rows = db.fetch_all(
        "SELECT id, target_id, detail FROM admin_logs WHERE pending_sync=1 AND sync_done_at IS NULL")
    cleared = 0
    for r in rows:
        ref = ""
        try:
            ref = (json.loads(r["detail"] or "{}") or {}).get("docs", "")
        except Exception:  # noqa: BLE001
            ref = ""
        if ref == doc_name or r["target_id"] == doc_name:
            db.execute("UPDATE admin_logs SET sync_done_at=NOW() WHERE id=%s", (r["id"],))
            cleared += 1
    if cleared:
        _log_admin(admin_uid, "knowledge_sync_confirm", "knowledge", doc_name,
                   detail={"cleared": cleared})
    return {"ok": True, "cleared": cleared,
            "message": (f"已确认 {doc_name} 同步完成，清除 {cleared} 条待同步标记"
                        if cleared else f"{doc_name} 没有待同步标记")}


@app.post("/api/admin/knowledge/rebuild")
def admin_knowledge_rebuild(request: Request = None):
    """重建 RAG 知识索引（重扫 docs/knowledge/*.md → 刷新 README.md 索引表）。"""
    admin_uid = _admin_guard(request)
    try:
        import sys as _sys
        sys_root = str(Path(__file__).resolve().parent / "scripts")
        if sys_root not in _sys.path:
            _sys.path.insert(0, sys_root)
        from rebuild_knowledge_index import rebuild as _rebuild
        stats = _rebuild()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"重建失败：{exc}")
    _log_admin(admin_uid, "knowledge_rebuild", "knowledge", "index", detail=stats)
    return {"ok": True, "stats": stats,
            "message": f"知识索引已重建：{stats.get('docs', 0)} 篇文档"}


# ---- 5) 系统公告：全员收件箱群发（复用通知体系 type=notice） --------------- #
@app.post("/api/admin/announcement")
def admin_announcement(req: _AnnouncementReq, request: Request = None):
    admin_uid = _admin_guard(request)
    if not req.title.strip() or not req.content.strip():
        raise HTTPException(status_code=400, detail="标题与内容必填")
    users = db.fetch_all("SELECT id FROM users")
    for u in users:
        inbox.push_notification(int(u["id"]), "notice",
                                req.title.strip()[:100], req.content)
    _log_admin(admin_uid, "notice_send", "notice", "all",
               detail={"title": req.title.strip(), "recipients": len(users)})
    return {"ok": True, "recipients": len(users),
            "message": f"公告已发送至 {len(users)} 位用户的收件箱"}


# ---- 6) 操作审计：管理员操作日志（admin_logs 独立视图） -------------------- #
@app.get("/api/admin/admin-logs")
def admin_admin_logs(action: str = "", admin_id: int | None = None,
                     limit: int = 200, request: Request = None):
    _admin_guard(request)
    where, args = ["1=1"], []
    if action:
        where.append("l.action=%s"); args.append(action)
    if admin_id:
        where.append("l.admin_id=%s"); args.append(admin_id)
    args.append(min(int(limit), 500))
    rows = db.fetch_all(
        "SELECT l.*, u.username AS admin_name FROM admin_logs l "
        "LEFT JOIN users u ON u.id=l.admin_id WHERE " + " AND ".join(where) +
        " ORDER BY l.id DESC LIMIT %s", tuple(args))
    return {"ok": True, "logs": rows, "pending": _pending_sync_rows()}


'''
s = s.replace(anchor, new_block + anchor)
io.open(p, 'w', encoding='utf-8', newline='').write(s)
print('ADMIN_API_OK')
