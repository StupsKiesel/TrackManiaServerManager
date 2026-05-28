from __future__ import annotations

import os
import sys


def main() -> int:
    if sys.platform != "linux":
        print("tmsm is Linux only.", file=sys.stderr)
        return 1

    from .paths import ensure_home
    from .tui.app import TmsmApp

    ensure_home()
    app = TmsmApp()
    app.run()
    if getattr(app, "restart_pending", False):
        # Replace the current process so the new code on disk is loaded.
        os.execv(sys.executable, [sys.executable, "-m", "tmsm"])
    return 0
