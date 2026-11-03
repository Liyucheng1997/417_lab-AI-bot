"""读取和保存 config.yaml。"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

# 打包成 exe 后：配置、数据、sop 放在 exe 旁边；界面文件在 PyInstaller 解出的资源目录里
FROZEN = getattr(sys, "frozen", False)
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent
RES_DIR = Path(getattr(sys, "_MEIPASS", ROOT))
CONFIG_PATH = ROOT / "config.yaml"
DATA_DIR = ROOT / "data"
MEMORY_PATH = DATA_DIR / "memory.md"

MODELS = {
    "claude-opus-5-5": "Opus 5.5",
    "claude-sonnet-5-5": "Sonnet 5.5",
    "claude-haiku-5-5": "Haiku 5.5",
}
POLICIES = {"smart": "高危需确认", "full": "完全放权", "strict": "每步确认"}
EFFORTS = ["low", "medium", "high", "xhigh", "max"]


@dataclass
class Config:
    model: str = "claude-opus-5-5"
    effort: str = "high"
    permission_policy: str = "smart"
    hotkey: str = "ctrl+alt+b"
    cwd: str = ""
    panel_width: int = 460
    load_user_settings: bool = True
    check_updates: bool = True
    extra_mcp_servers: dict = field(default_factory=dict)

    @property
    def work_dir(self) -> str:
        path = Path(os.path.expanduser(self.cwd)) if self.cwd else ROOT / "workspace"
        path.mkdir(parents=True, exist_ok=True)
        return str(path.resolve())

    @classmethod
    def load(cls) -> "Config":
        raw = {}
        if CONFIG_PATH.exists():
            raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        known = {f.name for f in fields(cls)}
        cfg = cls(**{k: v for k, v in raw.items() if k in known})
        if cfg.permission_policy not in POLICIES:
            cfg.permission_policy = "smart"
        if cfg.effort not in EFFORTS:
            cfg.effort = "high"
        return cfg

    def save_keys(self, **updates) -> None:
        """只改写指定的键，保留文件里的注释。"""
        for k, v in updates.items():
            setattr(self, k, v)
        if not CONFIG_PATH.exists():
            CONFIG_PATH.write_text(yaml.safe_dump(self.__dict__, allow_unicode=True), encoding="utf-8")
            return
        lines = CONFIG_PATH.read_text(encoding="utf-8").splitlines()
        for k, v in updates.items():
            for i, line in enumerate(lines):
                if line.startswith(f"{k}:"):
                    lines[i] = f"{k}: {v}"
                    break
            else:
                lines.append(f"{k}: {v}")
        CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_memory() -> str:
    DATA_DIR.mkdir(exist_ok=True)
    if not MEMORY_PATH.exists():
        MEMORY_PATH.write_text("# Bot 的长期记忆\n\n（这里记录主人的偏好、常用软件路径、常做的事等）\n", encoding="utf-8")
    return MEMORY_PATH.read_text(encoding="utf-8")
