"""Plain-text config editor for instance files (dedicated_cfg, matchsettings, etc.)."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, Label, ListItem, ListView, Static, TextArea


def _read_clipboard() -> str | None:
    """Best-effort read of the system clipboard. Returns None on failure."""
    # WSL: use Windows clipboard via powershell.exe
    if shutil.which("powershell.exe"):
        try:
            out = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                capture_output=True, text=True, timeout=3,
            )
            if out.returncode == 0:
                # PowerShell tacks on a trailing CRLF; strip a single one.
                text = out.stdout
                if text.endswith("\r\n"):
                    text = text[:-2]
                elif text.endswith("\n"):
                    text = text[:-1]
                # Normalize CRLF -> LF for editor consistency.
                return text.replace("\r\n", "\n")
        except (OSError, subprocess.TimeoutExpired):
            pass
    # Wayland
    if shutil.which("wl-paste"):
        try:
            out = subprocess.run(["wl-paste", "--no-newline"],
                                 capture_output=True, text=True, timeout=3)
            if out.returncode == 0:
                return out.stdout
        except (OSError, subprocess.TimeoutExpired):
            pass
    # X11
    for tool, args in (("xclip", ["xclip", "-selection", "clipboard", "-o"]),
                       ("xsel", ["xsel", "--clipboard", "--output"])):
        if shutil.which(tool):
            try:
                out = subprocess.run(args, capture_output=True, text=True, timeout=3)
                if out.returncode == 0:
                    return out.stdout
            except (OSError, subprocess.TimeoutExpired):
                pass
    # macOS
    if shutil.which("pbpaste"):
        try:
            out = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=3)
            if out.returncode == 0:
                return out.stdout
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


class FilePickerScreen(ModalScreen[Path | None]):
    """Pick one file from a list of (label, path) tuples."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    FilePickerScreen { align: center middle; }
    #picker-box {
        width: 70;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #picker-title { padding-bottom: 1; }
    ListView { height: auto; max-height: 16; }
    """

    def __init__(self, title: str, files: list[tuple[str, Path]]) -> None:
        super().__init__()
        self.title_text = title
        self.files = files

    def compose(self) -> ComposeResult:
        with Container(id="picker-box"):
            yield Label(f"[b]{self.title_text}[/b]", id="picker-title")
            items = [
                ListItem(Label(label), id=f"f-{i}")
                for i, (label, _) in enumerate(self.files)
            ]
            yield ListView(*items, id="picker-list")

    def on_mount(self) -> None:
        self.query_one(ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        if not item_id.startswith("f-"):
            return
        idx = int(item_id[2:])
        self.dismiss(self.files[idx][1])

    def action_cancel(self) -> None:
        self.dismiss(None)


class EditScreen(Screen):
    """Edit a single text file. Ctrl+S saves, Esc closes (with confirm if dirty)."""

    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("ctrl+r", "reload", "Reload from disk"),
        Binding("ctrl+v", "paste_clipboard", "Paste"),
        Binding("escape", "close", "Close"),
    ]

    DEFAULT_CSS = """
    #edit-header { padding: 0 1; }
    #edit-status { padding: 0 1; color: $text-muted; }
    TextArea { height: 1fr; }
    #edit-buttons { height: auto; padding: 1 1 0 1; }
    Button { margin-right: 1; }
    """

    def __init__(self, path: Path, label: str | None = None) -> None:
        super().__init__()
        self.path = path
        self.label = label or path.name
        self._dirty = False
        self._original = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(f"[b]{self.label}[/b]\n[dim]{self.path}[/dim]", id="edit-header")
        text = ""
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as e:
            text = f"# ERROR reading {self.path}: {e}\n"
        self._original = text
        ta = TextArea.code_editor(text, language=self._language(), id="editor")
        ta.show_line_numbers = True
        yield ta
        yield Static("Clean", id="edit-status")
        with Horizontal(id="edit-buttons"):
            yield Button("Save (Ctrl+S)", id="btn-save", variant="primary")
            yield Button("Reload (Ctrl+R)", id="btn-reload")
            yield Button("Close (Esc)", id="btn-close")
        yield Footer()

    def _language(self) -> str | None:
        suffix = self.path.suffix.lower()
        if suffix in (".txt", ".xml"):
            return "xml" if self._looks_xml() else None
        if suffix == ".py":
            return "python"
        if suffix in (".toml",):
            return "toml"
        if suffix in (".json",):
            return "json"
        if suffix in (".yml", ".yaml"):
            return "yaml"
        if suffix in (".ini", ".cnf", ".conf"):
            return None
        return None

    def _looks_xml(self) -> bool:
        try:
            head = self.path.read_text(encoding="utf-8", errors="ignore")[:200].lstrip()
            return head.startswith("<?xml") or head.startswith("<")
        except OSError:
            return False

    def on_mount(self) -> None:
        self.query_one(TextArea).focus()

    def on_text_area_changed(self, _event: TextArea.Changed) -> None:
        current = self.query_one(TextArea).text
        was_dirty = self._dirty
        self._dirty = current != self._original
        if self._dirty != was_dirty:
            self.query_one("#edit-status", Static).update(
                "[yellow]Modified — unsaved changes[/yellow]" if self._dirty else "Clean"
            )

    def action_save(self) -> None:
        text = self.query_one(TextArea).text
        try:
            self.path.write_text(text, encoding="utf-8")
        except OSError as e:
            self.notify(f"Save failed: {e}", severity="error")
            return
        self._original = text
        self._dirty = False
        self.query_one("#edit-status", Static).update("[green]Saved[/green]")
        self.notify(f"Saved {self.path.name}")

    def action_reload(self) -> None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as e:
            self.notify(f"Reload failed: {e}", severity="error")
            return
        self.query_one(TextArea).text = text
        self._original = text
        self._dirty = False
        self.query_one("#edit-status", Static).update("Clean")

    def action_paste_clipboard(self) -> None:
        text = _read_clipboard()
        if text is None:
            self.notify(
                "Clipboard unavailable. Install xclip/wl-paste, or run inside WSL.",
                severity="warning",
            )
            return
        if not text:
            self.notify("Clipboard is empty.", severity="information")
            return
        ta = self.query_one(TextArea)
        ta.insert(text)
        ta.focus()

    def action_close(self) -> None:
        if not self._dirty:
            self.dismiss(None)
            return
        from .confirm import ConfirmScreen
        self.app.push_screen(
            ConfirmScreen("Discard unsaved changes?"),
            lambda yes: self.dismiss(None) if yes else None,
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save":
            self.action_save()
        elif event.button.id == "btn-reload":
            self.action_reload()
        elif event.button.id == "btn-close":
            self.action_close()
