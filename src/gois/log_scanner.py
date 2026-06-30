"""Tail QClaw-related log files and trigger recovery on known error patterns.

Each pattern has a name (for the dashboard / metric) and a regex. A match
on any pattern is treated as evidence that the user-facing UI is broken
even when the process is still alive (Electron service connection error,
bridge daemon repeatedly dying, openclaw gateway disconnecting, …).

The scanner keeps a per-file byte offset so each scan only reads the new
tail. If a file shrank (rotated / truncated), we restart from 0. Offsets
persist in MonitorState so a monitor restart doesn't re-fire on old logs.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

log = logging.getLogger(__name__)

# Read at most this many bytes per file per scan. Protects against a log
# explosion (gateway.err.log is already 59 MB on this host).
MAX_READ_PER_FILE = 256 * 1024


@dataclass
class LogPattern:
    name: str
    pattern: str
    _compiled: re.Pattern[str] = field(init=False, repr=False)

    def __post_init__(self):
        self._compiled = re.compile(self.pattern, re.IGNORECASE)

    def search(self, text: str) -> bool:
        return self._compiled.search(text) is not None


@dataclass
class LogMatch:
    ts: float
    file: str
    line_no: int   # within the chunk just read
    pattern: str
    line: str      # truncated

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "file": self.file,
            "line_no": self.line_no,
            "pattern": self.pattern,
            "line": self.line,
        }


@dataclass
class ScanResult:
    ts: float = field(default_factory=time.time)
    files_scanned: int = 0
    bytes_read: int = 0
    matches: List[LogMatch] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        bits = [f"{self.files_scanned} files / {self.bytes_read} B"]
        if self.matches:
            bits.append(f"{len(self.matches)} match(es)")
        if self.errors:
            bits.append(f"{len(self.errors)} error(s)")
        return ", ".join(bits) if bits else "clean"

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "files_scanned": self.files_scanned,
            "bytes_read": self.bytes_read,
            "matches": [m.to_dict() for m in self.matches],
            "errors": self.errors,
            "summary": self.summary(),
        }


class LogScanner:
    def __init__(self, paths: Iterable[str], patterns: Iterable[LogPattern],
                 offsets: Optional[dict[str, int]] = None):
        self.paths = [str(Path(p).expanduser()) for p in paths]
        self.patterns = list(patterns)
        self.offsets: dict[str, int] = dict(offsets or {})

    def reset(self) -> None:
        """Forget offsets — useful when patterns change."""
        self.offsets.clear()

    def initialize_offsets(self) -> None:
        """On first run, jump to end of each file so we don't replay history."""
        for p in self.paths:
            if p in self.offsets:
                continue
            try:
                self.offsets[p] = Path(p).stat().st_size
            except FileNotFoundError:
                self.offsets[p] = 0

    async def scan_once(self) -> ScanResult:
        result = ScanResult()
        for path in self.paths:
            try:
                start = self.offsets.get(path, 0)
                stat = Path(path).stat()
                size = stat.st_size
                if size < start:
                    # rotated / truncated — read from new beginning
                    start = 0
                if size == start:
                    self.offsets[path] = size
                    continue
                read_size = min(size - start, MAX_READ_PER_FILE)
                # if the file is huge, jump close to the tail
                if size - start > MAX_READ_PER_FILE:
                    start = size - MAX_READ_PER_FILE
                with open(path, "rb") as f:
                    f.seek(start)
                    chunk = f.read(read_size)
                self.offsets[path] = start + len(chunk)
                result.files_scanned += 1
                result.bytes_read += len(chunk)
                text = chunk.decode("utf-8", errors="replace")
                for i, line in enumerate(text.splitlines()):
                    for pat in self.patterns:
                        if pat.search(line):
                            result.matches.append(
                                LogMatch(
                                    ts=time.time(),
                                    file=path,
                                    line_no=i,
                                    pattern=pat.name,
                                    line=line.strip()[:240],
                                )
                            )
                            break  # one pattern per line is enough
            except FileNotFoundError:
                # file disappeared since last scan — drop its offset
                self.offsets.pop(path, None)
            except PermissionError as e:
                result.errors.append(f"perm denied: {path}: {e}")
            except Exception as e:
                result.errors.append(f"{path}: {type(e).__name__}: {e}")
                log.warning("log scanner error on %s: %s", path, e)
        return result
