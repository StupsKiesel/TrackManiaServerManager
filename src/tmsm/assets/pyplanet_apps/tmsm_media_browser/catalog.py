"""Curated catalog of imagery usable inside Manialinks.

Two kinds of entries:

* URL-style:    ``{"key", "label", "url", "note"}``
                rendered with ``<quad image="..."/>``. Use for HTTPS thumbnails
                (TMX, CDN logos, placeholders) and the handful of reliable
                ``file://Media/Flags/*.dds`` paths.

* Style-style:  ``{"key", "label", "style", "substyle", "note"}``
                rendered with ``<quad style="..." substyle="..."/>``. Hits
                Nadeo's built-in atlas inside the client title pack so always
                resolves without any external assets.

Categories are ordered for tab display.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


# Nadeo style atlases shipped inside every title pack. Curated subsets of the
# substyle names; the full atlases include unused/blank slots in TM2020.

_ICONS_128x128_1 = [
    "Advanced", "Beginner", "Browse", "Buddies", "Challenge", "ChallengeAuthor",
    "Coppers", "Create", "Custom", "CustomStars", "Default", "Download",
    "Easy", "Editor", "Extreme", "Forever", "GhostEditor", "Hard", "Hotseat",
    "Inputs", "Invite", "LadderPoints", "Lan", "Launch", "Load", "LoadTrack",
    "Manialink", "ManiaZones", "MedalCount", "Medias", "Multiplayer",
    "NewTrack", "Nations", "Options", "Padlock", "Paint", "Personal", "Pro",
    "ProfileAdvanced", "ProfileVehicle", "Programming", "Puzzle", "Quit",
    "Race", "Rankings", "Replay", "Save", "ServersAll", "ServersFavorites",
    "ServersSuggested", "Share", "ShareBlink", "SkillPoints", "Solo",
    "Statistics", "Stunts", "TrackInfo", "Url", "Vehicles",
]

_ICONS_128x128_BLOCKS = [
    "GhostsAdd", "GhostsRemove", "Multi", "MultiHard", "Validate",
]

_ICONS_64x64_1 = [
    "ArrowDown", "ArrowFastNext", "ArrowFastPrev", "ArrowFirst", "ArrowLast",
    "ArrowNext", "ArrowPrev", "ArrowUp", "Browser", "Buddy", "Camera",
    "Check", "ClipPause", "ClipPlay", "ClipRewind", "Close", "EmptyIcon",
    "Finish", "Hotseat", "IconLeagueStarGold1", "IconLeagueStarGold2",
    "IconLeagueStarGold3", "IconPlayers", "IconServers", "Inbox", "Lan",
    "Maximize", "Medal_0", "Medal_1", "Medal_2", "Medal_3", "Minimize",
    "Multiplayer", "NewMessage", "OfficialRace", "Options", "Outbox",
    "Padlock", "QuitRace", "RestartRace", "ShowDown2", "ShowLeft2",
    "ShowRight2", "ShowUp2", "Solo", "SoloStats", "StateFavourite",
    "StatePrivate", "StatePublic", "TagTypeBronze", "TagTypeGold",
    "TagTypeNadeo", "TagTypeSilver", "ToolDelete", "ToolDown", "ToolLeft",
    "ToolRight", "ToolRoot", "ToolUp", "TrackInfo",
]

_ICONS_128x32_1 = [
    "RT_Cup", "RT_Laps", "RT_Rounds", "RT_Script", "RT_Stunts", "RT_Team",
    "RT_TimeAttack",
]

_BGS_1 = [
    "BgCard", "BgCard1", "BgCard2", "BgCard3", "BgCardBuddy", "BgCardChallenge",
    "BgCardFolder", "BgCardList", "BgCardOnline", "BgCardPlayer", "BgCardServer",
    "BgCardSystem", "BgList", "BgListLine", "BgPager", "BgProgressBar",
    "BgSlider", "BgTitle2", "BgTitle3_1", "BgTitle3_3", "BgTitle3_4",
    "BgTitle3_5", "BgTitleGlow", "BgTitleShadow", "BgWindow1", "BgWindow2",
    "BgWindow3", "NavButton", "NavButtonBlink", "ProgressBar", "ProgressBarSmall",
]

_MEDALS_BIG = [
    "MedalBronze", "MedalGold", "MedalGoldPerspective", "MedalNadeo",
    "MedalNadeoPerspective", "MedalNone", "MedalSilver", "MedalSlot",
]


def _style_items(prefix: str, style: str,
                 substyles: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for sub in substyles:
        out.append({
            "key":      "{}__{}".format(prefix, sub),
            "label":    sub,
            "style":    style,
            "substyle": sub,
            "note":     "Nadeo built-in: style={} substyle={}".format(style, sub),
        })
    return out


CATALOG: List[Dict[str, Any]] = [
    {
        "key":   "icons_128",
        "label": "Icons 128x128",
        "items": _style_items("i128", "Icons128x128_1", _ICONS_128x128_1),
    },
    {
        "key":   "icons_128_blocks",
        "label": "Icons 128 blocks",
        "items": _style_items("i128b", "Icons128x128_Blocks",
                              _ICONS_128x128_BLOCKS),
    },
    {
        "key":   "icons_64",
        "label": "Icons 64x64",
        "items": _style_items("i64", "Icons64x64_1", _ICONS_64x64_1),
    },
    {
        "key":   "icons_128x32",
        "label": "Race-mode tags",
        "items": _style_items("i12832", "Icons128x32_1", _ICONS_128x32_1),
    },
    {
        "key":   "bgs1",
        "label": "Backgrounds",
        "items": _style_items("bgs1", "Bgs1", _BGS_1),
    },
    {
        "key":   "bgs1_inrace",
        "label": "Backgrounds (race)",
        "items": _style_items("bgs1r", "Bgs1InRace", _BGS_1),
    },
    {
        "key":   "medals",
        "label": "Medals",
        "items": _style_items("med", "MedalsBig", _MEDALS_BIG),
    },
    {
        "key":   "flags",
        "label": "Country flags",
        "items": [
            {"key": "flag_world", "label": "World",
             "url": "file://Media/Flags/World.dds",
             "note": "Default fallback flag."},
            {"key": "flag_germany", "label": "Germany",
             "url": "file://Media/Flags/Germany.dds",
             "note": "Replace filename with any country name."},
            {"key": "flag_france", "label": "France",
             "url": "file://Media/Flags/France.dds", "note": ""},
            {"key": "flag_namerica", "label": "N. America",
             "url": "file://Media/Flags/namerica.dds",
             "note": "Continent flag."},
            {"key": "flag_samerica", "label": "S. America",
             "url": "file://Media/Flags/samerica.dds",
             "note": "Continent flag."},
        ],
    },
    {
        "key":   "tmx_vistas",
        "label": "TMX env vistas",
        "items": [
            {"key": "vista_stadium", "label": "Stadium",
             "url": "https://trackmania.exchange/img/env/tm3_e1.png",
             "note": "TMNF/TMUF Stadium environment vista (from TMX)."},
            {"key": "vista_white_shore", "label": "White Shore",
             "url": "https://trackmania.exchange/img/env/tm3_e5.png",
             "note": "TMNF/TMUF Ice / White-Shore vista (from TMX)."},
            {"key": "vista_blue_bay", "label": "Blue Bay",
             "url": "https://trackmania.exchange/img/env/tm3_e4.png",
             "note": "TMNF/TMUF Bay vista (from TMX)."},
            {"key": "vista_red_island", "label": "Red Island",
             "url": "https://trackmania.exchange/img/env/tm3_e2.png",
             "note": "TMNF/TMUF Island/Desert vista (from TMX)."},
        ],
    },
    {
        "key":   "external",
        "label": "External (HTTPS)",
        "items": [
            {"key": "placehold_dark",
             "label": "Placeholder 240x80",
             "url": "https://placehold.co/240x80/222/fff.png?text=tmsm",
             "note": "Sanity-check URL: confirms the client can fetch HTTPS."},
            {"key": "placehold_green",
             "label": "Placeholder 80x80",
             "url": "https://placehold.co/80x80/15df15/ffffff.png?text=OK",
             "note": "Used for live indicators."},
            {"key": "pyplanet_logo",
             "label": "PyPlanet logo (CDN)",
             "url": "http://maniacdn.net/toffe/pyplanet/assets/logo/pyplanet-xs.png",
             "note": "Shipped CDN logo used by the default PyPlanet UI."},
        ],
    },
]


def find_item(token: str) -> Optional[Dict[str, Any]]:
    """Locate a catalog item by URL or ``style:substyle`` token."""
    for cat in CATALOG:
        for it in cat["items"]:
            if "url" in it and it["url"] == token:
                return it
            if "style" in it and "{}:{}".format(it["style"], it["substyle"]) == token:
                return it
    return None


def item_by_key(key: str) -> Optional[Dict[str, Any]]:
    for cat in CATALOG:
        for it in cat["items"]:
            if it["key"] == key:
                return it
    return None


def category(key: str) -> Optional[Dict[str, Any]]:
    for cat in CATALOG:
        if cat["key"] == key:
            return cat
    return None

