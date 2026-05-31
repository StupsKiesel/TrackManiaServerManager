"""tmsm_gamemodes - orchestrator for tmsm custom scripted game modes.

Modes are plug-ins that drive map rotation (and optionally votes) on top of
the dedicated server's existing script. The orchestrator owns the shared
services every mode needs: vote engine, TMX map picker + validators, JSON
state, operator UI and player-facing HUD/vote panels.
"""
from .app import TmsmGamemodesApp  # noqa: F401
