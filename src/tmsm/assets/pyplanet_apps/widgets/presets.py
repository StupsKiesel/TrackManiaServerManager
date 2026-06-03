"""Widget configuration presets — CSV-backed full snapshots.

A preset is a CSV file under ``presets/`` whose columns mirror the
``WidgetConfigGlobal`` table 1:1. One row per widget. Every cell is
required: presets are *full snapshots*, not deltas.

Header comments (lines starting with ``#``) carry preset metadata::

    # preset_key: arcade
    # label: Arcade layout
    # description: tight HUD for fast modes

Validation is split in two independent steps:

1. *Engine validation* — performed here when the preset is loaded.
   Checks shape, types, enums, value ranges. Presets that fail this
   step are still listed (with their warnings) but cannot be applied.

2. *Requestor validation* — performed by ``validate_for(required_keys)``.
   Returns the set of required widget keys the preset is missing, so
   callers (e.g. game-modes) can refuse to use it.

Apply semantics are deliberately additive:

* ``apply_global`` writes ``WidgetConfigGlobal`` rows for *exactly* the
  widgets listed. Others are left as-is.
* ``apply_runtime`` emits runtime overrides for *exactly* the widgets
  listed. Unlisted widgets are not auto-hidden — that's the caller's
  decision via its own runtime overrides.
"""
from __future__ import annotations

import csv
import datetime
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# Columns mirror WidgetConfigGlobal (minus surrogate id + updated_at).
PRESET_COLUMNS: tuple[str, ...] = (
    "widget_key",
    "x", "y", "w", "h",
    "hide_while_driving",
    "drive_mode",
    "state_all",
    "state_loading_map",
    "state_warmup",
    "state_pre_race",
    "state_in_race",
    "state_in_podium",
    "state_post_race",
    "group_key",
    "group_member_enabled",
    "group_priority",
    "group_order",
    "anim_dir",
    "anim_duration_ms",
    "anim_delay_ms",
    "allow_personal",
    "strip_prefer_top",
    "widget_disabled",
)

_FLOAT_COLS = ("x", "y", "w", "h")
_INT_COLS = ("group_priority", "group_order", "anim_duration_ms", "anim_delay_ms")
_BOOL_COLS = (
    "hide_while_driving",
    "state_all",
    "state_loading_map",
    "state_warmup",
    "state_pre_race",
    "state_in_race",
    "state_in_podium",
    "state_post_race",
    "group_member_enabled",
    "allow_personal",
    "strip_prefer_top",
    "widget_disabled",
)
_STATE_BOOL_COLS = (
    "state_all",
    "state_loading_map",
    "state_warmup",
    "state_pre_race",
    "state_in_race",
    "state_in_podium",
    "state_post_race",
)
_STR_COLS = ("widget_key", "drive_mode", "group_key", "anim_dir")

_DRIVE_MODE_ENUM = {"fixed", "hide_while_driving", "only_shown_while_driving"}
_ANIM_DIR_ENUM = {"none", "left", "right", "up", "down"}

_WIDGET_KEY_RE = re.compile(r"^[A-Za-z0-9_./\-]+$")
_PRESET_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _parse_bool(raw: str) -> bool | None:
    s = str(raw).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return None


def _fmt_bool(v: Any) -> str:
    return "true" if bool(v) else "false"


def _fmt_float(v: Any) -> str:
    try:
        return f"{float(v):.4f}".rstrip("0").rstrip(".") or "0"
    except (TypeError, ValueError):
        return "0"


@dataclass
class WidgetPresetEntry:
    widget_key: str
    pos: dict[str, float] = field(default_factory=dict)
    behavior: dict[str, Any] = field(default_factory=dict)


@dataclass
class WidgetPreset:
    key: str
    label: str
    description: str
    source_path: Path | None
    entries: dict[str, WidgetPresetEntry] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.warnings and bool(self.entries)

    @property
    def widget_keys(self) -> set[str]:
        return set(self.entries.keys())


@dataclass
class ValidationResult:
    ok: bool
    missing_keys: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.ok


# ---- IO --------------------------------------------------------------

_META_RE = re.compile(r"^#\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$")


def _parse_row(raw: dict[str, str], lineno: int) -> tuple[WidgetPresetEntry | None, list[str]]:
    warns: list[str] = []
    cleaned: dict[str, Any] = {}
    for col in PRESET_COLUMNS:
        if col not in raw or raw[col] is None or str(raw[col]).strip() == "":
            warns.append(f"line {lineno}: missing required cell '{col}'")
            return None, warns
        cleaned[col] = str(raw[col]).strip()

    key = cleaned["widget_key"]
    if not _WIDGET_KEY_RE.match(key):
        warns.append(f"line {lineno}: invalid widget_key '{key}'")
        return None, warns

    pos: dict[str, float] = {}
    for col in _FLOAT_COLS:
        try:
            pos[col] = float(cleaned[col])
        except ValueError:
            warns.append(f"line {lineno}: '{col}' not a number: {cleaned[col]!r}")
            return None, warns

    behavior: dict[str, Any] = {}
    for col in _BOOL_COLS:
        v = _parse_bool(cleaned[col])
        if v is None:
            warns.append(f"line {lineno}: '{col}' not a boolean: {cleaned[col]!r}")
            return None, warns
        if col not in _STATE_BOOL_COLS:
            behavior[col] = v
    for col in _INT_COLS:
        try:
            behavior[col] = int(cleaned[col])
        except ValueError:
            warns.append(f"line {lineno}: '{col}' not an integer: {cleaned[col]!r}")
            return None, warns

    drive = cleaned["drive_mode"].lower()
    if drive not in _DRIVE_MODE_ENUM:
        warns.append(f"line {lineno}: invalid drive_mode '{cleaned['drive_mode']}'")
        return None, warns
    behavior["drive_mode"] = drive

    anim_dir = cleaned["anim_dir"].lower()
    if anim_dir not in _ANIM_DIR_ENUM:
        warns.append(f"line {lineno}: invalid anim_dir '{cleaned['anim_dir']}'")
        return None, warns
    behavior["anim_dir"] = anim_dir

    behavior["group_key"] = cleaned["group_key"]

    state_modes: list[str] = []
    if _parse_bool(cleaned["state_all"]):
        state_modes = ["all"]
    else:
        for col in _STATE_BOOL_COLS:
            if col == "state_all":
                continue
            if _parse_bool(cleaned[col]):
                state_modes.append(col[len("state_"):])
        if not state_modes:
            state_modes = ["all"]
    behavior["state_modes"] = state_modes

    return WidgetPresetEntry(widget_key=key, pos=pos, behavior=behavior), warns


def parse_preset_text(text: str, source_path: Path | None,
                       fallback_key: str) -> WidgetPreset:
    meta: dict[str, str] = {}
    data_lines: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            m = _META_RE.match(stripped)
            if m:
                meta[m.group(1).lower()] = m.group(2)
            continue
        if not stripped:
            continue
        data_lines.append(raw_line)

    preset_key = (meta.get("preset_key") or fallback_key).strip()
    label = (meta.get("label") or preset_key).strip()
    description = (meta.get("description") or "").strip()
    preset = WidgetPreset(
        key=preset_key, label=label, description=description,
        source_path=source_path,
    )

    if not _PRESET_KEY_RE.match(preset_key):
        preset.warnings.append(f"invalid preset_key '{preset_key}'")
        return preset

    if not data_lines:
        preset.warnings.append("preset is empty")
        return preset

    reader = csv.DictReader(io.StringIO("\n".join(data_lines)))
    if not reader.fieldnames:
        preset.warnings.append("missing header row")
        return preset
    missing_cols = [c for c in PRESET_COLUMNS if c not in reader.fieldnames]
    if missing_cols:
        preset.warnings.append(f"header missing columns: {', '.join(missing_cols)}")
        return preset

    lineno = 1  # header is row 1 of the CSV body
    seen: set[str] = set()
    for row in reader:
        lineno += 1
        entry, warns = _parse_row(row, lineno)
        preset.warnings.extend(warns)
        if entry is None:
            continue
        if entry.widget_key in seen:
            preset.warnings.append(f"line {lineno}: duplicate widget_key '{entry.widget_key}'")
            continue
        seen.add(entry.widget_key)
        preset.entries[entry.widget_key] = entry

    if not preset.entries and not preset.warnings:
        preset.warnings.append("no valid rows")
    return preset


def load_preset(path: Path) -> WidgetPreset:
    fallback = path.stem
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.exception("widgets.presets: failed to read %s", path)
        return WidgetPreset(
            key=fallback, label=fallback, description="",
            source_path=path,
            warnings=[f"read error: {exc}"],
        )
    return parse_preset_text(text, path, fallback)


def serialize_preset(preset: WidgetPreset) -> str:
    """Render a preset back into the canonical CSV-with-metadata format."""
    out = io.StringIO()
    out.write(f"# preset_key: {preset.key}\n")
    out.write(f"# label: {preset.label}\n")
    if preset.description:
        out.write(f"# description: {preset.description}\n")
    out.write(f"# generated_at: {datetime.datetime.utcnow().isoformat(timespec='seconds')}Z\n")
    writer = csv.DictWriter(out, fieldnames=list(PRESET_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for key in sorted(preset.entries.keys()):
        entry = preset.entries[key]
        row: dict[str, str] = {"widget_key": key}
        for col in _FLOAT_COLS:
            row[col] = _fmt_float(entry.pos.get(col, 0.0))
        modes = set(entry.behavior.get("state_modes") or ["all"])
        all_mode = "all" in modes
        for col in _STATE_BOOL_COLS:
            mode_name = col[len("state_"):]
            if col == "state_all":
                row[col] = _fmt_bool(all_mode)
            else:
                row[col] = _fmt_bool(False if all_mode else (mode_name in modes))
        for col in _BOOL_COLS:
            if col in _STATE_BOOL_COLS:
                continue
            row[col] = _fmt_bool(bool(entry.behavior.get(col, False)))
        for col in _INT_COLS:
            try:
                row[col] = str(int(entry.behavior.get(col, 0) or 0))
            except (TypeError, ValueError):
                row[col] = "0"
        row["drive_mode"] = str(entry.behavior.get("drive_mode") or "fixed")
        row["anim_dir"] = str(entry.behavior.get("anim_dir") or "none")
        row["group_key"] = str(entry.behavior.get("group_key") or "")
        writer.writerow(row)
    return out.getvalue()


def build_preset_from_snapshot(
    key: str,
    label: str,
    description: str,
    snapshot: Iterable[tuple[str, dict[str, float], dict[str, Any]]],
) -> WidgetPreset:
    """Build a :class:`WidgetPreset` from an iterable of
    ``(widget_key, position, behavior)`` triples."""
    preset = WidgetPreset(
        key=key.strip(), label=label.strip() or key.strip(),
        description=description.strip(), source_path=None,
    )
    for widget_key, pos, behavior in snapshot:
        wk = str(widget_key).strip()
        if not wk:
            continue
        entry_pos = {c: float(pos.get(c, 0.0) or 0.0) for c in _FLOAT_COLS}
        entry_beh: dict[str, Any] = {}
        for col in _BOOL_COLS:
            if col in _STATE_BOOL_COLS:
                continue
            entry_beh[col] = bool(behavior.get(col, False))
        for col in _INT_COLS:
            try:
                entry_beh[col] = int(behavior.get(col, 0) or 0)
            except (TypeError, ValueError):
                entry_beh[col] = 0
        dm = str(behavior.get("drive_mode") or "fixed").lower()
        entry_beh["drive_mode"] = dm if dm in _DRIVE_MODE_ENUM else "fixed"
        ad = str(behavior.get("anim_dir") or "none").lower()
        entry_beh["anim_dir"] = ad if ad in _ANIM_DIR_ENUM else "none"
        entry_beh["group_key"] = str(behavior.get("group_key") or "")
        modes = behavior.get("state_modes") or ["all"]
        if isinstance(modes, str):
            modes = [modes]
        entry_beh["state_modes"] = list(modes)
        preset.entries[wk] = WidgetPresetEntry(
            widget_key=wk, pos=entry_pos, behavior=entry_beh,
        )
    return preset


# ---- registry --------------------------------------------------------


class PresetRegistry:
    """In-memory registry of CSV-backed presets under a directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._by_key: dict[str, WidgetPreset] = {}

    def ensure_dir(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception("widgets.presets: cannot create %s", self.root)

    def reload(self) -> None:
        self._by_key.clear()
        self.ensure_dir()
        if not self.root.is_dir():
            return
        for path in sorted(self.root.glob("*.csv")):
            preset = load_preset(path)
            if preset.key in self._by_key:
                logger.warning(
                    "widgets.presets: duplicate preset_key '%s' (file %s) — ignoring",
                    preset.key, path,
                )
                continue
            self._by_key[preset.key] = preset
        logger.info("widgets.presets: loaded %d preset(s) from %s",
                    len(self._by_key), self.root)

    def list(self) -> list[WidgetPreset]:
        return sorted(self._by_key.values(), key=lambda p: p.key)

    def get(self, key: str) -> WidgetPreset | None:
        return self._by_key.get(str(key or "").strip())

    def path_for(self, key: str) -> Path:
        return self.root / f"{key}.csv"

    def save(self, preset: WidgetPreset, *, overwrite: bool = False) -> Path:
        if not _PRESET_KEY_RE.match(preset.key):
            raise ValueError(f"invalid preset_key '{preset.key}'")
        self.ensure_dir()
        path = self.path_for(preset.key)
        if path.exists() and not overwrite:
            raise FileExistsError(str(path))
        text = serialize_preset(preset)
        path.write_text(text, encoding="utf-8")
        preset.source_path = path
        self._by_key[preset.key] = preset
        return path

    def delete(self, key: str) -> bool:
        preset = self._by_key.pop(key, None)
        if preset is None:
            return False
        path = preset.source_path or self.path_for(key)
        try:
            if path.exists():
                path.unlink()
        except OSError:
            logger.exception("widgets.presets: cannot delete %s", path)
            return False
        return True

    def validate_for(self, preset: WidgetPreset,
                     required_keys: Iterable[str]) -> ValidationResult:
        if not preset.ok:
            return ValidationResult(ok=False, missing_keys=list(required_keys))
        missing = sorted(set(required_keys) - preset.widget_keys)
        return ValidationResult(ok=not missing, missing_keys=missing)


def default_presets_dir() -> Path:
    """Directory shipped alongside this module."""
    return Path(__file__).resolve().parent / "presets"
