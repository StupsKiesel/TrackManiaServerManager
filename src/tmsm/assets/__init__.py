"""Asset (PyPlanet addon) management for tmsm.

Two flavours:
  * `bundled`  — shipped inside tmsm at `src/tmsm/assets/pyplanet_apps/`.
                 Installed into  PYPLANET_SRC/pyplanet/apps/tmsm/<name>/  (symlink).
  * `community` — downloaded on demand from GitHub repos listed in catalog.json.
                  Installed into  PYPLANET_SRC/pyplanet/apps/contrib/<name>/ (symlink).

After install, the addon is added (commented-out) to every pool's
settings/apps.py inside a tmsm-managed block. Activating an addon for a pool
is just removing the leading `#` from that line.
"""
from .catalog import Addon, AddonSource, list_catalog, list_bundled
from .state import State, load_state, save_state
from .installer import (
    install_addon, remove_addon, update_addon, list_installed,
    reconcile_installed, ReconcileReport,
)

__all__ = [
    "Addon", "AddonSource",
    "list_catalog", "list_bundled",
    "State", "load_state", "save_state",
    "install_addon", "remove_addon", "update_addon", "list_installed",
    "reconcile_installed", "ReconcileReport",
]
