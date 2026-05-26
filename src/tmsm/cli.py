from __future__ import annotations

import sys


def main() -> int:
    if sys.platform != "linux":
        print("tmsm is Linux only.", file=sys.stderr)
        return 1

    from .paths import ensure_home
    from .tui.app import TmsmApp

    ensure_home()
    TmsmApp().run()
    return 0
