from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import tomli_w

from .base import Instance, Kind


@dataclass
class BotMeta:
    name: str
    source: str = ""                    # original zip path or URL (informational)
    run_script: str = "run.sh"          # entry script inside the bot root
    db_name: str = ""                   # empty if no DB was provisioned
    db_user: str = ""
    db_password: str = ""

    @staticmethod
    def load(root: Path) -> "BotMeta":
        with (root / "bot.toml").open("rb") as f:
            data = tomllib.load(f)
        return BotMeta(**data)

    def save(self, root: Path) -> None:
        with (root / "bot.toml").open("wb") as f:
            tomli_w.dump({k: v for k, v in self.__dict__.items() if v is not None}, f)


class DiscordBotInstance(Instance):
    kind = Kind.BOT

    def __init__(self, root: Path):
        self.root = root
        self.meta = BotMeta.load(root)
        self.name = self.meta.name

    def cwd(self) -> Path:
        return self.root

    def log_file(self) -> Path:
        return self.root / "logs" / "tmsm.log"

    def argv(self) -> list[str]:
        return ["bash", self.meta.run_script]

    def detail_rows(self) -> Iterable[tuple[str, str]]:
        st = self.status()
        yield ("Type", "Discord bot")
        yield ("Path", str(self.root))
        yield ("Source", self.meta.source or "—")
        yield ("Database", self.meta.db_name or "—")
        yield ("Status", st.status.value)
        if st.pid:
            yield ("PID", str(st.pid))
        if st.mem_mb is not None:
            yield ("Memory", f"{st.mem_mb:.1f} MB")

    def editable_files(self) -> list[tuple[str, Path]]:
        files: list[tuple[str, Path]] = []
        for name, label in [
            (".env", "Environment (.env)"),
            (".env.example", "Environment template (.env.example)"),
            ("requirements.txt", "Dependencies (requirements.txt)"),
        ]:
            p = self.root / name
            if p.is_file():
                files.append((label, p))
        return files
