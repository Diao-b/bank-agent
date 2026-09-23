"""数字柜员「小微」形象配置（人物模型 + 背景）。

设计：
- 配置是**全局**的（全行共享同一个 Live2D 渲染端），存于 bank-agent/data/appearance.json。
- 仅管理员可写（run_server 里用 _admin_guard 拦截）；读对登录用户开放。
- 可用「人物」= mate-human dist/Resources 下含 *.model3.json 的子目录；
  可用「背景」= 该目录根下的 *.png（排除 desk_foreground/icon_gear 这类前景与图标）。
- 管理员可上传新背景图，落位到 dist/Resources，供 URL 参数 ?bg=<filename> 直接引用。

★ 路径约定：本项目服务的是 mate-human 的**预构建 dist/**，且 vite preview 静态直读磁盘，
  故改 dist/ 即刻生效；背景图也直接写进 dist/Resources，引用只用文件名（同 5173 源，免 CORS）。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger("appearance")

# bank-agent 根目录（本文件在 bank-agent/utils/ 下）
_ROOT = Path(__file__).resolve().parents[1]
_DATA_DIR = _ROOT / "data"
_APPEARANCE_FILE = _DATA_DIR / "appearance.json"

# mate-human 预构建资源目录（相对 bank-agent 根的上一级是 agaent/）
_MATE_RESOURCES = (
    _ROOT.parent
    / "fay"
    / "mate-human"
    / "CubismSdkForWeb-5-r.4"
    / "Samples"
    / "TypeScript"
    / "Demo"
    / "dist"
    / "Resources"
)

# 默认形象（与 lappdefine.ts 硬编码值保持一致；URL 不传参数时 Live2D 前端回退到这里）
DEFAULT_MODEL = "Haru"
DEFAULT_BACKGROUND = "nokia_reception_desk.png"

# 背景候选中排除：前景吧台与图标（不是"背景"语义）
_BG_EXCLUDE = {"desk_foreground.png", "icon_gear.png"}

# 文件名白名单：上传背景只允许 [字母数字._-] + .png，防止路径穿越
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_.\-]+\.png$")


def _ensure_dirs() -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001  数据目录不可写也不要崩主流程
        pass


def get_appearance() -> dict:
    """返回当前形象配置（含默认值兜底）。"""
    cfg = {"model": DEFAULT_MODEL, "background": DEFAULT_BACKGROUND}
    try:
        if _APPEARANCE_FILE.exists():
            raw = json.loads(_APPEARANCE_FILE.read_text(encoding="utf-8") or "{}")
            if isinstance(raw, dict):
                cfg.update({k: raw[k] for k in ("model", "background") if k in raw})
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取形象配置失败，回退默认：%s", exc)
    return cfg


def list_models() -> list[str]:
    """可用的数字人模型（人物）。"""
    try:
        if _MATE_RESOURCES.is_dir():
            models = sorted(
                d.name for d in _MATE_RESOURCES.iterdir()
                if d.is_dir() and any(d.glob("*.model3.json"))
            )
            if models:
                return models
    except Exception as exc:  # noqa: BLE001
        logger.warning("扫描模型目录失败：%s", exc)
    # 兜底：已知内置模型
    return ["Haru", "Hiyori", "Mao", "Mark", "Natori", "Rice", "Wanko"]


def list_backgrounds() -> list[str]:
    """可用的背景图（dist/Resources 根目录下的 *.png，排除前景/图标）。"""
    try:
        if _MATE_RESOURCES.is_dir():
            bgs = sorted(
                p.name for p in _MATE_RESOURCES.glob("*.png")
                if p.name not in _BG_EXCLUDE
            )
            if bgs:
                return bgs
    except Exception as exc:  # noqa: BLE001
        logger.warning("扫描背景目录失败：%s", exc)
    return [DEFAULT_BACKGROUND]


def set_appearance(model: str, background: str) -> dict:
    """写入形象配置；非法取值抛 ValueError。返回最新配置。"""
    models = list_models()
    backgrounds = list_backgrounds()
    if model not in models:
        raise ValueError(f"未知人物模型：{model}（可选：{', '.join(models)}）")
    if background not in backgrounds:
        raise ValueError(f"未知背景：{background}（可选：{', '.join(backgrounds)}）")
    _ensure_dirs()
    cfg = {"model": model, "background": background}
    _APPEARANCE_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def save_background_file(filename: str, content: bytes) -> str:
    """保存上传的背景图到 dist/Resources；返回落盘后的文件名。

    文件名经白名单校验 + 统一小写扩展名，杜绝路径穿越。
    """
    if not filename:
        raise ValueError("文件名不能为空")
    name = Path(filename).name  # 去掉任何目录成分
    if not _SAFE_FILENAME.match(name):
        # 规范化：非法字符替换为下划线，强制 .png
        stem = re.sub(r"[^A-Za-z0-9_.\-]", "_", Path(filename).stem)
        name = (stem or "bg") + ".png"
    if not _SAFE_FILENAME.match(name):
        raise ValueError("背景文件名不合法")
    _MATE_RESOURCES.mkdir(parents=True, exist_ok=True)
    dest = _MATE_RESOURCES / name
    dest.write_bytes(content)
    return name
