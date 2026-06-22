"""about addon: a player-facing window with PyPlanet & tmsm version and project info.

Two group boxes (PyPlanet on top, TrackMania Server Manager below), each with a
logo/avatar and a short blurb plus the live version string. Opened from the hub
tile (all players) or the ``/about`` chat command. Requires tmsm_ui + tmsm_hub.
"""
from .app import AboutApp  # noqa: F401
