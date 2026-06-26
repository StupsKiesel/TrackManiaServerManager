"""Race BigMessage GBX replacement widget.

Large centered announcement that replaces the title-pack
``Race_BigMessage`` slot. Renders a single bold banner of the form::

    {WinnerNick} has won the game!

Per-client localisation
------------------------
GBX replacement widgets are server-wide: the engine builds one XML body
and broadcasts it to every player. We therefore cannot pick a language
per player on the Python side. Instead the broadcast manialink ships a
small ManiaScript that reads the *local* client language
(``CMlScript.LocalUser.Language``) and rewrites the banner label into
that player's own language. A German client sees the German text, an
English client the English text — from the very same broadcast.

The winner is detected automatically from the most recent
``trackmania:scores`` ranking and announced on ``podium_start``. External
driver code can also call :meth:`set_winner` directly.
"""
from __future__ import annotations

import logging
from dataclasses import replace

from pyplanet.apps.tmsm.widget_engine.registry import (
    GbxReplacement,
    Phase,
    WidgetKind,
)
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase

logger = logging.getLogger(__name__)


_MANIALINK_ID = "tmsm_race_bigmessage_gbx_widget"
_LABEL_ID = "race_bigmessage_text"

# Default banner text per game-language prefix (first two characters of
# ``LocalUser.Language``, e.g. "de", "en"). ``{nick}`` marks where the
# winner nickname is inserted. English is the fallback for any language
# not listed here.
_TRANSLATIONS: dict[str, str] = {
    "en": "{nick} has won the game!",
    "de": "{nick} hat das Spiel gewonnen!",
    "fr": "{nick} a remporté la partie !",
    "es": "{nick} ha ganado la partida!",
    "it": "{nick} ha vinto la partita!",
    "pt": "{nick} venceu o jogo!",
    "nl": "{nick} heeft het spel gewonnen!",
    "pl": "{nick} wygrał grę!",
    "ru": "{nick} победил в игре!",
    "cz": "{nick} vyhrál hru!",
    "sk": "{nick} vyhral hru!",
    "tr": "{nick} oyunu kazandı!",
}


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _ms_string_literal(value: str) -> str:
    """Escape a Python string so it is a safe ManiaScript string literal
    embedded inside a ``<script><!-- ... --></script>`` block."""
    out = value.replace("\\", "\\\\").replace('"', '\\"')
    # Never let the nickname close the surrounding XML comment early.
    out = out.replace("-->", "--")
    # Collapse any stray newlines/control chars onto a single line.
    out = out.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return out


class RaceBigMessageGbxWidgetApp(WidgetAppBase):
    name = "pyplanet.apps.tmsm.race_bigmessage_gbx_widget"
    label = "race_bigmessage_gbx_widget"

    WIDGET_KEY = "race_bigmessage_gbx_widget"
    WIDGET_NAME = "Race BigMessage"
    WIDGET_DESCRIPTION = (
        "GBX replacement for Race_BigMessage. Announces the game winner "
        "with a large banner, translated into each client's own language."
    )
    WIDGET_ICON = "bullhorn"

    # Centered, upper-middle of the screen, where the title-pack big
    # message banner normally appears.
    WIDGET_DEFAULT_X = -50.0
    WIDGET_DEFAULT_Y = 36.0
    WIDGET_DEFAULT_W = 100.0
    WIDGET_DEFAULT_H = 12.0

    # GBX replacement only — never render the regular persistent frame.
    WIDGET_KIND = WidgetKind.POPUP

    # Only ever shown during the podium phase (winner announcement).
    WIDGET_VISIBLE_PHASES = (Phase.IN_PODIUM,)
    WIDGET_HIDE_NAMED: list[str] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Winner nickname currently announced. Empty -> render nothing.
        self._winner_nick: str = ""
        # Most recent ranking observed from `trackmania:scores`, used to
        # resolve the winner at podium time.
        self._latest_scores: list[dict] = []

    # ---- lifecycle -----------------------------------------------------

    async def on_start(self) -> None:
        await super().on_start()
        for signal, handler in (
            ("trackmania:scores", self._on_scores),
            ("maniaplanet:podium_start", self._on_podium_start),
            ("maniaplanet:map_start", self._on_map_reset),
            ("maniaplanet:map_begin", self._on_map_reset),
        ):
            try:
                self.context.signals.listen(signal, handler)
            except Exception:
                pass

    # ---- winner tracking ----------------------------------------------

    async def _on_scores(self, section=None, players=None, **kwargs) -> None:  # noqa: ARG002
        if players:
            self._latest_scores = [p for p in players if isinstance(p, dict)]

    async def _on_podium_start(self, **kwargs) -> None:  # noqa: ARG002
        nick = self._winner_from_scores()
        if nick:
            await self.set_winner(nick)

    async def _on_map_reset(self, **kwargs) -> None:  # noqa: ARG002
        if self._winner_nick or self._latest_scores:
            self._latest_scores = []
            await self.set_winner("")

    def _winner_from_scores(self) -> str:
        """Resolve the rank-1 nickname from the latest scores ranking.

        The mode-provided `rank` field is authoritative when present;
        otherwise the first entry (modes emit scores already ordered) is
        used as a fallback.
        """
        first_nick = ""
        for item in self._latest_scores:
            player = item.get("player")
            nick = (
                str(getattr(player, "nickname", "") or "")
                or str(item.get("nickname") or "")
                or str(getattr(player, "login", "") or "")
                or str(item.get("login") or "")
            )
            if not nick:
                continue
            try:
                rank = int(item.get("rank") or 0)
            except (TypeError, ValueError):
                rank = 0
            if rank == 1:
                return nick
            if not first_nick:
                first_nick = nick
        return first_nick

    async def set_winner(self, nick: str) -> None:
        """Update the announced winner and re-push the replacement."""
        self._winner_nick = (nick or "").strip()
        if self.engine is not None:
            try:
                await self.engine.push_replacement(self.WIDGET_KEY)
            except Exception:
                logger.exception(
                    "race_bigmessage_gbx_widget: push_replacement failed",
                )

    # ---- registration --------------------------------------------------

    def build_entry(self):
        entry = super().build_entry()
        return replace(
            entry,
            gbx_replace=GbxReplacement(
                manialink_id=_MANIALINK_ID,
                # Widget ships its own top-level <script> (client-side
                # localisation); the engine chrome would nest it inside a
                # frame and ManiaScript would silently drop it.
                chrome=False,
            ),
        )

    # ---- rendering -----------------------------------------------------

    async def build_replacement_xml(self, login: str) -> str:  # noqa: ARG002
        nick = self._winner_nick.strip()
        if not nick:
            return ""

        resolved = self.engine.resolve(self.WIDGET_KEY, login) if self.engine else None
        x = float(getattr(resolved, "x", self.WIDGET_DEFAULT_X) or self.WIDGET_DEFAULT_X)
        y = float(getattr(resolved, "y", self.WIDGET_DEFAULT_Y) or self.WIDGET_DEFAULT_Y)
        w = float(getattr(resolved, "w", self.WIDGET_DEFAULT_W) or self.WIDGET_DEFAULT_W)
        h = float(getattr(resolved, "h", self.WIDGET_DEFAULT_H) or self.WIDGET_DEFAULT_H)

        text_size = max(2.0, min(h * 0.65, 5.0))

        # Static fallback (English): if the client-side script ever fails
        # to run, the English banner still shows.
        fallback = _TRANSLATIONS["en"].replace("{nick}", nick) + "$z$s$fff"

        chrome = self._chrome_quads(resolved, w, h)

        frame = (
            f'<frame pos="{x:.2f} {y:.2f}" z-index="40">'
            f'{chrome}'
            f'<label id="{_LABEL_ID}" pos="{w / 2.0:.2f} -{h / 2.0:.2f}" z-index="41" '
            f'halign="center" valign="center2" '
            f'textsize="{text_size:.2f}" textfont="GameFontBlack" '
            f'text="{_xml_escape(fallback)}" />'
            f'</frame>'
        )

        return frame + self._build_localized_script(nick)

    @staticmethod
    def _chrome_quads(resolved, w: float, h: float) -> str:
        """Reproduce the widget-engine chrome (background quad + accent
        strip) inside our own body.

        The engine's ``_inject_replacement_chrome`` cannot be used here:
        it appends its own ``<script>`` (slide animation), and a manialink
        may only contain one ``<script>`` — which this widget already
        spends on per-client localisation. So we paint the same bg/strip
        ourselves, honouring the resolved row so colour overrides from the
        engine UI still apply.
        """
        bg = str(getattr(resolved, "bg_color", "") or "40404080")
        strip_color = str(getattr(resolved, "strip_color", "") or "ffae00")
        strip_enabled = bool(getattr(resolved, "strip_enabled", True))
        strip_edge = str(getattr(resolved, "strip_edge", "") or "")
        try:
            strip_t = float(getattr(resolved, "strip_thickness", 1.0) or 1.0)
        except (TypeError, ValueError):
            strip_t = 1.0
        # Default the strip to the left edge when enabled but unresolved
        # (first push before the resolver has a row).
        if strip_enabled and not strip_edge:
            strip_edge = "left"

        bg_quad = (
            f'<quad pos="0 0" z-index="1" size="{w:.2f} {h:.2f}" '
            f'bgcolor="{bg}" halign="left" valign="top"/>'
        )

        strip_quad = ""
        if strip_enabled and strip_edge == "left":
            strip_quad = (
                f'<quad pos="-{strip_t:.2f} 0" z-index="2" size="{strip_t:.2f} {h:.2f}" '
                f'bgcolor="{strip_color}" halign="left" valign="top"/>'
            )
        elif strip_enabled and strip_edge == "right":
            strip_quad = (
                f'<quad pos="{w:.2f} 0" z-index="2" size="{strip_t:.2f} {h:.2f}" '
                f'bgcolor="{strip_color}" halign="left" valign="top"/>'
            )
        elif strip_enabled and strip_edge == "top":
            strip_quad = (
                f'<quad pos="0 {strip_t:.2f}" z-index="2" size="{w:.2f} {strip_t:.2f}" '
                f'bgcolor="{strip_color}" halign="left" valign="top"/>'
            )
        elif strip_enabled and strip_edge == "bottom":
            strip_quad = (
                f'<quad pos="0 -{h:.2f}" z-index="2" size="{w:.2f} {strip_t:.2f}" '
                f'bgcolor="{strip_color}" halign="left" valign="top"/>'
            )

        return bg_quad + strip_quad

    @staticmethod
    def _ms_concat_expr(template: str) -> str:
        """Turn a ``{nick}`` template into a ManiaScript expression that
        concatenates escaped literal parts around the ``Nick`` variable,
        e.g. ``"" ^ Nick ^ " has won the game!"``."""
        parts = template.split("{nick}")
        pieces = [f'"{_ms_string_literal(parts[0])}"']
        for part in parts[1:]:
            pieces.append("Nick")
            pieces.append(f'"{_ms_string_literal(part)}"')
        return " ^ ".join(pieces)

    def _build_localized_script(self, nick: str) -> str:
        """ManiaScript that rewrites the banner into the local client's
        language, read from ``LocalUser.Language``."""
        nick_ms = _ms_string_literal(nick)

        # Build the if/else chain over the language prefix. English is the
        # default assignment; every other entry overrides it on a match.
        lines: list[str] = []
        first = True
        for lang, template in _TRANSLATIONS.items():
            if lang == "en":
                continue
            branch = "if" if first else "else if"
            lines.append(
                f'  {branch} (Lang == "{lang}") '
                f'Msg = {self._ms_concat_expr(template)};\n'
            )
            first = False
        branches = "".join(lines)

        en_expr = self._ms_concat_expr(_TRANSLATIONS["en"])

        script = (
            '<script><!--\n'
            '#Include "TextLib" as TL\n'
            'main() {\n'
            f'  declare Text Nick = "{nick_ms}";\n'
            '  declare Text Lang = "en";\n'
            '  declare Text Msg = "";\n'
            '  declare Boolean Applied = False;\n'
            '  if (LocalUser != Null && LocalUser.Language != "") '
            'Lang = TL::SubString(LocalUser.Language, 0, 2);\n'
            f'  Msg = {en_expr};\n'
            + branches +
            '  while (True) {\n'
            '    yield;\n'
            f'    declare CMlLabel Lbl <=> (Page.GetFirstChild("{_LABEL_ID}") as CMlLabel);\n'
            '    if (Lbl == Null) { Applied = False; continue; }\n'
            '    if (!Applied) {\n'
            '      Lbl.SetText(Msg);\n'
            '      Applied = True;\n'
            '    }\n'
            '  }\n'
            '}\n'
            '--></script>'
        )
        return script
