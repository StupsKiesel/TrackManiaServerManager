"""ui_demo — showcase every tmsm.ui v1 widget on a single panel.

Visible to master-admins. Every interactive element prints to chat what it
did so you can verify the wiring without instrumenting the framework.
"""
from __future__ import annotations

import logging
import os

from aiohttp import web

from pyplanet.apps.config import AppConfig

from .gbx_thumb import read_thumbnail
from .view import DemoView

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:  # hub app not installed in this pool
    _HAS_HUB = False

logger = logging.getLogger(__name__)

CHAT = "$ff0[ui_demo]$z"


class UiDemoApp(AppConfig):
    name = "pyplanet.apps.tmsm.ui_demo"
    label = "ui_demo"
    app_dependencies = ["core.maniaplanet", "tmsm_ui", "tmsm_hub"]
    game_dependencies = ["trackmania", "trackmania_next"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.view: DemoView | None = None
        self.checkbox_state: bool = False
        self.public_state: bool = True
        self.text_value: str = ""
        self.active_tab: str = "buttons"
        self.scroll_offset: float = 0.0
        self.scroll_step: float = 5.0  # one row at a time
        self.scroll_max: float = (30 * 5 + 2) - 56  # content_h - viewport h
        self.radio_choice: str = "normal"
        # advanced tab state
        self.combo_open: bool = False
        self.combo_value: str = "eu"
        self.stepper_value: int = 10
        self.modal_open: bool = False
        self.toast_msg: str | None = None
        self.toast_variant: str = "primary"
        self.tags: list[str] = ["speed", "endurance", "tech", "dirt"]
        # data table state
        self.table_sort_key: str = "score"
        self.table_sort_dir: str = "desc"
        self.stack_step: str = "intro"
        # dialogs tab state
        self.info_open: bool = False
        self.error_open: bool = False
        self.confirm_open: bool = False
        self.confirm_action_open: bool = False
        self.confirm_save_open: bool = False
        self.confirm_publish_open: bool = False
        self.confirm_reveal_open: bool = False
        self.dlg_savequit_open: bool = False
        self.image_msg: str = ""
        # 'more' tab state (switch, slider, pagination, accordion, menu)
        self.sw_notify: bool = True
        self.sw_replays: bool = False
        self.sw_public: bool = True
        self.vol: int = 60
        self.tmo: int = 30
        self.page: int = 1
        self.total_pages: int = 12
        self.menu_open: bool = False
        self.menu_result: str = ""
        self.acc_open: list[str] = ["general"]
        # 'more2' tab state
        self.q_value: str = ""
        self.crumb_current: str = "Foo"
        self.banner_info: bool = True
        self.banner_off_dismiss: bool = True
        self.confirm_result: str = ""
        # link + tree_view
        self.link_msg: str = ""
        self.tree_open: set[str] = {"maps", "maps/summer"}
        self.tree_selected: str = ""
        # current map thumbnail
        self.thumb_port: int = 8765
        self.thumb_uid: str = ""
        self.thumb_name: str = ""
        self._thumb_runner: web.AppRunner | None = None

    async def on_start(self) -> None:
        logger.info("ui_demo: on_start")
        try:
            self.view = DemoView(self)
        except Exception:
            logger.exception("ui_demo: view init failed")
            return
        await self._start_thumb_server()
        await self._refresh_current_map()
        self.context.signals.listen("maniaplanet:map_begin", self._on_map_begin)
        await self._register_with_hub()

        logger.info("ui_demo: view ready (id=%s); launch via the hub", self.view.id)

    async def _register_with_hub(self) -> None:
        if not _HAS_HUB:
            logger.info("ui_demo: tmsm_hub not available; skipping hub registration")
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            logger.info("ui_demo: tmsm_hub:register signal not registered yet; skipping")
            return
        entry = HubAppEntry(
            key="ui_demo",
            name="UI Demo",
            icon="cog",
            role=Role.MASTER,
            description="tmsm.ui v1 widget showcase",
            open=self._hub_open,
            order=999,
        )
        await sig.send_robust({"entry": entry}, raw=True)

    async def _hub_open(self, player) -> None:
        if self.view is None:
            return
        try:
            await self.view.display(player_logins=[player.login])
        except Exception:
            logger.exception("ui_demo: hub_open display failed")

    async def on_stop(self) -> None:
        if self.view is not None:
            await self.view.hide()
            self.view = None
        await self._stop_thumb_server()

    # ---- thumbnail server ---------------------------------------------

    async def _start_thumb_server(self) -> None:
        app = web.Application()
        app.router.add_get("/thumb.jpg", self._handle_thumb)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.thumb_port)
        try:
            await site.start()
            self._thumb_runner = runner
            logger.info("ui_demo: thumbnail server on :%d", self.thumb_port)
        except OSError as e:
            logger.warning("ui_demo: thumbnail server failed to bind: %s", e)
            await runner.cleanup()

    async def _stop_thumb_server(self) -> None:
        if self._thumb_runner is not None:
            await self._thumb_runner.cleanup()
            self._thumb_runner = None

    async def _handle_thumb(self, request: web.Request) -> web.Response:
        path = self._current_map_path()
        if path is None:
            return web.Response(status=404, text="no current map")
        blob = read_thumbnail(path)
        if blob is None:
            return web.Response(status=404, text="no thumbnail in map")
        # GBX stores the JPEG upside-down -- flip vertically before serving.
        try:
            from io import BytesIO
            from PIL import Image
            img = Image.open(BytesIO(blob)).transpose(Image.FLIP_TOP_BOTTOM)
            buf = BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=88)
            blob = buf.getvalue()
        except Exception:
            logger.exception("ui_demo: thumbnail flip failed; serving raw")
        return web.Response(
            body=blob,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    def _current_map_path(self) -> str | None:
        cur = self.instance.map_manager.current_map
        if cur is None:
            return None
        rel = getattr(cur, "file", None)
        base = getattr(self.instance.game, "server_map_dir", None)
        if not rel or not base:
            return None
        full = os.path.join(base, rel)
        return full if os.path.isfile(full) else None

    async def _refresh_current_map(self) -> None:
        cur = self.instance.map_manager.current_map
        if cur is None:
            self.thumb_uid = ""
            self.thumb_name = ""
            return
        self.thumb_uid = getattr(cur, "uid", "") or getattr(cur, "map_uid", "") or ""
        self.thumb_name = getattr(cur, "name", "") or ""

    async def _on_map_begin(self, *args, **kwargs) -> None:
        # Update internal thumbnail state.
        await self._refresh_current_map()
        # TM2020 manialinks persist across map changes (timeout=0). Hide the
        # demo for everyone so it doesn't carry over to the next map; the
        # user can re-open it from the hub if they want.
        if self.view is not None:
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.view)
            except Exception:
                logger.exception("ui_demo: hide on map_begin failed")

    # ---- handlers ------------------------------------------------------

    async def on_click(self, player, **kwargs):
        logger.info("ui_demo: on_click by %s", player.login)
        await self.instance.chat(f"{CHAT} ${player.nickname}$z clicked a button")

    async def on_toggle(self, player, **kwargs):
        self.checkbox_state = not self.checkbox_state
        logger.info("ui_demo: on_toggle by %s -> %s", player.login, self.checkbox_state)
        await self.instance.chat(
            f"{CHAT} ${player.nickname}$z toggled checkbox "
            f"-> {'ON' if self.checkbox_state else 'OFF'}"
        )
        if self.view is not None:
            await self.view.refresh()

    async def on_public_toggle(self, player, **kwargs):
        self.public_state = not self.public_state
        if self.view is not None:
            await self.view.refresh()

    async def on_submit(self, player, values=None, **kwargs):
        logger.info("ui_demo: on_submit by %s, values keys=%s",
                    player.login, list(values.keys()) if values else None)
        if values:
            key = next(
                (k for k in values if k.startswith("entry_") and k.endswith("__title")),
                None,
            )
            if key:
                self.text_value = str(values.get(key, ""))
        await self.instance.chat(
            f"{CHAT} ${player.nickname}$z submitted: $fff{self.text_value!r}"
        )
        if self.view is not None:
            await self.view.refresh()

    async def on_tab_list(self, player, **kwargs):
        self.active_tab = "list"
        if self.view is not None:
            await self.view.refresh()

    async def on_tab_info(self, player, **kwargs):
        self.active_tab = "info"
        if self.view is not None:
            await self.view.refresh()

    def make_tab_handler(self, key: str):
        async def _handler(player, **kwargs):
            self.active_tab = key
            logger.info("ui_demo: switched tab -> %s", key)
            if self.view is not None:
                await self.view.refresh()
        return _handler

    def make_radio_handler(self, attr: str, value):
        async def _handler(player, **kwargs):
            setattr(self, attr, value)
            logger.info("ui_demo: %s -> %r", attr, value)
            await self.instance.chat(
                f"{CHAT} ${player.nickname}$z picked $fff{value}$z"
            )
            if self.view is not None:
                await self.view.refresh()
        return _handler

    # ---- advanced handlers --------------------------------------------

    async def on_combo_toggle(self, player, **kwargs):
        self.combo_open = not self.combo_open
        if self.view is not None:
            await self.view.refresh()

    def make_combo_pick(self, value: str):
        async def _handler(player, **kwargs):
            self.combo_value = value
            self.combo_open = False
            if self.view is not None:
                await self.view.refresh()
        return _handler

    async def on_stepper_inc(self, player, **kwargs):
        self.stepper_value += 1
        if self.view is not None:
            await self.view.refresh()

    async def on_stepper_dec(self, player, **kwargs):
        self.stepper_value -= 1
        if self.view is not None:
            await self.view.refresh()

    async def on_modal_open(self, player, **kwargs):
        self.modal_open = True
        if self.view is not None:
            await self.view.refresh()

    async def on_modal_close(self, player, **kwargs):
        self.modal_open = False
        if self.view is not None:
            await self.view.refresh()

    async def on_modal_confirm(self, player, **kwargs):
        self.modal_open = False
        await self.instance.chat(f"{CHAT} ${player.nickname}$z confirmed the modal")
        if self.view is not None:
            await self.view.refresh()

    async def on_show_toast(self, player, **kwargs):
        import asyncio
        self.toast_msg = f"Hello {player.nickname}!"
        self.toast_variant = "success"
        if self.view is not None:
            await self.view.refresh()
        async def _clear():
            await asyncio.sleep(3.0)
            self.toast_msg = None
            if self.view is not None:
                await self.view.refresh()
        asyncio.ensure_future(_clear())

    def make_tag_remove(self, value: str):
        async def _handler(player, **kwargs):
            if value in self.tags:
                self.tags.remove(value)
            if self.view is not None:
                await self.view.refresh()
        return _handler

    def make_table_sort(self, key: str):
        async def _handler(player, **kwargs):
            if self.table_sort_key == key:
                self.table_sort_dir = "desc" if self.table_sort_dir == "asc" else "asc"
            else:
                self.table_sort_key = key
                self.table_sort_dir = "asc"
            if self.view is not None:
                await self.view.refresh()
        return _handler

    async def on_stack_next(self, player, **kwargs):
        order = ["intro", "setup", "done"]
        i = order.index(self.stack_step)
        self.stack_step = order[(i + 1) % len(order)]
        if self.view is not None:
            await self.view.refresh()

    async def on_stack_prev(self, player, **kwargs):
        order = ["intro", "setup", "done"]
        i = order.index(self.stack_step)
        self.stack_step = order[(i - 1) % len(order)]
        if self.view is not None:
            await self.view.refresh()

    async def on_scroll_up(self, player, **kwargs):
        self.scroll_offset = max(0.0, self.scroll_offset - self.scroll_step)
        if self.view is not None:
            await self.view.refresh()

    async def on_scroll_down(self, player, **kwargs):
        self.scroll_offset = min(self.scroll_max, self.scroll_offset + self.scroll_step)
        if self.view is not None:
            await self.view.refresh()

    # ---- dialogs tab --------------------------------------------------

    async def _refresh(self):
        if self.view is not None:
            await self.view.refresh()

    async def on_open_info(self, player, **kwargs):
        self.info_open = True
        await self._refresh()

    async def on_info_ok(self, player, **kwargs):
        self.info_open = False
        await self._refresh()

    async def on_open_error(self, player, **kwargs):
        self.error_open = True
        await self._refresh()

    async def on_error_ok(self, player, **kwargs):
        self.error_open = False
        await self._refresh()

    async def on_open_confirm(self, player, **kwargs):
        self.confirm_open = True
        self.confirm_result = ""
        await self._refresh()

    async def on_confirm_ok(self, player, **kwargs):
        self.confirm_open = False
        self.confirm_result = "DELETED"
        await self.instance.chat(f"{CHAT} ${player.nickname}$z confirmed (Delete)")
        await self._refresh()

    async def on_confirm_cancel(self, player, **kwargs):
        self.confirm_open = False
        self.confirm_result = "cancelled"
        await self._refresh()

    async def on_open_confirm_action(self, player, **kwargs):
        self.confirm_action_open = True
        self.confirm_result = ""
        await self._refresh()

    async def on_confirm_action_ok(self, player, **kwargs):
        self.confirm_action_open = False
        self.confirm_result = "RESTARTED"
        await self.instance.chat(f"{CHAT} ${player.nickname}$z confirmed (Restart)")
        await self._refresh()

    async def on_confirm_action_cancel(self, player, **kwargs):
        self.confirm_action_open = False
        self.confirm_result = "cancelled"
        await self._refresh()

    async def on_open_confirm_save(self, player, **kwargs):
        self.confirm_save_open = True
        self.confirm_result = ""
        await self._refresh()

    async def on_confirm_save_ok(self, player, **kwargs):
        self.confirm_save_open = False
        self.confirm_result = "SAVED"
        await self.instance.chat(f"{CHAT} ${player.nickname}$z confirmed (Save)")
        await self._refresh()

    async def on_confirm_save_cancel(self, player, **kwargs):
        self.confirm_save_open = False
        self.confirm_result = "cancelled"
        await self._refresh()

    async def on_open_confirm_publish(self, player, **kwargs):
        self.confirm_publish_open = True
        self.confirm_result = ""
        await self._refresh()

    async def on_confirm_publish_ok(self, player, **kwargs):
        self.confirm_publish_open = False
        self.confirm_result = "PUBLISHED"
        await self.instance.chat(f"{CHAT} ${player.nickname}$z confirmed (Publish)")
        await self._refresh()

    async def on_confirm_publish_cancel(self, player, **kwargs):
        self.confirm_publish_open = False
        self.confirm_result = "cancelled"
        await self._refresh()

    async def on_open_confirm_reveal(self, player, **kwargs):
        self.confirm_reveal_open = True
        self.confirm_result = ""
        await self._refresh()

    async def on_confirm_reveal_ok(self, player, **kwargs):
        self.confirm_reveal_open = False
        self.confirm_result = "REVEALED"
        await self.instance.chat(f"{CHAT} ${player.nickname}$z confirmed (Show)")
        await self._refresh()

    async def on_confirm_reveal_cancel(self, player, **kwargs):
        self.confirm_reveal_open = False
        self.confirm_result = "cancelled"
        await self._refresh()

    async def on_open_dlg_savequit(self, player, **kwargs):
        self.dlg_savequit_open = True
        self.confirm_result = ""
        await self._refresh()

    async def on_dlg_savequit_ok(self, player, **kwargs):
        self.dlg_savequit_open = False
        self.confirm_result = "SAVED"
        await self.instance.chat(f"{CHAT} ${player.nickname}$z chose Save")
        await self._refresh()

    async def on_dlg_savequit_extra(self, player, **kwargs):
        self.dlg_savequit_open = False
        self.confirm_result = "DISCARDED"
        await self.instance.chat(f"{CHAT} ${player.nickname}$z chose Don't save")
        await self._refresh()

    async def on_dlg_savequit_cancel(self, player, **kwargs):
        self.dlg_savequit_open = False
        self.confirm_result = "cancelled"
        await self._refresh()

    async def on_img_clicked(self, player, **kwargs):
        self.image_msg = f"clicked by {player.nickname}"
        await self._refresh()

    # --- 'more' tab handlers ---
    async def on_sw_notify_toggle(self, player, **kwargs):
        self.sw_notify = not self.sw_notify
        await self._refresh()

    async def on_sw_replays_toggle(self, player, **kwargs):
        self.sw_replays = not self.sw_replays
        await self._refresh()

    async def on_sw_public_toggle(self, player, **kwargs):
        self.sw_public = not self.sw_public
        await self._refresh()

    def make_slider_set(self, attr: str, value: int):
        async def handler(player, **kwargs):
            setattr(self, attr, int(value))
            await self._refresh()
        return handler

    def make_slider_step(self, attr: str, delta: int, lo: int, hi: int):
        async def handler(player, **kwargs):
            new = max(lo, min(hi, getattr(self, attr) + delta))
            setattr(self, attr, new)
            await self._refresh()
        return handler

    def make_page_goto(self, n: int):
        async def handler(player, **kwargs):
            self.page = max(1, min(self.total_pages, n))
            await self._refresh()
        return handler

    async def on_pg_first(self, player, **kwargs):
        self.page = 1
        await self._refresh()

    async def on_pg_prev(self, player, **kwargs):
        self.page = max(1, self.page - 1)
        await self._refresh()

    async def on_pg_next(self, player, **kwargs):
        self.page = min(self.total_pages, self.page + 1)
        await self._refresh()

    async def on_pg_last(self, player, **kwargs):
        self.page = self.total_pages
        await self._refresh()

    async def on_menu_toggle(self, player, **kwargs):
        self.menu_open = not self.menu_open
        await self._refresh()

    def make_menu_pick(self, key: str):
        async def handler(player, **kwargs):
            self.menu_result = key
            self.menu_open = False
            await self._refresh()
        return handler

    def make_acc_toggle(self, key: str):
        async def handler(player, **kwargs):
            if key in self.acc_open:
                self.acc_open.remove(key)
            else:
                self.acc_open.append(key)
            await self._refresh()
        return handler

    # --- 'more2' tab handlers ---
    async def on_q_submit(self, player, action=None, values=None, **kwargs):
        # entry submit posts the values dict; key is `entry_<view_id>__q`
        if values:
            for k, v in values.items():
                if k.endswith("__q"):
                    self.q_value = v
                    break
        await self._refresh()

    async def on_q_clear(self, player, **kwargs):
        self.q_value = ""
        await self._refresh()

    def make_crumb_nav(self, key: str, label: str):
        async def handler(player, **kwargs):
            self.crumb_current = label
            await self._refresh()
        return handler

    async def on_b_info_dismiss(self, player, **kwargs):
        self.banner_info = False
        await self._refresh()

    async def on_b_offline_dismiss(self, player, **kwargs):
        self.banner_off_dismiss = False
        await self._refresh()

    async def on_link_details(self, player, **kwargs):
        self.link_msg = "details link"
        await self.instance.chat(f"{CHAT} link 'Show details' clicked")
        await self._refresh()

    def make_tree_toggle(self, key: str):
        async def handler(player, **kwargs):
            if key in self.tree_open:
                self.tree_open.discard(key)
            else:
                self.tree_open.add(key)
            await self._refresh()
        return handler

    def make_tree_select(self, key: str):
        async def handler(player, **kwargs):
            self.tree_selected = key
            await self._refresh()
        return handler
