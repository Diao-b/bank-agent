"""
路由域：运行时状态面（P2.1）。

数字柜员状态 / 打断 · 数字人形象配置 · 后台调度器状态。
★ 全部必须注册在兜底代理 /api/{path:path} 之前，否则被转发给 mock_bank → 404。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from utils import scheduler
from utils import appearance as appearance_cfg
from utils.digital_human import digital_human
from pydantic import BaseModel
from fastapi.responses import JSONResponse

from routes._common import _admin_guard, _log_admin

router = APIRouter()


class _AppearanceUpdate(BaseModel):
    model: str

# --------------------------------------------------------------------------- #
#  数字柜员状态（同源查询）：供前端面板判断「启用 / 在线 / 前端地址」
#  ★ 必须注册在下方 /api/{path:path} 兜底代理【之前】：
#    否则请求会先被兜底代理转发给 mock_bank，由其返回 404 {"detail":"Not Found"}。
#  ★ 前端据此拿 FAY_FRONTEND_URL 并以 iframe 直连（Live2D 前端含根绝对资源路径，
#    经子路径代理不可靠，故此处只回报状态与地址，不做页面代理）。
# --------------------------------------------------------------------------- #
@router.get("/api/digital-human/status")
def digital_human_status():
    ap = appearance_cfg.get_appearance()
    return JSONResponse({
        "enabled": digital_human.enabled,
        "ready": digital_human.is_ready(),
        "frontend_url": digital_human.frontend_url,
        # 数字人驱动 WS：前端「与小微对话」旁听它，按真实音频时长决定何时重开麦克风
        "ws_url": digital_human.ws_url,
        "username": digital_human.username,
        # 当前生效的人物模型 + 背景（前端面板据此拼 ?model=&bg=，让全局形象设置真正落地）
        "model": ap["model"],
        "background": ap["background"],
    })


@router.post("/api/digital-human/interrupt")
def digital_human_interrupt():
    """让数字柜员立即停止播报（「与小微对话」模式里的「打断」）。

    桥接层 stop() 本身静默失败：Fay 未启动 / 异常也不报错，前端照常继续对话。
    同样必须注册在 /api/{path:path} 兜底代理之前。
    """
    digital_human.stop()
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
#  数字人形象配置（人物模型 + 背景）
#  ★ 必须注册在 /api/{path:path} 兜底代理【之前】，否则被转发给 mock_bank → 404。
#  读对登录用户开放；写（PUT / 上传背景）仅管理员。
# --------------------------------------------------------------------------- #
@router.get("/api/appearance")
def appearance_get(request: Request = None):
    ap = appearance_cfg.get_appearance()
    return JSONResponse({
        "ok": True,
        "model": ap["model"],
        "background": ap["background"],
        "models": appearance_cfg.list_models(),
        "backgrounds": appearance_cfg.list_backgrounds(),
        # Live2D 前端地址（5173）：前端预览 iframe 直接拼 ?model=&bg= 直连
        "frontend_url": digital_human.frontend_url,
    })


@router.put("/api/appearance")
def appearance_update(req: _AppearanceUpdate, request: Request = None):
    admin_uid = _admin_guard(request)
    try:
        cfg = appearance_cfg.set_appearance(req.model, req.background)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _log_admin(admin_uid, "appearance_update", "appearance", "global",
               detail={"model": cfg["model"], "background": cfg["background"]})
    return JSONResponse({
        "ok": True,
        "model": cfg["model"],
        "background": cfg["background"],
        "message": f"数字人形象已更新：人物={cfg['model']}，背景={cfg['background']}",
    })


@router.post("/api/appearance/background")
async def appearance_upload_background(request: Request):
    """上传新背景图（管理员）。落位到 mate-human dist/Resources，引用只用文件名。"""
    admin_uid = _admin_guard(request)
    form = await request.form()
    f = form.get("file") if form else None
    if not f or not getattr(f, "filename", None):
        raise HTTPException(status_code=400, detail="未收到文件")
    content = await f.read()
    if not content or len(content) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件为空或超出 8MB 上限")
    try:
        filename = appearance_cfg.save_background_file(f.filename, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _log_admin(admin_uid, "appearance_background_upload", "appearance", filename,
               detail={"bytes": len(content)})
    return JSONResponse({
        "ok": True,
        "filename": filename,
        "backgrounds": appearance_cfg.list_backgrounds(),
        "message": f"背景图已上传：{filename}",
    })


@router.get("/api/housekeeping")
def housekeeping_status():
    """后台调度器状态（P1.2，只读）：间隔 / 轮次 / 最近一轮的补扣·清算汇总。

    ★ 必须定义在兜底代理 /api/{path:path} 之前，否则会被转发给 mock_bank → 404。
    """
    return {"ok": True, **scheduler.status()}
