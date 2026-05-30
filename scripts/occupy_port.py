"""Occupy a TCP port for testing tmsm's MariaDB port auto-selection.

Usage:
    python scripts/occupy_port.py            # binds 0.0.0.0:3306
    python scripts/occupy_port.py 3307
    python scripts/occupy_port.py 3306 127.0.0.1

Keeps the socket open until you press Ctrl+C. Any tmsm install run while this
script is alive should detect the conflict and bump to the next free port.
"""
from __future__ import annotations

import socket
import sys


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3306
    host = sys.argv[2] if len(sys.argv) > 2 else "0.0.0.0"
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
    except OSError as e:
        print(f"bind {host}:{port} failed: {e}", file=sys.stderr)
        return 1
    s.listen(1)
    print(f"Listening on {host}:{port} — Ctrl+C to release.")
    try:
        while True:
            conn, addr = s.accept()
            print(f"  connection from {addr}")
            conn.close()
    except KeyboardInterrupt:
        print("\nReleasing port.")
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
