"""Edit a pool's `settings/apps.py` to keep a tmsm-managed block of addon entries.

Layout we write:

    APPS = {
        "default": [
            "pyplanet.apps.contrib.admin",
            ...
            # >>> tmsm-managed (uncomment a line to activate the addon) >>>
            # "pyplanet.apps.contrib.cup_manager",
            "pyplanet.apps.tmsm.my_widget",
            # <<< tmsm-managed <<<
        ]
    }

Rules:
  * On sync we never change entries the user wrote themselves outside the block.
  * Within the block, an existing line's comment state (commented = inactive,
    uncommented = active) is preserved when we rewrite it.
  * Newly-installed addons land in the block commented-out by default — the
    user activates by removing the leading `#`.
"""
from __future__ import annotations

import re
from pathlib import Path

BLOCK_START = "# >>> tmsm-managed (uncomment a line to activate the addon) >>>"
BLOCK_END   = "# <<< tmsm-managed <<<"

_BLOCK_RE = re.compile(
    r"^[ \t]*#[ \t]*>>>[ \t]*tmsm-managed.*?^[ \t]*#[ \t]*<<<[ \t]*tmsm-managed[^\n]*\n?",
    re.DOTALL | re.MULTILINE,
)

# Match an entry inside the block: optional leading "# " then a quoted module name then comma.
# Accepts both single and double quotes.
_ENTRY_RE = re.compile(
    r"""^[ \t]*(\#[ \t]*)?["']([A-Za-z0-9_.]+)["'][ \t]*,?[ \t]*$""",
)

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
    lines = [f"{indent}{BLOCK_START}"]
    for module in modules:
        active = states.get(module, False)  # default = commented
        prefix = "" if active else "# "
        lines.append(f"{indent}{prefix}{q}{module}{q},")
    lines.append(f"{indent}{BLOCK_END}")
    return "\n".join(lines)


def sync_apps_py(apps_py: Path, modules: list[str]) -> None:
    """Ensure `apps_py` contains a tmsm-managed block listing exactly `modules`.

    Preserves existing active/inactive state per module. Brand-new modules are
    inserted commented-out (inactive) so the pool doesn't auto-load them.
    """
    if not apps_py.is_file():
        return
    text = apps_py.read_text(encoding="utf-8")

    # Self-heal: remove any stale "could not locate" fallback comments.
    text = _FALLBACK_MSG_RE.sub("", text)

    existing = _BLOCK_RE.search(text)
    states = _parse_block_states(existing.group(0)) if existing else {}

    quote = _detect_quote(text)

    # Indent inside the default list — fall back to 8 spaces (template default).
    indent = "        "
    close = _DEFAULT_CLOSE_RE.search(text)
    if close:
        # Re-derive the indent from the closing-bracket line's leading whitespace + 4 spaces.
        bracket_indent = close.group(2).lstrip("\n")
        indent = bracket_indent + "    "

    new_block = _format_block(modules, states, indent, quote)

    if existing:
        # Replace the existing block in place, keeping a trailing newline.
        text = text[:existing.start()] + new_block + "\n" + text[existing.end():]
    elif close:
        # Insert before the closing `]` of the default list.
        insertion = f"\n{new_block}"
        text = text[:close.end(1)] + insertion + text[close.end(1):]
    else:
        # Last-resort fallback: append a single notice (idempotent — message is
        # stripped at the top of the next sync, so we won't accumulate copies).
        text = text.rstrip() + (
            "\n\n# tmsm could not locate the APPS['default'] list to edit.\n"
        )

    apps_py.write_text(text, encoding="utf-8")


def remove_modules(apps_py: Path, to_remove: set[str]) -> None:
    """Drop the given modules from the tmsm-managed block entirely."""
    if not apps_py.is_file():
        return
    text = apps_py.read_text(encoding="utf-8")
    m = _BLOCK_RE.search(text)
    if not m:
        return
    block = m.group(0)
    new_lines: list[str] = []
    for line in block.splitlines():
        em = _ENTRY_RE.match(line)
        if em and em.group(2) in to_remove:
            continue
        new_lines.append(line)
    new_block = "\n".join(new_lines)
    if not new_block.endswith("\n"):
        new_block += "\n"
    text = text[:m.start()] + new_block + text[m.end():]
    apps_py.write_text(text, encoding="utf-8")
