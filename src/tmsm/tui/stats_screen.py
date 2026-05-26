"""System statistics screen — CPU, memory, disk, network (eth0), GPU, temps."""
from __future__ import annotations

import subprocess
import time
from collections import deque
from typing import NamedTuple

import psutil
from rich.panel import Panel
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

HISTORY_LEN = 40
SPARK = " ▁▂▃▄▅▆▇█"   # 9 levels; space = zero


# ── rendering helpers ─────────────────────────────────────────────────────────

def _bar(pct: float, width: int = 26) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = round(width * pct / 100)
    c = "green" if pct < 70 else "yellow" if pct < 90 else "red"
    return f"[{c}]{'█' * filled}[/{c}]{'░' * (width - filled)}"


def _sparkline(values: deque[float], colour: str) -> str:
    if not values:
        return ""
    max_v = max(values) or 1.0
    chars = [SPARK[min(int(v / max_v * (len(SPARK) - 1)), len(SPARK) - 1)] for v in values]
    return f"[{colour}]{''.join(chars)}[/{colour}]"


def _pct_colour(pct: float) -> str:
    return "green" if pct < 70 else "yellow" if pct < 90 else "red"


def _gb(b: int) -> str:
    return f"{b / 1024 ** 3:.2f} GB"


def _speed(bps: float) -> str:
    if bps >= 1024 ** 2:
        return f"{bps / 1024 ** 2:7.2f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:7.2f} KB/s"
    return f"{bps:7.0f}  B/s"


# ── section panels ────────────────────────────────────────────────────────────

def _cpu_panel(cpu_hist: deque[float]) -> Panel:
    percents = psutil.cpu_percent(percpu=True)
    freq = psutil.cpu_freq()
    overall = sum(percents) / len(percents) if percents else 0.0
    cpu_hist.append(overall)

    lines: list[str] = []
    for i, p in enumerate(percents):
        c = _pct_colour(p)
        lines.append(f"  Core {i:<3d}  {_bar(p)}  [{c}]{p:5.1f}%[/{c}]")

    c = _pct_colour(overall)
    freq_str = f"  [dim]{freq.current:.0f} MHz[/dim]" if freq else ""
    lines.append("")
    lines.append(f"  [dim]Overall[/dim]  {_bar(overall)}  [{c}]{overall:5.1f}%[/{c}]{freq_str}")
    lines.append(f"  {_sparkline(cpu_hist, 'cyan')}")

    return Panel(
        "\n".join(lines),
        title="[bold cyan]CPU[/bold cyan]",
        border_style="cyan",
        padding=(0, 1),
    )


def _mem_panel() -> Panel:
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    cr = _pct_colour(vm.percent)
    cs = _pct_colour(sw.percent)
    lines = [
        f"  [bold]RAM [/bold]  {_bar(vm.percent)}  [{cr}]{vm.percent:5.1f}%[/{cr}]"
        f"  {_gb(vm.used)} / {_gb(vm.total)}",
        f"  [bold]Swap[/bold]  {_bar(sw.percent)}  [{cs}]{sw.percent:5.1f}%[/{cs}]"
        f"  {_gb(sw.used)} / {_gb(sw.total)}",
    ]
    return Panel(
        "\n".join(lines),
        title="[bold magenta]Memory[/bold magenta]",
        border_style="magenta",
        padding=(0, 1),
    )


def _disk_panel() -> Panel:
    lines: list[str] = []
    seen: set[str] = set()
    for part in psutil.disk_partitions():
        if part.mountpoint in seen:
            continue
        seen.add(part.mountpoint)
        try:
            u = psutil.disk_usage(part.mountpoint)
        except PermissionError:
            continue
        c = _pct_colour(u.percent)
        mp = part.mountpoint[:20]
        lines.append(
            f"  {mp:<20s}  {_bar(u.percent)}  [{c}]{u.percent:5.1f}%[/{c}]"
            f"  {_gb(u.used)} / {_gb(u.total)}"
        )
    body = "\n".join(lines) if lines else "  [dim]No partitions found[/dim]"
    return Panel(
        body,
        title="[bold yellow]Disk[/bold yellow]",
        border_style="yellow",
        padding=(0, 1),
    )


class _NetSnap(NamedTuple):
    sent: int
    recv: int
    ts: float


def _net_panel(
    prev: _NetSnap | None,
    up_hist: deque[float],
    dn_hist: deque[float],
) -> tuple[Panel, _NetSnap | None]:
    counters = psutil.net_io_counters(pernic=True)
    eth = counters.get("eth0")
    if eth is None:
        panel = Panel(
            "[dim]eth0 not found[/dim]",
            title="[bold green]Network (eth0)[/bold green]",
            border_style="green",
        )
        return panel, None

    now = _NetSnap(eth.bytes_sent, eth.bytes_recv, time.monotonic())
    if prev is not None and (now.ts - prev.ts) > 0:
        dt = now.ts - prev.ts
        up_bps = max(0.0, (now.sent - prev.sent) / dt)
        dn_bps = max(0.0, (now.recv - prev.recv) / dt)
    else:
        up_bps = dn_bps = 0.0

    up_hist.append(up_bps)
    dn_hist.append(dn_bps)

    lines = [
        f"  [green]↑ Upload  [/green] {_speed(up_bps)}  {_sparkline(up_hist, 'green')}",
        f"  [cyan]↓ Download[/cyan] {_speed(dn_bps)}  {_sparkline(dn_hist, 'cyan')}",
        "",
        f"  [dim]Total  sent: {_gb(eth.bytes_sent)}   recv: {_gb(eth.bytes_recv)}[/dim]",
    ]
    return Panel(
        "\n".join(lines),
        title="[bold green]Network (eth0)[/bold green]",
        border_style="green",
        padding=(0, 1),
    ), now


def _gpu_panel() -> Panel | None:
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    lines: list[str] = []
    for line in r.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        name, util, mem_used, mem_total, temp = parts
        try:
            util_f = float(util)
            mem_pct = float(mem_used) / float(mem_total) * 100
            temp_f = float(temp)
        except ValueError:
            continue
        tc = _pct_colour(temp_f)
        lines += [
            f"  [bold]{name}[/bold]",
            f"  Load  {_bar(util_f)}  {util_f:5.1f}%",
            f"  VRAM  {_bar(mem_pct)}  {mem_used} / {mem_total} MB",
            f"  Temp  [{tc}]{temp_f:.0f}°C[/{tc}]",
        ]
    if not lines:
        return None
    return Panel(
        "\n".join(lines),
        title="[bold red]GPU[/bold red]",
        border_style="red",
        padding=(0, 1),
    )


def _temp_panel() -> Panel | None:
    try:
        sensors = psutil.sensors_temperatures()
    except AttributeError:
        return None
    if not sensors:
        return None

    lines: list[str] = []
    for chip, entries in sensors.items():
        for e in entries:
            label = (e.label or chip)[:28]
            c = "green" if e.current < 70 else "yellow" if e.current < 85 else "red"
            crit = f"  [dim]crit: {e.critical:.0f}°C[/dim]" if e.critical else ""
            lines.append(f"  {label:<28s}  [{c}]{e.current:5.1f}°C[/{c}]{crit}")
    if not lines:
        return None
    return Panel(
        "\n".join(lines),
        title="[bold white]Temperatures[/bold white]",
        border_style="bright_black",
        padding=(0, 1),
    )


# ── screen ────────────────────────────────────────────────────────────────────

class StatsScreen(Screen):
    """Full-screen system stats with sparkline network history."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("q", "app.pop_screen", "Back"),
        Binding("R", "refresh_stats", "Refresh"),
    ]

    DEFAULT_CSS = """
    StatsScreen #stats-columns {
        height: 1fr;
    }
    StatsScreen #stats-left {
        width: 1fr;
        padding: 0 1;
    }
    StatsScreen #stats-right {
        width: 1fr;
        padding: 0 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._net_prev: _NetSnap | None = None
        self._cpu_hist: deque[float] = deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN)
        self._up_hist: deque[float] = deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN)
        self._dn_hist: deque[float] = deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="stats-columns"):
            with VerticalScroll(id="stats-left"):
                yield Static(id="stats-cpu")
            with VerticalScroll(id="stats-right"):
                yield Static(id="stats-mem")
                yield Static(id="stats-disk")
                yield Static(id="stats-net")
                yield Static(id="stats-gpu")
                yield Static(id="stats-temp")
        yield Footer()

    def on_mount(self) -> None:
        # Prime network counter so first tick shows a real delta
        eth = psutil.net_io_counters(pernic=True).get("eth0")
        if eth:
            self._net_prev = _NetSnap(eth.bytes_sent, eth.bytes_recv, time.monotonic())
        self.update_stats()
        self.set_interval(2.0, self.update_stats)

    def action_refresh_stats(self) -> None:
        self.update_stats()

    def update_stats(self) -> None:
        self.query_one("#stats-cpu", Static).update(_cpu_panel(self._cpu_hist))
        self.query_one("#stats-mem", Static).update(_mem_panel())
        self.query_one("#stats-disk", Static).update(_disk_panel())

        net, self._net_prev = _net_panel(self._net_prev, self._up_hist, self._dn_hist)
        self.query_one("#stats-net", Static).update(net)

        gpu = _gpu_panel()
        self.query_one("#stats-gpu", Static).update(gpu if gpu is not None else "")

        temp = _temp_panel()
        self.query_one("#stats-temp", Static).update(temp if temp is not None else "")

