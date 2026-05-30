"""Demo view — uses every v1 widget macro."""
from pyplanet.apps.tmsm.ui import Audience, BaseView


class DemoView(BaseView):
    template_name = "ui_demo/demo.xml"
    audience = Audience.master_admins()
    breadcrumbs = [{"key": "hub", "label": "Hub"}]

    def __init__(self, app):
        super().__init__(app)
        # Connect all signals emitted by widgets in the template.
        self.connect("primary_btn",  app.on_click)
        self.connect("danger_btn",   app.on_click)
        self.connect("ghost_btn",    app.on_click)
        self.connect("success_btn",  app.on_click)
        self.connect("warning_btn",  app.on_click)
        self.connect("reload_btn",   app.on_click)
        self.connect("save_btn",     app.on_click)
        self.connect("cog_btn",      app.on_click)
        self.connect("trash_btn",    app.on_click)
        self.connect("toggle_cb",    app.on_toggle)
        self.connect("submit",       app.on_submit)
        # new grid + form demo signals
        for sig in ("new", "open", "copy", "del"):
            self.connect(sig, app.on_click)
        self.connect("f_public", app.on_public_toggle)
        # tab + scroll signals (server-driven state in app)
        for key in ("buttons", "inputs", "lists", "scroll", "misc", "advanced", "data", "layout", "dialogs", "more", "more2", "more3"):
            self.connect(f"main__tab__{key}", app.make_tab_handler(key))
        self.connect("demo_scroll__scroll_up",   app.on_scroll_up)
        self.connect("demo_scroll__scroll_down", app.on_scroll_down)
        for v in ("easy", "normal", "hard", "insane"):
            self.connect(f"difficulty__set__{v}",
                         app.make_radio_handler("radio_choice", v))
        # advanced tab
        self.connect("region__toggle", app.on_combo_toggle)
        for v in ("eu", "na", "as", "oc"):
            self.connect(f"region__pick__{v}", app.make_combo_pick(v))
        self.connect("volume__inc", app.on_stepper_inc)
        self.connect("volume__dec", app.on_stepper_dec)
        self.connect("open_modal",     app.on_modal_open)
        self.connect("confirm__close", app.on_modal_close)
        self.connect("confirm_ok",     app.on_modal_confirm)
        self.connect("confirm_cancel", app.on_modal_close)
        self.connect("show_toast",     app.on_show_toast)
        for v in ("speed", "endurance", "tech", "dirt"):
            self.connect(f"filters__remove__{v}", app.make_tag_remove(v))
        # data table sort signals
        for k in ("login", "country", "score", "wins"):
            self.connect(f"players_tbl__sort__{k}", app.make_table_sort(k))
        # layout tab
        self.connect("stack_next", app.on_stack_next)
        self.connect("stack_prev", app.on_stack_prev)

        # dialogs tab
        self.connect("open_info",     app.on_open_info)
        self.connect("open_error",    app.on_open_error)
        self.connect("open_confirm",  app.on_open_confirm)
        self.connect("open_confirm_action", app.on_open_confirm_action)
        self.connect("open_confirm_save",    app.on_open_confirm_save)
        self.connect("open_confirm_publish", app.on_open_confirm_publish)
        self.connect("open_confirm_reveal",  app.on_open_confirm_reveal)
        self.connect("open_dlg_savequit",    app.on_open_dlg_savequit)
        self.connect("img_clicked",          app.on_img_clicked)
        self.connect("info__ok",      app.on_info_ok)
        self.connect("error__ok",     app.on_error_ok)
        self.connect("confirm_dlg__ok",     app.on_confirm_ok)
        self.connect("confirm_dlg__cancel", app.on_confirm_cancel)
        self.connect("confirm_action__ok",     app.on_confirm_action_ok)
        self.connect("confirm_action__cancel", app.on_confirm_action_cancel)
        self.connect("confirm_save__ok",       app.on_confirm_save_ok)
        self.connect("confirm_save__cancel",   app.on_confirm_save_cancel)
        self.connect("confirm_publish__ok",    app.on_confirm_publish_ok)
        self.connect("confirm_publish__cancel", app.on_confirm_publish_cancel)
        self.connect("confirm_reveal__ok",     app.on_confirm_reveal_ok)
        self.connect("confirm_reveal__cancel", app.on_confirm_reveal_cancel)
        self.connect("dlg_savequit__ok",       app.on_dlg_savequit_ok)
        self.connect("dlg_savequit__cancel",   app.on_dlg_savequit_cancel)
        self.connect("dlg_savequit__extra",    app.on_dlg_savequit_extra)

        # 'more' tab
        self.connect("sw_notify__toggle",  app.on_sw_notify_toggle)
        self.connect("sw_replays__toggle", app.on_sw_replays_toggle)
        self.connect("sw_public__toggle",  app.on_sw_public_toggle)
        # sliders: dec/inc + N segment values
        self.connect("vol__dec", app.make_slider_step("vol", -10, 0, 100))
        self.connect("vol__inc", app.make_slider_step("vol",  10, 0, 100))
        for n in range(0, 101, 10):
            self.connect(f"vol__set__{n}", app.make_slider_set("vol", n))
        self.connect("tmo__dec", app.make_slider_step("tmo", -5, 0, 60))
        self.connect("tmo__inc", app.make_slider_step("tmo",  5, 0, 60))
        for n in range(0, 61, 5):
            self.connect(f"tmo__set__{n}", app.make_slider_set("tmo", n))
        # pagination
        self.connect("pg__first", app.on_pg_first)
        self.connect("pg__prev",  app.on_pg_prev)
        self.connect("pg__next",  app.on_pg_next)
        self.connect("pg__last",  app.on_pg_last)
        for n in range(1, 13):
            self.connect(f"pg__page__{n}", app.make_page_goto(n))
        # menu
        self.connect("menu_toggle",       app.on_menu_toggle)
        self.connect("menu__toggle",      app.on_menu_toggle)
        for k in ("rename", "duplicate", "export", "delete"):
            self.connect(f"menu__pick__{k}", app.make_menu_pick(k))
        # accordion
        for k in ("general", "network", "advanced"):
            self.connect(f"acc__toggle__{k}", app.make_acc_toggle(k))

        # 'more2' tab
        self.connect("q",         app.on_q_submit)
        self.connect("q__clear",  app.on_q_clear)
        self.connect("b_info__dismiss",    app.on_b_info_dismiss)
        self.connect("b_offline__dismiss", app.on_b_offline_dismiss)
        for c in (("home", "Server"), ("maps", "Maps"), ("tracks", "Tracks")):
            self.connect(f"crumbs__nav__{c[0]}", app.make_crumb_nav(c[0], c[1]))
        # link + tree_view
        self.connect("lk_details", app.on_link_details)
        # 'more3' tab (tree_view)
        for k in self._all_tree_keys():
            self.connect(f"tv__toggle__{k}", app.make_tree_toggle(k))
            self.connect(f"tv__select__{k}", app.make_tree_select(k))

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx["checked"] = self.app.checkbox_state
        ctx["public_checked"] = self.app.public_state
        ctx["text_value"] = self.app.text_value
        ctx["paragraph"] = (
            "The tmsm.ui v1 framework wraps PyPlanet's manialink primitives in a "
            "Qt-style, declarative widget API. Use macros for buttons, forms, "
            "lists and grids; connect signals from Python with self.connect(...)."
        )
        ctx["players"] = [
            {"login": "alice",   "score": 1850, "country": "DE"},
            {"login": "bob",     "score": 1620, "country": "FR"},
            {"login": "charlie", "score": 1490, "country": "US"},
            {"login": "dora",    "score": 1310, "country": "JP"},
        ]
        ctx["tools"] = [
            {"id": "new",   "label": "New",   "icon": "plus",   "variant": "primary"},
            {"id": "open",  "label": "Open",  "icon": "edit",   "variant": "ghost"},
            {"id": "copy",  "label": "Copy",  "icon": "save",   "variant": "ghost"},
            {"id": "del",   "label": "Del",   "icon": "trash",  "variant": "danger"},
        ]
        ctx["form_rows"] = [
            ("name",   {"kind": "entry", "name": "f_name",   "value": "tmsm"}),
            ("port",   {"kind": "entry", "name": "f_port",   "value": "2350"}),
            ("public", {"kind": "check", "name": "f_public", "value": True}),
        ]
        # tabs + scroll
        ctx["main_tabs"] = [
            {"key": "buttons",  "label": "Buttons"},
            {"key": "inputs",   "label": "Inputs"},
            {"key": "lists",    "label": "Lists"},
            {"key": "scroll",   "label": "Scroll"},
            {"key": "misc",     "label": "Misc"},
            {"key": "advanced", "label": "Advanced"},
            {"key": "data",     "label": "Data"},
            {"key": "layout",   "label": "Layout"},
            {"key": "dialogs",  "label": "Dialogs"},
            {"key": "more",     "label": "More"},
            {"key": "more2",    "label": "More 2"},
            {"key": "more3",    "label": "More 3"},
        ]
        ctx["active_tab"] = self.app.active_tab
        ctx["scroll_items"] = [f"row #{i:02d} — entry value {i}" for i in range(30)]
        ctx["scroll_content_h"] = 30 * 5 + 2  # rows * row_h + top padding
        ctx["scroll_offset"] = self.app.scroll_offset
        ctx["radio_options"] = [
            ("easy",   "Easy"),
            ("normal", "Normal"),
            ("hard",   "Hard"),
            ("insane", "Insane"),
        ]
        ctx["radio_choice"] = self.app.radio_choice
        # advanced tab
        ctx["region_options"] = [
            ("eu", "Europe"),
            ("na", "North America"),
            ("as", "Asia"),
            ("oc", "Oceania"),
        ]
        ctx["region_value"] = self.app.combo_value
        ctx["region_open"]  = self.app.combo_open
        ctx["stepper_value"] = self.app.stepper_value
        ctx["modal_open"]   = self.app.modal_open
        ctx["toast_msg"]    = self.app.toast_msg
        ctx["toast_variant"] = self.app.toast_variant
        ctx["tags"]         = self.app.tags
        # data table
        ctx["table_columns"] = [
            {"key": "login",   "label": "Player",  "w": 50, "align": "left"},
            {"key": "country", "label": "Country", "w": 20, "align": "center", "color": "4af"},
            {"key": "score",   "label": "Score",   "w": 30, "align": "right", "color": "ff0"},
            {"key": "wins",    "label": "Wins",    "w": 30, "align": "right", "color": "0f8"},
        ]
        raw_rows = [
            {"login": "alice",   "country": "DE", "score": 1850, "wins": 42},
            {"login": "bob",     "country": "FR", "score": 1620, "wins": 31},
            {"login": "charlie", "country": "US", "score": 1490, "wins": 27},
            {"login": "dora",    "country": "JP", "score": 1310, "wins": 19},
            {"login": "eli",     "country": "BR", "score": 1190, "wins": 15},
            {"login": "frank",   "country": "CA", "score": 1080, "wins": 12},
        ]
        sk = self.app.table_sort_key
        sd = self.app.table_sort_dir
        ctx["table_rows"] = sorted(raw_rows, key=lambda r: r.get(sk, ""),
                                   reverse=(sd == "desc"))
        ctx["table_sort_key"] = sk
        ctx["table_sort_dir"] = sd
        # layout tab
        ctx["hlist_items"] = [
            {"label": "Home",   "icon": "home"},
            {"label": "Search", "icon": "search"},
            {"label": "Stats",  "icon": "list"},
            {"label": "Setup",  "icon": "cog"},
        ]
        ctx["stack_step"] = self.app.stack_step
        # dialogs tab
        ctx["info_open"]     = self.app.info_open
        ctx["error_open"]    = self.app.error_open
        ctx["confirm_open"]  = self.app.confirm_open
        ctx["confirm_action_open"] = self.app.confirm_action_open
        ctx["confirm_save_open"]    = self.app.confirm_save_open
        ctx["confirm_publish_open"] = self.app.confirm_publish_open
        ctx["confirm_reveal_open"]  = self.app.confirm_reveal_open
        ctx["dlg_savequit_open"]    = self.app.dlg_savequit_open
        ctx["image_msg"]            = self.app.image_msg
        # 'more' tab
        ctx["sw_notify"]     = self.app.sw_notify
        ctx["sw_replays"]    = self.app.sw_replays
        ctx["sw_public"]     = self.app.sw_public
        ctx["vol"]           = self.app.vol
        ctx["tmo"]           = self.app.tmo
        ctx["page"]          = self.app.page
        ctx["total_pages"]   = self.app.total_pages
        ctx["menu_open"]     = self.app.menu_open
        ctx["menu_result"]   = self.app.menu_result
        ctx["menu_items"] = [
            {"key": "rename",    "label": "Rename",    "icon": "edit"},
            {"key": "duplicate", "label": "Duplicate", "icon": "plus"},
            {"separator": True},
            {"key": "export",    "label": "Export",    "icon": "save"},
            {"key": "delete",    "label": "Delete",    "icon": "trash",
             "variant": "danger"},
        ]
        ctx["acc_open"]      = self.app.acc_open
        ctx["acc_sections"] = [
            {"key": "general",  "title": "General",  "icon": "cog"},
            {"key": "network",  "title": "Network",  "icon": "info"},
            {"key": "advanced", "title": "Advanced", "icon": "warning"},
        ]
        # 'more2' tab
        ctx["q_value"]            = self.app.q_value
        ctx["crumb_current"]      = self.app.crumb_current
        ctx["crumb_items"] = [
            {"key": "home",   "label": "Server"},
            {"key": "maps",   "label": "Maps"},
            {"key": "tracks", "label": "Tracks"},
            {"key": "_cur",   "label": self.app.crumb_current},
        ]
        ctx["banner_info"]        = self.app.banner_info
        ctx["banner_off_dismiss"] = self.app.banner_off_dismiss
        ctx["confirm_result"] = self.app.confirm_result
        ctx["link_msg"] = self.app.link_msg
        ctx["tree_selected"] = self.app.tree_selected
        ctx["tree_nodes"] = self._flatten_tree()
        # current map thumbnail
        ctx["thumb_uid"] = self.app.thumb_uid
        ctx["thumb_name"] = self.app.thumb_name
        ctx["thumb_port"] = self.app.thumb_port
        return ctx

    # ---- tree data (static demo hierarchy) -----------------------------

    _TREE = [
        {"key": "server", "label": "Server", "icon": "cog", "children": [
            {"key": "server/general", "label": "General", "icon": "info"},
            {"key": "server/network", "label": "Network", "icon": "info"},
        ]},
        {"key": "maps", "label": "Maps", "icon": "folder", "children": [
            {"key": "maps/summer", "label": "Summer pack", "icon": "folder",
             "children": [
                {"key": "maps/summer/a01", "label": "A01 - Dunes", "icon": "file"},
                {"key": "maps/summer/a02", "label": "A02 - Cliffs", "icon": "file"},
             ]},
            {"key": "maps/winter", "label": "Winter pack", "icon": "folder",
             "children": [
                {"key": "maps/winter/b01", "label": "B01 - Ice", "icon": "file"},
             ]},
        ]},
        {"key": "players", "label": "Players", "icon": "user"},
    ]

    def _all_tree_keys(self):
        out = []

        def walk(nodes):
            for n in nodes:
                out.append(n["key"])
                if n.get("children"):
                    walk(n["children"])

        walk(self._TREE)
        return out

    def _flatten_tree(self):
        open_keys = self.app.tree_open
        selected = self.app.tree_selected
        out = []

        def walk(nodes, depth):
            for n in nodes:
                has_kids = bool(n.get("children"))
                is_open = n["key"] in open_keys
                out.append({
                    "key": n["key"],
                    "label": n["label"],
                    "depth": depth,
                    "has_children": has_kids,
                    "is_open": is_open,
                    "icon": n.get("icon"),
                    "selected": n["key"] == selected,
                })
                if has_kids and is_open:
                    walk(n["children"], depth + 1)

        walk(self._TREE, 0)
        return out
