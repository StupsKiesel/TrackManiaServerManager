"""Discord webhook delivery for bug reports.

Discord limits:
  * 2000 chars per message `content`
  * up to 10 embeds per message; each embed description ≤ 4096 chars
  * total embed payload ≤ 6000 chars

We keep it simple: send each report as a single embed; batch ≤ 10 per
HTTP POST. Failure raises `DiscordDeliveryError` so the caller can
record + retry on next tick.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Iterable

import aiohttp

logger = logging.getLogger(__name__)


class DiscordDeliveryError(RuntimeError):
    pass


# Status -> Discord embed color (decimal)
_STATUS_COLOR = {
    "open":    0xF09000,  # amber
    "fixed":   0x00C04A,  # green
    "wontfix": 0x808080,  # grey
}


def _format_embed(row: dict[str, Any]) -> dict[str, Any]:
    """Build a single Discord embed dict for one report row."""
    rid = int(row.get("id", 0))
    subject = (row.get("subject") or "(no subject)").strip()
    status = (row.get("status") or "open").strip()
    color = _STATUS_COLOR.get(status, 0x5865F2)

    fields: list[dict[str, Any]] = []
    nick = row.get("nickname") or row.get("login") or "?"
    login = row.get("login") or "?"
    auth = row.get("auth_level") or ""
    reporter = f"{nick}  (`{login}`)"
    if auth:
        reporter += f"  · auth: {auth}"
    fields.append({"name": "Reporter", "value": reporter[:1024], "inline": False})

    map_name = row.get("map_name") or ""
    map_uid = row.get("map_uid") or ""
    mode = row.get("mode_script") or ""
    phase = row.get("game_phase") or ""
    if map_name or mode or phase:
        bits = []
        if map_name:
            bits.append(f"**Map:** {map_name}" + (f"  `[{map_uid}]`" if map_uid else ""))
        if mode:
            bits.append(f"**Mode:** `{mode}`")
        if phase:
            bits.append(f"**Phase:** {phase}")
        fields.append({"name": "Context", "value": "\n".join(bits)[:1024], "inline": False})

    tags = []
    if row.get("about_widgets"):
        tags.append("widgets")
    if row.get("about_ui"):
        tags.append("UI windows")
    classification_bits = []
    if tags:
        classification_bits.append("**About:** " + ", ".join(tags))
    if row.get("input_device"):
        classification_bits.append(f"**Input:** {row['input_device']}")
    classification_bits.append(
        f"**Openplanet:** {'yes' if row.get('uses_openplanet') else 'no'}"
    )
    if row.get("game_version"):
        classification_bits.append(f"**Server:** {row['game_version']}")
    if row.get("client_version"):
        classification_bits.append(f"**Client:** {row['client_version']}")
    if classification_bits:
        fields.append({"name": "Classification",
                       "value": "\n".join(classification_bits)[:1024], "inline": False})

    uptimes = []
    if row.get("pyplanet_uptime_s"):
        uptimes.append(f"PyPlanet {row['pyplanet_uptime_s']}s")
    if row.get("dedicated_uptime_s"):
        uptimes.append(f"Dedicated {row['dedicated_uptime_s']}s")
    if uptimes:
        fields.append({"name": "Uptimes",
                       "value": "  ·  ".join(uptimes)[:1024], "inline": True})

    details = (row.get("details") or "").strip()
    description = f"```\n{details[:3900]}\n```" if details else "_(no details)_"

    embed: dict[str, Any] = {
        "title": f"#{rid} — {subject[:240]}",
        "description": description,
        "color": color,
        "fields": fields,
        "footer": {"text": f"status: {status}"},
    }
    created = row.get("created_at")
    if hasattr(created, "isoformat"):
        try:
            embed["timestamp"] = created.replace(microsecond=0).isoformat() + "Z"
        except Exception:
            pass
    return embed


async def send_reports(webhook_url: str, rows: Iterable[dict[str, Any]],
                       *, header: str | None = None,
                       timeout_s: float = 10.0) -> int:
    """POST one or more report rows to a Discord webhook in batches of 10.

    Returns the count of rows actually delivered. Raises
    `DiscordDeliveryError` on the first failed batch (callers should
    treat any earlier batches as delivered).
    """
    rows = list(rows)
    if not webhook_url:
        raise DiscordDeliveryError("webhook URL not configured")
    if not rows:
        return 0

    timeout = aiohttp.ClientTimeout(total=timeout_s)
    delivered = 0
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for i in range(0, len(rows), 10):
            batch = rows[i:i + 10]
            payload: dict[str, Any] = {
                "embeds": [_format_embed(r) for r in batch],
                "allowed_mentions": {"parse": []},
            }
            if i == 0 and header:
                payload["content"] = header[:2000]
            try:
                async with session.post(webhook_url, json=payload) as resp:
                    if resp.status not in (200, 204):
                        body = await resp.text()
                        raise DiscordDeliveryError(
                            f"Discord webhook returned {resp.status}: {body[:200]}"
                        )
            except aiohttp.ClientError as e:
                raise DiscordDeliveryError(f"network error: {e}") from e
            delivered += len(batch)
    return delivered


async def send_ping(webhook_url: str, *, timeout_s: float = 10.0) -> None:
    """Send a one-line test message to verify the webhook works."""
    if not webhook_url:
        raise DiscordDeliveryError("webhook URL not configured")
    ts = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    payload = {
        "content": f":white_check_mark: tmsm `bug_reports` test ping ({ts}).",
        "allowed_mentions": {"parse": []},
    }
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(webhook_url, json=payload) as resp:
                if resp.status not in (200, 204):
                    body = await resp.text()
                    raise DiscordDeliveryError(
                        f"Discord webhook returned {resp.status}: {body[:200]}"
                    )
    except aiohttp.ClientError as e:
        raise DiscordDeliveryError(f"network error: {e}") from e
