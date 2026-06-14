"""Race DisplayMessage GBX replacement widget.

Single-line replacement for the title-pack ``Race_DisplayMessage`` slot.
Renders one centered label of the form::

    {WinnerNick} has won the Race!

The actual winner resolution and hide-rule wiring (which game phases
the message is allowed in, which engine UI modules to suppress, etc.)
are configured by the widget engine / operator MODULES editor. This
addon only provides the replacement surface.
"""
from __future__ import annotations

from dataclasses import replace

from pyplanet.apps.tmsm.widget_engine.registry import (
    GbxReplacement,
    WidgetKind,
)
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase


_MANIALINK_ID = "tmsm_race_displaymessage_gbx_widget"


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class RaceDisplayMessageGbxWidgetApp(WidgetAppBase):
    name = "pyplanet.apps.tmsm.race_displaymessage_gbx_widget"
    label = "race_displaymessage_gbx_widget"

    WIDGET_KEY = "race_displaymessage_gbx_widget"
    WIDGET_NAME = "Race DisplayMessage"
    WIDGET_DESCRIPTION = (
        "GBX replacement for Race_DisplayMessage. Renders a single text line "
        "announcing the race winner."
    )
    WIDGET_ICON = "trophy"

    # Sensible default position: roughly where the engine's own
    # Race_DisplayMessage banner shows up (upper-middle of the screen).
    WIDGET_DEFAULT_X = -40.0
    WIDGET_DEFAULT_Y = 40.0
    WIDGET_DEFAULT_W = 80.0
    WIDGET_DEFAULT_H = 6.0

    # GBX replacement only — never render the regular persistent frame.
    WIDGET_KIND = WidgetKind.POPUP

    # Always visible regardless of phase (warmup, pre-race, in-race,
    # post-race, podium). `None` is the engine convention for "no phase
    # gate"; we set it explicitly so behaviour can't drift if the base
    # class default ever changes.
    WIDGET_VISIBLE_PHASES = None

    # No hide rules — the message must stay on screen in every situation
    # the engine drives it through.
    WIDGET_HIDE_NAMED: list[str] = []

    # Current winner nickname, set by the engine / driver code that
    # detects the race winner. Empty string -> render nothing.
    _winner_nick: str = ""

    async def set_winner(self, nick: str) -> None:
        """Update the announced winner and re-push the replacement to all players."""
        self._winner_nick = nick or ""
        if self.engine is not None:
            try:
                await self.engine.push_replacement(self.WIDGET_KEY)
            except Exception:
                pass

    def build_entry(self):
        entry = super().build_entry()
        return replace(
            entry,
            gbx_replace=GbxReplacement(
                manialink_id=_MANIALINK_ID,
                # Widget paints its own quad/label and ships no script —
                # engine chrome would add the standard background strip,
                # which we don't want for a transient banner message.
                chrome=False,
            ),
        )

    async def build_replacement_xml(self, login: str) -> str:  # noqa: ARG002
        nick = self._winner_nick.strip()
        if not nick:
            return ""

        resolved = self.engine.resolve(self.WIDGET_KEY, login) if self.engine else None
        x = float(getattr(resolved, "x", self.WIDGET_DEFAULT_X) or self.WIDGET_DEFAULT_X)
        y = float(getattr(resolved, "y", self.WIDGET_DEFAULT_Y) or self.WIDGET_DEFAULT_Y)
        w = float(getattr(resolved, "w", self.WIDGET_DEFAULT_W) or self.WIDGET_DEFAULT_W)
        h = float(getattr(resolved, "h", self.WIDGET_DEFAULT_H) or self.WIDGET_DEFAULT_H)

        text = f"{nick}$z$s$fff has won the Race!"
        text_size = max(1.4, min(h * 0.55, 3.0))

        return (
            f'<frame pos="{x:.2f} {y:.2f}" z-index="40">'
            f'<label pos="{w / 2.0:.2f} -{h / 2.0:.2f}" z-index="41" '
            f'halign="center" valign="center2" '
            f'textsize="{text_size:.2f}" textfont="GameFontBlack" '
            f'text="{_xml_escape(text)}" />'
            f'</frame>'
        )
