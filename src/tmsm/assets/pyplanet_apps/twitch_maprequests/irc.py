"""Minimal asyncio Twitch IRC client.

Anonymous-read by default (no OAuth required): connects as
``justinfan<random>`` which Twitch accepts as a read-only chatter.
Optional bot OAuth (`oauth:...`) enables outbound PRIVMSGs so the
streamer can let the bot reply in chat.

We negotiate the ``twitch.tv/tags`` capability so PRIVMSG lines carry
``@badges=...;mod=1;subscriber=1;...`` tags, which the caller uses for
permission gating. No external dependencies.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

IRC_HOST = "irc.chat.twitch.tv"
IRC_PORT = 6667  # plain TCP; Twitch also accepts 6697 TLS, not needed here.


@dataclass
class ChatMessage:
    nick: str
    text: str
    tags: dict[str, str] = field(default_factory=dict)

    # ── badge helpers ──────────────────────────────────────────────────
    @property
    def badges(self) -> dict[str, str]:
        """Parsed ``badges=`` tag — {name: version}."""
        out: dict[str, str] = {}
        for chunk in (self.tags.get("badges") or "").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            name, _, ver = chunk.partition("/")
            out[name] = ver
        return out

    def is_broadcaster(self) -> bool:
        return "broadcaster" in self.badges

    def is_moderator(self) -> bool:
        return self.tags.get("mod") == "1" or "moderator" in self.badges

    def is_vip(self) -> bool:
        return "vip" in self.badges

    def is_subscriber(self) -> bool:
        return self.tags.get("subscriber") == "1" or "subscriber" in self.badges


def _parse_tags(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in raw.split(";"):
        if not chunk:
            continue
        k, _, v = chunk.partition("=")
        # IRCv3 tag escaping (we only care about a few cases).
        v = (v.replace(r"\:", ";").replace(r"\s", " ")
              .replace(r"\\", "\\").replace(r"\r", "\r").replace(r"\n", "\n"))
        out[k] = v
    return out


def _parse_line(line: str) -> tuple[dict[str, str], str, str, list[str]]:
    """Parse a single IRC line into ``(tags, prefix, command, params)``."""
    tags: dict[str, str] = {}
    if line.startswith("@"):
        tag_blob, _, line = line.partition(" ")
        tags = _parse_tags(tag_blob[1:])
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line.partition(" ")
        prefix = prefix[1:]
    # Split off trailing param (after ' :').
    trailing = ""
    if " :" in line:
        line, _, trailing = line.partition(" :")
    parts = [p for p in line.split(" ") if p]
    command = parts[0] if parts else ""
    params = parts[1:]
    if trailing:
        params.append(trailing)
    return tags, prefix, command, params


class TwitchIRC:
    """Single-channel Twitch IRC connection.

    Call :meth:`run` in a background asyncio task. It connects, joins the
    channel, and yields each PRIVMSG to ``on_message``. Reconnects with
    exponential backoff on disconnect; cancelling the task cleanly shuts
    everything down.
    """

    def __init__(
        self,
        channel: str,
        on_message,
        *,
        nick: str | None = None,
        oauth: str | None = None,
    ) -> None:
        self.channel = channel.lower().lstrip("#")
        self._on_message = on_message
        self._oauth = (oauth or "").strip()
        if nick and self._oauth:
            self.nick = nick.lower().strip()
        else:
            # Read-only anonymous user. Twitch accepts NICK justinfan<digits>
            # with any (or no) PASS.
            self.nick = f"justinfan{random.randint(10_000, 99_999_999)}"
        self._writer: asyncio.StreamWriter | None = None
        self._reader: asyncio.StreamReader | None = None
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()

    # ── public API ─────────────────────────────────────────────────────

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._connect_once()
                backoff = 1.0   # reset on clean exit
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("twitch_irc: %s (#%s) — reconnecting in %.1fs",
                               e, self.channel, backoff)
            await self._sleep_or_stop(backoff)
            backoff = min(backoff * 2, 60.0)

    async def stop(self) -> None:
        self._stop.set()
        w = self._writer
        if w is not None:
            try:
                w.close()
                await w.wait_closed()
            except Exception:
                pass

    async def say(self, text: str) -> None:
        """Send a PRIVMSG. No-op if no OAuth was configured."""
        if not self._oauth:
            return
        w = self._writer
        if w is None or w.is_closing():
            return
        # Twitch caps PRIVMSG payload around 500 chars. Trim to be safe.
        text = text.replace("\r", " ").replace("\n", " ")[:450]
        line = f"PRIVMSG #{self.channel} :{text}\r\n"
        async with self._lock:
            try:
                w.write(line.encode("utf-8", errors="replace"))
                await w.drain()
            except Exception:
                logger.exception("twitch_irc: say failed")

    # ── internals ──────────────────────────────────────────────────────

    async def _sleep_or_stop(self, sec: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=sec)
        except asyncio.TimeoutError:
            return

    async def _connect_once(self) -> None:
        reader, writer = await asyncio.open_connection(IRC_HOST, IRC_PORT)
        self._reader, self._writer = reader, writer
        try:
            # CAP first so the JOIN ACK already carries tags.
            await self._send_raw("CAP REQ :twitch.tv/tags twitch.tv/commands")
            # PASS is required by Twitch even for anonymous (any value works);
            # for OAuth users we send the real token.
            pw = self._oauth if self._oauth else "SCHMOOPIIE"
            await self._send_raw(f"PASS {pw}")
            await self._send_raw(f"NICK {self.nick}")
            await self._send_raw(f"JOIN #{self.channel}")
            logger.info("twitch_irc: connected as %s, joined #%s",
                        self.nick, self.channel)

            while not self._stop.is_set():
                raw = await reader.readline()
                if not raw:
                    raise ConnectionError("server closed connection")
                try:
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                except Exception:
                    continue
                if not line:
                    continue
                tags, prefix, cmd, params = _parse_line(line)

                if cmd == "PING":
                    pong_target = params[0] if params else ":tmi.twitch.tv"
                    await self._send_raw(f"PONG :{pong_target}")
                    continue

                if cmd == "PRIVMSG" and len(params) >= 2:
                    nick = prefix.split("!", 1)[0] if "!" in prefix else prefix
                    msg = ChatMessage(nick=nick, text=params[-1], tags=tags)
                    try:
                        await self._on_message(msg)
                    except Exception:
                        logger.exception("twitch_irc: on_message handler raised")
                    continue

                if cmd == "NOTICE":
                    # Login failures land here. Useful for streamer feedback.
                    logger.warning("twitch_irc NOTICE: %s", " ".join(params))
                    continue

                # Other server-side commands (RECONNECT, USERSTATE, ROOMSTATE,
                # JOIN/PART, 001/002/...) — ignore.
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            self._reader = None
            self._writer = None

    async def _send_raw(self, line: str) -> None:
        w = self._writer
        if w is None:
            return
        w.write((line + "\r\n").encode("utf-8", errors="replace"))
        try:
            await w.drain()
        except Exception:
            pass
