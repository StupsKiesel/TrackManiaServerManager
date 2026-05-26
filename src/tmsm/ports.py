"""Allocate non-conflicting ports for new instances."""
from __future__ import annotations

import socket
import tomllib

from . import paths


def _used_ports() -> set[int]:
    used: set[int] = set()
    if not paths.SERVERS_DIR.exists():
        return used
    for f in paths.SERVERS_DIR.glob("*/instance.toml"):
        try:
            with f.open("rb") as fh:
                data = tomllib.load(fh)
            for k in ("game_port", "xmlrpc_port"):
                if k in data:
                    used.add(int(data[k]))
        except Exception:
            continue
    return used


def _free_on_loopback(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _next(start: int, used: set[int]) -> int:
    p = start
    while p in used or not _free_on_loopback(p):
        p += 1
    used.add(p)
    return p


def allocate_server_ports() -> tuple[int, int]:
    used = _used_ports()
    game = _next(2350, used)
    xmlrpc = _next(max(5000, game + 1), used)
    return game, xmlrpc
