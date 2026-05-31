"""Curated catalog of media URIs usable in Manialink ``<quad image="...">``.

The catalog is intentionally small at first \u2013 expand it as we confirm which
asset paths the player's client actually has. Each item is a dict:

    {
        "key":   "unique_slug",
        "label": "Human label",
        "url":   "file://Media/... or https://...",
        "note":  "where it's typically used"
    }

Categories are ordered for tab display.
"""
from __future__ import annotations

from typing import Any, Dict, List


CATALOG: List[Dict[str, Any]] = [
    {
        "key":   "stamps",
        "label": "Painter Stamps",
        "items": [
            {"key": "stamp_3", "label": "Stamp 3",
             "url": "file://Media/Painter/Stamps/3.dds",
             "note": "Numeral 3 \u2014 large bright digit, good for countdowns."},
            {"key": "stamp_arrow", "label": "Arrow",
             "url": "file://Media/Painter/Stamps/Arrow.dds",
             "note": "Generic arrow stamp."},
        ],
    },
    {
        "key":   "manialink_common",
        "label": "Manialinks/Common",
        "items": [
            {"key": "logo_nadeo", "label": "Nadeo logo",
             "url": "file://Media/Manialinks/Common/Logos/nadeo.dds",
             "note": "Stock Nadeo logo \u2014 may vary per title."},
        ],
    },
    {
        "key":   "external",
        "label": "External (HTTPS)",
        "items": [
            {"key": "placehold_dark",
             "label": "Placeholder 240x80 (dark)",
             "url": "https://placehold.co/240x80/222/fff.png?text=tmsm",
             "note": "Sanity-check URL: confirms the client can fetch HTTPS images."},
            {"key": "placehold_green",
             "label": "Placeholder 80x80 (green)",
             "url": "https://placehold.co/80x80/15df15/ffffff.png?text=OK",
             "note": "Used for live indicators."},
        ],
    },
]


def find_item(url: str) -> Dict[str, Any] | None:
    """Return the catalog item matching ``url`` (or None)."""
    for cat in CATALOG:
        for it in cat["items"]:
            if it["url"] == url:
                return it
    return None


def category(key: str) -> Dict[str, Any] | None:
    for cat in CATALOG:
        if cat["key"] == key:
            return cat
    return None
