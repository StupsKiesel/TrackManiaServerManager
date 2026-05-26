"""HTTP download + archive extraction helpers."""
from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path
from typing import Callable

import httpx

Log = Callable[[str], None]


def download(url: str, dest: Path, log: Log) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"Downloading {url}")
    with httpx.stream("GET", url, follow_redirects=True, timeout=300.0) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        got = 0
        last_report = -1
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes(chunk_size=256 * 1024):
                f.write(chunk)
                got += len(chunk)
                if total:
                    pct = got * 100 // total
                    if pct != last_report and pct % 5 == 0:
                        log(f"  {pct:3d}%  {got // (1024 * 1024)}M / {total // (1024 * 1024)}M")
                        last_report = pct
        tmp.replace(dest)
    log(f"Saved -> {dest}")


def extract_zip(src: Path, dest: Path, log: Log) -> None:
    log(f"Extracting {src.name} -> {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src) as z:
        z.extractall(dest)
    log(f"Extracted {sum(1 for _ in dest.rglob('*'))} entries")


def extract_tar(src: Path, dest: Path, log: Log, *, strip_components: int = 0) -> None:
    """Extract a tar(.gz/.xz/.bz2) archive, optionally stripping leading path components."""
    log(f"Extracting {src.name} -> {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(src) as tf:
        members = []
        for m in tf.getmembers():
            if strip_components:
                parts = m.name.split("/", strip_components)
                if len(parts) <= strip_components:
                    continue
                m.name = parts[strip_components]
                if m.linkname and m.issym() is False and m.islnk():
                    # leave link targets alone — they may point outside
                    pass
            members.append(m)
        tf.extractall(dest, members=members)
    log(f"Extracted {sum(1 for _ in dest.rglob('*'))} entries")
