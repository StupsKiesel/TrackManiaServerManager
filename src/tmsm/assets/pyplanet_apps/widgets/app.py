"""tmsm widgets — global widget framework.

Widgets are registered by other apps via the ``tmsm_widgets:register``
signal. They are HUD overlays (clock, race info, popup notifications,
etc.) that:

  * persist on-screen (kind=PERSISTENT) or pop up on demand (kind=POPUP)
  * resolve their position from defaults < global override < per-player override
  * can be moved in-game via the position editor (admin command ``/widgets``)
  * hide themselves client-side when named or raw ManiaScript conditions match
  * animate show/hide with configurable direction, duration, and delay

Signals exposed (namespace ``tmsm_widgets``)::

    register            entry=WidgetEntry             register or replace widget
    refresh             (none)                        re-render the editor
    popup               key=str, login=str            trigger a popup widget
    position_changed    key=str, scope=str, login=?   announce edit took effect
    edit_mode           login=str, active=bool        editor open / closed for player

Commands::

    /widgets            open the position editor (admin)
"""
from __future__ import annotations

import logging
from typing import Any

from pyplanet.apps.config import AppConfig
from pyplanet.apps.tmsm.hub.registry import HubAppEntry, Role, Status
from pyplanet.contrib.command import Command
from pyplanet.core.events import Signal

from .registry import WidgetEntry, WidgetKind
from .storage import WidgetStorage, default_defaults_path
from .views import WidgetEditorView

logger = logging.getLogger(__name__)

_ANIM_DIR_OPTIONS = ("none", "left", "right", "up", "down")


class WidgetsApp(AppConfig):
    name = "pyplanet.apps.tmsm.widgets"
    label = "tmsm_widgets"
    app_dependencies = ["core.maniaplanet", "tmsm_ui", "tmsm_hub"]
    game_dependencies = ["trackmania", "trackmania_next"]

    HUB_KEY = "widgets"
    HUB_NAME = "Widgets"
    HUB_ICON = "object-group"
    HUB_COLOR = "15f"
    HUB_DESCRIPTION = "Position and configure on-screen widgets."
    HUB_ROLE = Role.PLAYER
    HUB_STATUS = Status.BETA
    HUB_ORDER = 30
    HUB_COMMAND = "widgets"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entries: dict[str, WidgetEntry] = {}
        self.storage: WidgetStorage = WidgetStorage(self.instance)
        self.editor: WidgetEditorView | None = None
        # logins currently in editor mode (positions are draggable in their UI)
        self._editing: set[str] = set()
        # default scope chosen in the editor: "global" or "player"
        self._scope: dict[str, str] = {}  # login -> scope
        # currently selected widget in the editor: login -> widget key
        self._selected: dict[str, str] = {}
        # step size for nudge buttons (manialink units)
        self._step: dict[str, float] = {}
        # per-login debug overlay (master-only); when on, every widget
        # renders its debug chip + status label.
        self._debug: set[str] = set()

    # ---- lifecycle -----------------------------------------------------

    async def on_init(self) -> None:
        for code in ("register", "refresh", "popup",
                     "position_changed", "edit_mode", "request_register"):
            try:
                self.context.signals.register_signal(
                    Signal(code=code, namespace="tmsm_widgets")
                )
            except Exception:
                logger.exception("widgets: failed to register signal tmsm_widgets:%s", code)

    async def on_start(self) -> None:
        # DB-backed storage: seed the global table from bundled defaults
        # on first boot, then populate in-memory caches from the DB.
        try:
            await self.storage.seed_defaults(default_defaults_path())
            await self.storage.load()
        except Exception:
            logger.exception("widgets: storage init failed; in-memory only")

        self.editor = WidgetEditorView(self)
        self.editor.handle_catch_all = self._editor_catch_all  # type: ignore[assignment]

        try:
            await self.instance.command_manager.register(
                Command(command="widgets", target=self._cmd_widgets,
                        description="Open the tmsm widget position editor."),
            )
        except Exception:
            logger.exception("widgets: /widgets command registration failed")

        self.context.signals.listen("tmsm_widgets:register", self._on_register)
        self.context.signals.listen("tmsm_widgets:popup", self._on_popup_signal)
        self.context.signals.listen("tmsm_widgets:refresh", self._on_refresh)
        self.context.signals.listen("maniaplanet:player_disconnect", self._on_player_disconnect)
        self.context.signals.listen("maniaplanet:player_connect", self._on_player_connect)

        # Ask any widget-providing app that started before us (e.g. tmsm_hub)
        # to register itself now that our `register` signal has a listener.
        try:
            sig = self.context.signals.get_signal("tmsm_widgets:request_register")
            await sig.send_robust({}, raw=True)
        except Exception:
            logger.exception("widgets: emit tmsm_widgets:request_register failed")

        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
            entry = HubAppEntry(
                key=self.HUB_KEY,
                name=self.HUB_NAME,
                icon=self.HUB_ICON,
                color=self.HUB_COLOR,
                description=self.HUB_DESCRIPTION,
                role=self.HUB_ROLE,
                status=self.HUB_STATUS,
                tags=[],
                order=self.HUB_ORDER,
                command=self.HUB_COMMAND,
                author="tmsm",
                version="0.1",
                open=self._hub_open,
            )
            await sig.send_robust({"entry": entry}, raw=True)
        except KeyError:
            logger.info("widgets: tmsm_hub:register not available")
        except Exception:
            logger.exception("widgets: hub registration failed")

        logger.info("widgets: started; editor available via //widgets")

    async def _hub_open(self, player) -> None:
        await self._open_editor(player.login)

    async def on_stop(self) -> None:
        if self.editor is not None:
            try:
                await self.editor.destroy()
            except Exception:
                logger.exception("widgets: editor destroy failed")

    # ---- registry ------------------------------------------------------

    async def _on_register(self, entry: WidgetEntry | None = None, **kwargs) -> None:
        if entry is None or not isinstance(entry, WidgetEntry):
            logger.warning("widgets: tmsm_widgets:register received invalid payload: %r", entry)
            return
        self.entries[entry.key] = entry
        logger.info("widgets: registered '%s' (%s, kind=%s)",
                    entry.key, entry.name, entry.kind.value)
        # If anyone has the editor open, refresh it so the new widget shows.
        if self._editing:
            await self._refresh_editor_for(list(self._editing))

    async def _on_refresh(self, **kwargs) -> None:
        if self._editing:
            await self._refresh_editor_for(list(self._editing))

    async def _on_popup_signal(self, key: str | None = None, login: str | None = None,
                                **kwargs) -> None:
        if not key or not login:
            return
        entry = self.entries.get(key)
        if entry is None or entry.popup_trigger is None:
            logger.info("widgets: popup '%s' for %s skipped (not registered as popup)",
                        key, login)
            return
        try:
            await entry.popup_trigger(login)
        except Exception:
            logger.exception("widgets: popup trigger raised for '%s'", key)

    async def _on_player_disconnect(self, player, **kwargs) -> None:
        login = getattr(player, "login", None)
        if not login:
            return
        self._editing.discard(login)
        self._scope.pop(login, None)
        self._selected.pop(login, None)
        self._step.pop(login, None)

    async def _on_player_connect(self, player, **kwargs) -> None:
        # Persistent widgets are only sent at WidgetAppBase.on_start() to
        # players already online. A player who connects (or reconnects)
        # later otherwise never gets the manialink pushed to them.
        login = getattr(player, "login", None)
        if not login:
            return
        for entry in self.entries.values():
            if entry.kind != WidgetKind.PERSISTENT:
                continue
            app = self._find_widget_app(entry.key)
            if app is None or app.view is None:
                continue
            try:
                await app.view.display(player_logins=[login])
            except Exception:
                logger.exception("widgets: reconnect push '%s' failed", entry.key)

    # ---- public API for widget views -----------------------------------

    def resolve_position(self, key: str, login: str) -> dict[str, float]:
        entry = self.entries.get(key)
        if entry is None:
            return {}
        defaults = {
            "x": entry.default_x,
            "y": entry.default_y,
            "w": entry.default_w,
            "h": entry.default_h,
        }
        return self.storage.resolve(key, login, defaults)

    def resolve_behavior(self, key: str, login: str | None = None) -> dict[str, Any]:
        entry = self.entries.get(key)
        if entry is None:
            return {}
        defaults = {
            "hide_while_driving": True,
            "anim_dir": entry.animation.direction,
            "anim_duration_ms": entry.animation.duration_ms,
            "anim_delay_ms": entry.animation.delay_ms,
            "allow_personal": bool(entry.allow_personal),
        }
        return self.storage.resolve_behavior(key, defaults, login=login)

    def allow_personal(self, key: str) -> bool:
        """Effective personalization flag (class default + DB override)."""
        beh = self.resolve_behavior(key)
        return bool(beh.get("allow_personal", True))

    def is_editing(self, login: str) -> bool:
        return login in self._editing

    def is_debug(self, login: str, key: str | None = None) -> bool:
        """Whether the master-admin debug overlay is on for ``login``.

        When ``key`` is given, the overlay is only active for the widget
        currently selected in that login's editor.
        """
        if login not in self._debug:
            return False
        if key is None:
            return True
        return self._selected.get(login) == key

    # ---- commands ------------------------------------------------------

    async def _cmd_widgets(self, player, data, **kwargs) -> None:
        login = player.login
        if login in self._editing:
            await self._close_editor(login)
        else:
            await self._open_editor(login)

    async def _open_editor(self, login: str) -> None:
        self._editing.add(login)
        # Only master admins can edit global config. Everyone else is
        # locked to personal scope and only sees widgets that allow it.
        is_master = await self._login_is_master(login)
        if not is_master:
            self._scope[login] = "player"
        else:
            self._scope.setdefault(login, "global")
        self._step.setdefault(login, 1.0)
        if self._selected.get(login) is None and self.entries:
            self._selected[login] = sorted(self.entries.keys())[0]
        try:
            sig = self.context.signals.get_signal("tmsm_widgets:edit_mode")
            await sig.send_robust({"login": login, "active": True}, raw=True)
        except Exception:
            pass
        await self._refresh_editor_for([login])
        await self._refresh_all_widget_frames(login)

    async def _close_editor(self, login: str) -> None:
        self._editing.discard(login)
        try:
            sig = self.context.signals.get_signal("tmsm_widgets:edit_mode")
            await sig.send_robust({"login": login, "active": False}, raw=True)
        except Exception:
            pass
        if self.editor is not None:
            # BaseView.hide() destroys the underlying manialink (and nulls
            # its data), which would break the next display(). Use the raw
            # TemplateView per-player hide so we keep the editor alive.
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.editor, player_logins=[login])
            except Exception:
                logger.exception("widgets: editor per-player hide failed")
        await self._refresh_all_widget_frames(login)

    # ---- editor render -------------------------------------------------

    async def _refresh_editor_for(self, logins: list[str]) -> None:
        if self.editor is None or not logins:
            return
        try:
            await self.editor.display(player_logins=logins)
        except Exception:
            logger.exception("widgets: editor display failed")

    async def _refresh_all_widget_frames(self, login: str) -> None:
        """Refresh every widget view for one player (so edit-mode UI toggles)."""
        for entry in self.entries.values():
            app = self._find_widget_app(entry.key)
            if app is None or app.view is None:
                continue
            try:
                await app.view.display(player_logins=[login])
            except Exception:
                logger.exception("widgets: refresh '%s' failed", entry.key)

    def _find_widget_app(self, key: str):
        """Walk PyPlanet's app registry for a WidgetAppBase with this key."""
        try:
            apps = self.instance.apps.apps.values()
        except Exception:
            return None
        for app in apps:
            if getattr(app, "WIDGET_KEY", None) == key:
                return app
        return None

    # ---- editor actions ------------------------------------------------

    async def _editor_catch_all(self, player, action, values, **kwargs) -> None:
        login = player.login
        # PyPlanet strips the "<id>__" prefix before calling catch-all,
        # so we receive "<verb>__<arg>" here.
        try:
            verb, arg = action.split("__", 1)
        except ValueError:
            logger.warning("widgets editor: unrecognised action %s", action)
            return
        handler = getattr(self, f"_act_{verb}", None)
        if handler is None:
            logger.warning("widgets editor: no handler for verb '%s'", verb)
            return
        try:
            await handler(login, arg, values or {})
        except Exception:
            logger.exception("widgets editor: action '%s' raised", action)

    async def _act_select(self, login: str, key: str, _values: dict) -> None:
        if key in self.entries:
            self._selected[login] = key
            await self._refresh_editor_for([login])
            # debug overlay follows the selection -> repaint widget frames
            if login in self._debug:
                await self._refresh_all_widget_frames(login)

    async def _act_scope(self, login: str, scope: str, _values: dict) -> None:
        # radio_group emits "scope__set__<value>" -> arg arrives as "set__<value>"
        if scope.startswith("set__"):
            scope = scope[len("set__"):]
        if scope == "global" and not await self._login_is_master(login):
            await self._toast(login, "global config is master-admin only", "warning")
            return
        if scope == "player":
            sel = self._selected.get(login)
            entry = self.entries.get(sel) if sel else None
            if entry is not None and not self.allow_personal(sel):
                await self._toast(login, f"{entry.name}: personalization is disabled", "warning")
                return
        if scope in ("global", "player"):
            self._scope[login] = scope
            await self._refresh_editor_for([login])

    async def _login_is_master(self, login: str) -> bool:
        try:
            from pyplanet.apps.tmsm.ui import perms as _perms
            return _perms.is_master(login)
        except Exception:
            return False

    async def _act_step(self, login: str, step: str, _values: dict) -> None:
        opts = [0.5, 1.0, 2.0, 5.0]
        cur = self._step.get(login, 1.0)
        if step in ("inc", "dec"):
            # snap to nearest preset, then move one slot
            idx = min(range(len(opts)), key=lambda i: abs(opts[i] - cur))
            idx = max(0, min(len(opts) - 1, idx + (1 if step == "inc" else -1)))
            self._step[login] = opts[idx]
        else:
            try:
                self._step[login] = max(0.1, float(step))
            except ValueError:
                return
        await self._refresh_editor_for([login])

    async def _act_nudge(self, login: str, direction: str, _values: dict) -> None:
        key = self._selected.get(login)
        if not key or key not in self.entries:
            return
        step = self._step.get(login, 1.0)
        cur = self.resolve_position(key, login)
        dx = dy = 0.0
        if direction == "left":  dx = -step
        elif direction == "right": dx = step
        elif direction == "up":    dy = step
        elif direction == "down":  dy = -step
        else: return
        await self._write_pos(login, key, {"x": cur.get("x", 0) + dx,
                                          "y": cur.get("y", 0) + dy})

    async def _act_set(self, login: str, key: str, values: dict) -> None:
        if key not in self.entries:
            return
        new_pos: dict[str, float] = {}
        for field in ("x", "y", "w", "h"):
            raw = values.get(f"widget_{key}_{field}")
            if raw is None or raw == "":
                continue
            try:
                new_pos[field] = float(raw)
            except (TypeError, ValueError):
                continue
        beh_patch: dict[str, Any] = {}
        for field, caster in (("anim_duration_ms", int), ("anim_delay_ms", int)):
            raw = values.get(f"entry_widget_{key}_{field}")
            if raw is None or raw == "":
                continue
            try:
                beh_patch[field] = caster(float(raw))
            except (TypeError, ValueError):
                continue
        if not new_pos and not beh_patch:
            await self._toast(login, f"No values to apply for '{key}'", "warning")
            return
        if new_pos:
            await self._write_pos(login, key, new_pos)
        if beh_patch:
            scope = self._scope.get(login, "global")
            if scope == "player":
                if not self.allow_personal(key):
                    await self._toast(login, "personalization is disabled", "warning")
                else:
                    await self.storage.set_player_behavior(key, login, beh_patch)
                    await self._refresh_editor_for([login])
                    await self._refresh_widget_for_all(key)
            elif not await self._login_is_master(login):
                await self._toast(login, "global config is master-admin only", "warning")
            else:
                await self.storage.set_behavior(key, beh_patch)
                await self._refresh_editor_for([login])
                await self._refresh_widget_for_all(key)
        entry = self.entries.get(key)
        label = entry.name if entry else key
        await self._toast(login, f"{label}: settings saved", "success", source="widgets")

    async def _act_setdir(self, login: str, arg: str, _values: dict) -> None:
        # radio_group emits "setdir__set__<value>" -> arg arrives as "set__<value>"
        if arg.startswith("set__"):
            arg = arg[len("set__"):]
        try:
            key, direction = arg.split("|", 1)
        except ValueError:
            return
        if key not in self.entries or direction not in _ANIM_DIR_OPTIONS:
            return
        scope = self._scope.get(login, "global")
        is_master = await self._login_is_master(login)
        if scope == "player":
            if not self.allow_personal(key):
                await self._toast(login, "personalization is disabled", "warning")
                return
            await self.storage.set_player_behavior(key, login, {"anim_dir": direction})
        else:
            if not is_master:
                await self._toast(login, "global config is master-admin only", "warning")
                return
            await self.storage.set_behavior(key, {"anim_dir": direction})
        await self._refresh_editor_for([login])
        await self._refresh_widget_for_all(key)

    async def _act_drive(self, login: str, key: str, _values: dict) -> None:
        if key not in self.entries:
            return
        if not await self._login_is_master(login):
            await self._toast(login, "global config is master-admin only", "warning")
            return
        cur = self.resolve_behavior(key)
        await self.storage.set_behavior(key, {
            "hide_while_driving": not bool(cur.get("hide_while_driving", True)),
        })
        await self._refresh_editor_for([login])
        await self._refresh_widget_for_all(key)

    async def _act_allowperson(self, login: str, key: str, _values: dict) -> None:
        if key not in self.entries:
            return
        if not await self._login_is_master(login):
            await self._toast(login, "global config is master-admin only", "warning")
            return
        new_val = not self.allow_personal(key)
        await self.storage.set_behavior(key, {"allow_personal": new_val})
        await self._refresh_editor_for([login])
        await self._refresh_widget_for_all(key)

    async def _act_debug(self, login: str, _arg: str, _values: dict) -> None:
        """Toggle the per-login debug overlay (master-admin only)."""
        if not await self._login_is_master(login):
            await self._toast(login, "debug mode is master-admin only", "warning")
            return
        if login in self._debug:
            self._debug.discard(login)
        else:
            self._debug.add(login)
        await self._refresh_editor_for([login])
        await self._refresh_all_widget_frames(login)

    async def _act_dump(self, login: str, key: str, _values: dict) -> None:
        """Render the selected widget for the caller and write XML to disk
        (master-admin only). Dumps the currently-selected widget when
        ``key`` is empty or unknown."""
        if not await self._login_is_master(login):
            await self._toast(login, "dump is master-admin only", "warning")
            return
        if not key or key not in self.entries:
            key = self._selected.get(login) or ""
        if not key or key not in self.entries:
            await self._toast(login, "no widget selected to dump", "warning")
            return
        app = self._find_widget_app(key)
        if app is None or app.view is None:
            await self._toast(login, f"{key}: widget not active", "warning")
            return
        try:
            path = await app.view.dump_render(login)
        except Exception:
            logger.exception("widgets: dump of '%s' for %s failed", key, login)
            await self._toast(login, f"{key}: dump failed (see logs)", "error")
            return
        await self._toast(login, f"{key}: dumped to {path}", "success")

    async def _toast(self, login: str, msg: str, severity: str = "info",
                     source: str = "widgets") -> None:
        try:
            sig = self.context.signals.get_signal("tmsm_status:notify")
        except Exception:
            return
        try:
            await sig.send_robust({
                "message": msg, "severity": severity,
                "login": login, "source": source,
            })
        except Exception:
            logger.exception("widgets: toast emit failed")

    async def _act_drop(self, login: str, key: str, values: dict) -> None:
        """Legacy editor path — kept for future use. Per-widget drag uses
        :meth:`handle_widget_drop` instead, called by ``WidgetView``."""
        if key not in self.entries:
            return
        new_pos: dict[str, float] = {}
        for src, dst in (("widget_drop_x", "x"), ("widget_drop_y", "y")):
            raw = values.get(src)
            if raw is None or raw == "":
                continue
            try:
                new_pos[dst] = float(raw)
            except (TypeError, ValueError):
                continue
        if new_pos:
            await self._write_pos(login, key, new_pos)

    async def _act_mdrop(self, login: str, arg: str, _values: dict) -> None:
        """Mouse-drop from the editor's drag overlay.

        ``arg`` = ``<widget_key>|<x>|<y>`` (ManiaScript-formatted floats).
        """
        parts = arg.split("|")
        if len(parts) < 3:
            return
        key, x_raw, y_raw = parts[0], parts[1], parts[2]
        if key not in self.entries:
            return
        try:
            pos = {"x": float(x_raw), "y": float(y_raw)}
        except ValueError:
            return
        await self._write_pos(login, key, pos)

    async def handle_widget_drop(self, login: str, action: str, _values: dict) -> None:
        """Called from each WidgetView's catch-all on drag-release or click.

        Action received here (prefix already stripped):
          ``drop__<widget_key>|<x>|<y>`` or ``click__<widget_key>``.
        """
        logger.info("widgets: widget event %s from %s", action, login)
        try:
            verb, arg = action.split("__", 1)
        except ValueError:
            return
        if verb == "dbg":
            logger.warning("[tmsm_widgets][script] %s :: %s", login, arg)
            return
        if verb == "click":
            return  # click path is informational for now
        if verb != "drop":
            return
        parts = arg.split("|")
        if len(parts) < 3:
            return
        key, x_raw, y_raw = parts[0], parts[1], parts[2]
        if key not in self.entries:
            return
        try:
            pos = {"x": float(x_raw), "y": float(y_raw)}
        except ValueError:
            return
        await self._write_pos(login, key, pos)

    async def _act_reset(self, login: str, key: str, _values: dict) -> None:
        if key not in self.entries:
            return
        scope = self._scope.get(login, "global")
        if scope == "player":
            await self.storage.clear_player(key, login)
        else:
            await self.storage.clear_global(key)
        await self._announce_position_changed(key, scope, login)
        await self._refresh_editor_for([login])
        await self._refresh_widget_for_all(key)

    async def _act_close(self, login: str, _arg: str, _values: dict) -> None:
        await self._close_editor(login)

    # ---- write helper --------------------------------------------------

    async def _write_pos(self, login: str, key: str, pos: dict[str, float]) -> None:
        scope = self._scope.get(login, "global")
        entry = self.entries.get(key)
        if scope == "global" and not await self._login_is_master(login):
            await self._toast(login, "global config is master-admin only", "warning")
            return
        if scope == "player" and entry is not None and not self.allow_personal(key):
            await self._toast(
                login, f"{entry.name}: personalization is disabled", "warning",
            )
            return
        if scope == "player":
            await self.storage.set_player(key, login, pos)
        else:
            await self.storage.set_global(key, pos)
        await self._announce_position_changed(key, scope, login)
        await self._refresh_editor_for([login])
        await self._refresh_widget_for_all(key)

    async def _announce_position_changed(self, key: str, scope: str, login: str) -> None:
        try:
            sig = self.context.signals.get_signal("tmsm_widgets:position_changed")
            await sig.send_robust(
                {"key": key, "scope": scope, "login": login}, raw=True,
            )
        except Exception:
            pass

    async def _refresh_widget_for_all(self, key: str) -> None:
        app = self._find_widget_app(key)
        if app is None or app.view is None:
            return
        try:
            await app.view.refresh()
        except Exception:
            logger.exception("widgets: post-edit refresh of '%s' failed", key)

    # ---- editor context (consumed by editor.xml) -----------------------

    def editor_context(self, login: str) -> dict[str, Any]:
        # Only master admins see every widget and may edit global config.
        # Everyone else only sees widgets that allow personalization.
        try:
            from pyplanet.apps.tmsm.ui import perms as _perms
            is_master = bool(_perms.is_master(login))
        except Exception:
            is_master = False
        all_keys = sorted(self.entries.keys())
        if is_master:
            keys = all_keys
        else:
            keys = [k for k in all_keys if self.allow_personal(k)]
        selected_key = self._selected.get(login)
        if selected_key not in keys:
            selected_key = keys[0] if keys else ""
            self._selected[login] = selected_key
        selected = self.entries.get(selected_key)
        scope = self._scope.get(login, "global")
        sel_allow = self.allow_personal(selected_key) if selected_key else True
        # Non-master always forced to player scope.
        if not is_master:
            scope = "player"
            self._scope[login] = scope
        elif selected is not None and not sel_allow and scope == "player":
            scope = "global"
            self._scope[login] = scope
        step = self._step.get(login, 1.0)
        rows = []
        for k in keys:
            e = self.entries[k]
            pos = self.resolve_position(k, login)
            rows.append({
                "key": k,
                "name": e.name,
                "icon": e.icon,
                "kind": e.kind.value,
                "selected": k == selected_key,
                "x": pos.get("x", e.default_x),
                "y": pos.get("y", e.default_y),
                "w": pos.get("w", e.default_w),
                "h": pos.get("h", e.default_h),
            })
        return {
            "rows": rows,
            "selected_key": selected_key,
            "selected_name": selected.name if selected else "",
            "scope": scope,
            "allow_personal": sel_allow,
            "is_master": is_master,
            "debug": login in self._debug,
            "step": step,
            "step_options": [0.5, 1.0, 2.0, 5.0],
            "behavior": self.resolve_behavior(selected_key, login=login) if selected_key else {},
        }
