"""Edit a pool's `settings/apps.py` to keep a tmsm-managed block of addon entries.

Layout we write:

    APPS = {
        "default": [
            "pyplanet.apps.contrib.admin",
            ...

            # --- TrackManiaServerManager ---
            # "pyplanet.apps.tmsm.consoles",
            "pyplanet.apps.tmsm.system",

            # --- Community made ---
            # "pyplanet.apps.contrib.cup_manager",
        ]
    }

Rules:
  * The block is anchored by its group headers (`# --- ... ---`). No
    explicit start/end marker comments are written anymore.
  * Within the block, an existing line's comment state (commented = inactive,
    uncommented = active) is preserved when we rewrite it.
  * Newly-installed addons land in the block commented-out by default — the
    user activates by removing the leading `#`.
  * Legacy `# >>> tmsm-managed >>>` / `# <<< tmsm-managed <<<` markers are
    still recognised on read and stripped on next sync (self-migration).
"""
from __future__ import annotations

import re
from pathlib import Path

# The visible group headers double as block-detection anchors — we no longer
# emit explicit start/end marker comments. Keeping the names for back-compat
# with any external importers, but they're not written into apps.py anymore.
BLOCK_START = ""
BLOCK_END = ""

_GROUP_HEADERS = ("TrackManiaServerManager", "Community made", "other")

_HEADER_RE = re.compile(
    r"^[ \t]*#[ \t]*---[ \t]*(?:TrackManiaServerManager|Community made|other)[ \t]*---[ \t]*$"
)

# Match an entry inside the block: optional leading "# " then a quoted module name then comma.
# Accepts both single and double quotes.
_ENTRY_RE = re.compile(
    r"""^[ \t]*(\#[ \t]*)?["']([A-Za-z0-9_.]+)["'][ \t]*,?[ \t]*$""",
)

# Legacy markers we may still find in pools created before headers replaced them.
# Stripped on sync so old pools self-migrate.
_LEGACY_BLOCK_RE = re.compile(
    r"^[ \t]*#[ \t]*>>>[ \t]*tmsm-managed.*?^[ \t]*#[ \t]*<<<[ \t]*tmsm-managed[^\n]*\n?",
    re.DOTALL | re.MULTILINE,
)
_LEGACY_START_RE = re.compile(r"^[ \t]*#[ \t]*>>>[ \t]*tmsm-managed[^\n]*\n?", re.MULTILINE)
_LEGACY_END_RE   = re.compile(r"^[ \t]*#[ \t]*<<<[ \t]*tmsm-managed[^\n]*\n?", re.MULTILINE)

# Find the closing `]` of the default APPS list — handles the standard template.
# Accepts both single and double quotes for the "default" key.
_DEFAULT_CLOSE_RE = re.compile(
    r"""(["']default["']\s*:\s*\[[^\]]*?)(\n[ \t]*)\](\s*\})""",
    re.DOTALL,
)

# Stale fallback marker from previous buggy syncs — stripped on every sync to self-heal.
_FALLBACK_MSG_RE = re.compile(
    r"^[ \t]*#[ \t]*tmsm could not locate the APPS\['default'\] list to edit\.[ \t]*\n",
    re.MULTILINE,
)


def _find_block_span(text: str) -> tuple[int, int] | None:
    """Locate the contiguous tmsm-managed block by its group headers.

    A block is a run of lines starting at the first `# --- TrackManiaServerManager ---`
    (or `# --- Community made ---` / `# --- other ---`) header and continuing
    while subsequent lines are blank, another such header, or a quoted entry
    (with or without a leading `#` comment).

    Returns (start_offset, end_offset) of the slice to replace, or None if no
    block is present. The slice includes one trailing newline so callers can
    cleanly splice in a replacement.
    """
    lines = text.splitlines(keepends=True)
    # Compute cumulative offsets for slicing
    offsets = [0]
    for ln in lines:
        offsets.append(offsets[-1] + len(ln))

    # First, find the first header line.
    first_idx: int | None = None
    for i, ln in enumerate(lines):
        if _HEADER_RE.match(ln):
            first_idx = i
            break
    if first_idx is None:
        return None

    # Walk back over any blank lines that precede the header so we replace
    # the leading whitespace too.
    start_idx = first_idx
    while start_idx > 0 and lines[start_idx - 1].strip() == "":
        start_idx -= 1

    # Walk forward as long as the line is blank, a group header, or an entry.
    last_content_idx = first_idx  # last non-blank line that belongs to the block
    i = first_idx
    while i < len(lines):
        ln = lines[i]
        stripped = ln.strip()
        if stripped == "":
            i += 1
            continue
        if _HEADER_RE.match(ln) or _ENTRY_RE.match(ln):
            last_content_idx = i
            i += 1
            continue
        # Something else (e.g. closing `]`, code, unrelated comment): stop.
        break

    # End slice right after the last entry/header line — include its newline.
    end_idx = last_content_idx + 1
    return offsets[start_idx], offsets[end_idx]


def _parse_block_states(block_text: str) -> dict[str, bool]:
    """Return {module_name: active}. active = uncommented."""
    states: dict[str, bool] = {}
    for line in block_text.splitlines():
        m = _ENTRY_RE.match(line)
        if not m:
            continue
        commented = m.group(1) is not None
        module = m.group(2)
        states[module] = not commented
    return states


def _detect_quote(text: str) -> str:
    """Pick the quote style to match the surrounding file (defaults to single)."""
    single = text.count("'")
    double = text.count('"')
    return '"' if double > single else "'"


def _format_block(modules: list[str], states: dict[str, bool], indent: str, q: str) -> str:
    # Group entries by namespace so the block stays organized as it grows.
    tmsm_mods: list[str] = []
    git_mods: list[str] = []
    other_mods: list[str] = []
    for m in modules:
        if m.startswith("pyplanet.apps.tmsm."):
            tmsm_mods.append(m)
        elif m.startswith("pyplanet.apps.contrib."):
            git_mods.append(m)
        else:
            other_mods.append(m)

    lines: list[str] = []

    def _emit(header: str, mods: list[str]) -> None:
        if not mods:
            return
        if lines:
            lines.append("")  # blank line between groups
        lines.append(f"{indent}# --- {header} ---")
        for module in mods:
            active = states.get(module, False)  # default = commented
            prefix = "" if active else "# "
            lines.append(f"{indent}{prefix}{q}{module}{q},")

    _emit("TrackManiaServerManager", tmsm_mods)
    _emit("Community made", git_mods)
    _emit("other", other_mods)

    if not lines:
        return ""  # nothing to write
    # Two leading newlines so the block is visually separated from the
    # template's last entry by a blank line; one trailing newline keeps the
    # closing `]` flush on its own line.
    return "\n\n" + "\n".join(lines) + "\n"


def sync_apps_py(apps_py: Path, modules: list[str]) -> None:
    """Ensure `apps_py` contains a tmsm-managed block listing exactly `modules`.

    Preserves existing active/inactive state per module. Brand-new modules are
    inserted commented-out (inactive) so the pool doesn't auto-load them.
    """
    if not apps_py.is_file():
        return
    text = apps_py.read_text(encoding="utf-8")

    # Self-heal: remove stale fallback notes and legacy >>> / <<< markers
    # (those are no longer emitted; strip so detection only finds headers).
    text = _FALLBACK_MSG_RE.sub("", text)
    text = _LEGACY_START_RE.sub("", text)
    text = _LEGACY_END_RE.sub("", text)

    # Capture per-module state from either a header-style block or any
    # surviving legacy block fragment.
    span = _find_block_span(text)
    states: dict[str, bool] = {}
    if span is not None:
        states.update(_parse_block_states(text[span[0]:span[1]]))
    legacy = _LEGACY_BLOCK_RE.search(text)
    if legacy is not None:
        states.update(_parse_block_states(legacy.group(0)))
        # Remove the legacy block so we don't end up with duplicates.
        text = text[:legacy.start()] + text[legacy.end():]
        span = _find_block_span(text)  # offsets shifted

    quote = _detect_quote(text)

    # Drop any modules already present *outside* the managed block (e.g. they
    # were written by the template). Re-emitting them here would produce a
    # duplicate entry under the synced headers.
    outside = text
    if span is not None:
        outside = text[:span[0]] + text[span[1]:]
    pre_existing: set[str] = set()
    for line in outside.splitlines():
        em = _ENTRY_RE.match(line)
        if em:
            pre_existing.add(em.group(2))
    modules = [m for m in modules if m not in pre_existing]

    # Indent inside the default list — fall back to 8 spaces (template default).
    indent = "        "
    close = _DEFAULT_CLOSE_RE.search(text)
    if close:
        bracket_indent = close.group(2).lstrip("\n")
        indent = bracket_indent + "    "

    new_block = _format_block(modules, states, indent, quote)

    if span is not None:
        text = text[:span[0]] + new_block + text[span[1]:]
    elif close and new_block:
        text = text[:close.end(1)] + new_block + text[close.end(1):]
    elif new_block:
        # Last-resort fallback (idempotent: the notice is stripped next sync).
        text = text.rstrip() + (
            "\n\n# tmsm could not locate the APPS['default'] list to edit.\n"
        )

    apps_py.write_text(text, encoding="utf-8")


def remove_modules(apps_py: Path, to_remove: set[str]) -> None:
    """Drop the given modules from the tmsm-managed block entirely."""
    if not apps_py.is_file():
        return
    text = apps_py.read_text(encoding="utf-8")
    span = _find_block_span(text)
    if span is None:
        # Legacy block fallback
        legacy = _LEGACY_BLOCK_RE.search(text)
        if legacy is None:
            return
        block = legacy.group(0)
        kept = [ln for ln in block.splitlines()
                if not (_ENTRY_RE.match(ln) and _ENTRY_RE.match(ln).group(2) in to_remove)]  # type: ignore[union-attr]
        new_block = "\n".join(kept)
        if not new_block.endswith("\n"):
            new_block += "\n"
        text = text[:legacy.start()] + new_block + text[legacy.end():]
        apps_py.write_text(text, encoding="utf-8")
        return

    block = text[span[0]:span[1]]
    kept_lines: list[str] = []
    for line in block.splitlines():
        em = _ENTRY_RE.match(line)
        if em and em.group(2) in to_remove:
            continue
        kept_lines.append(line)
    new_block = "\n".join(kept_lines)
    if new_block and not new_block.endswith("\n"):
        new_block += "\n"
    text = text[:span[0]] + new_block + text[span[1]:]
    apps_py.write_text(text, encoding="utf-8")
