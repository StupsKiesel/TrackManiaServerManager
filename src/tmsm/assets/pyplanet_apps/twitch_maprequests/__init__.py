"""Twitch Map Requests - viewers add TMX maps via Twitch chat.

Listens to a Twitch channel and lets allowed chatters run `!mr <tmx-id>`
to install a TMX map, queue it in the jukebox, and auto-remove it after
it has been played once.
"""
from .app import App_TwitchMapRequests  # noqa: F401
