"""Event sources. Each one turns a rule's config into "what changed since last time".

The folder source polls with ``os.scandir`` instead of an OS watch API: no native dependency, the
same behaviour on Windows, macOS and Linux (and on network drives, where watch APIs are unreliable),
and a handful of folders once a second costs nothing measurable. Drive and log sources poll the same way.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

MAX_ENTRIES = 5000
RENAME_WINDOW_S = 10.0
AGENT_TOUCH_WINDOW_S = 120.0
FILE_EVENTS = ("created", "modified", "deleted")

# Files that are still being written, or that the OS/app creates as scratch: never interesting.
_PARTIAL_SUFFIXES = (".crdownload", ".part", ".partial", ".download", ".tmp", ".temp", ".swp", ".opdownload")
_PARTIAL_PREFIXES = (".", "~$", "~")
_IGNORED_NAMES = {"thumbs.db", "desktop.ini"}

Fingerprint = tuple[int, int]  # (size, mtime_ns)


class SourceError(Exception):
    """The source cannot be read right now (folder missing, too large). Shown as the rule's health."""


@dataclass
class FileChange:
    kind: str  # created | modified | deleted
    path: str
    size: int = 0


def parse_patterns(pattern: str) -> list[str]:
    parts = [p.strip().lower() for p in (pattern or "").replace(";", ",").split(",")]
    return [p for p in parts if p] or ["*"]


def _matches(name: str, patterns: list[str]) -> bool:
    low = name.lower()
    if low in _IGNORED_NAMES or low.startswith(_PARTIAL_PREFIXES) or low.endswith(_PARTIAL_SUFFIXES):
        return False
    return any(fnmatch.fnmatchcase(low, p) for p in patterns)


def validate_folder(raw: str, *, recursive: bool = False) -> Path:
    """Resolve a user/agent-supplied folder, or raise ValueError with a message fit to show them."""
    if not (raw or "").strip():
        raise ValueError("A folder path is required.")
    path = Path(os.path.expandvars(os.path.expanduser(raw.strip().strip('"').strip("'"))))
    if not path.is_absolute():
        raise ValueError(f"Use an absolute folder path (got {raw!r}).")
    try:
        path = path.resolve()
    except OSError as exc:
        raise ValueError(f"Cannot resolve {raw!r}: {exc}") from exc
    if not path.is_dir():
        raise ValueError(f"{path} is not an existing folder.")
    if path.parent == path:
        raise ValueError("Watching a whole drive or the filesystem root is not allowed; pick a folder.")
    if recursive and path == Path.home().resolve():
        raise ValueError("Watching your whole home folder recursively is not allowed; pick a subfolder.")
    return path


def scan(root: str, patterns: list[str], recursive: bool = False) -> dict[str, Fingerprint]:
    """Matching files under ``root`` with their fingerprints."""
    found: dict[str, Fingerprint] = {}
    stack = [root]
    try:
        while stack:
            current = stack.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if recursive and not entry.name.startswith("."):
                                stack.append(entry.path)
                            continue
                        if not entry.is_file(follow_symlinks=False) or not _matches(entry.name, patterns):
                            continue
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue  # vanished between listing and stat
                    found[entry.path] = (st.st_size, st.st_mtime_ns)
                    if len(found) > MAX_ENTRIES:
                        raise SourceError(f"more than {MAX_ENTRIES} matching files; narrow the folder or the pattern")
    except FileNotFoundError as exc:
        raise SourceError("folder not found (unplugged drive or deleted folder?)") from exc
    except PermissionError as exc:
        raise SourceError("no permission to read the folder") from exc
    except OSError as exc:
        raise SourceError(f"cannot read the folder: {exc}") from exc
    return found


def _readable(path: str) -> bool:
    """False while another program still holds the file open for writing (Windows) or it vanished."""
    try:
        with open(path, "rb"):
            return True
    except OSError:
        return False


@dataclass
class FolderWatcher:
    """Diffs a folder against the baseline of files the rule already knows about.

    A file is reported only once it is *settled*: same size and mtime on two consecutive polls and
    openable, so a download or a large copy fires once, when it is complete. A file that only moved
    or was renamed inside the folder, and a file the agent itself just wrote, update the baseline
    silently; otherwise a reflex whose action renames its input would trigger itself.
    """

    root: str
    patterns: list[str]
    recursive: bool = False
    events: tuple[str, ...] = ("created",)
    baseline: Optional[dict[str, Fingerprint]] = None  # None = not baselined yet
    _settling: dict[str, Fingerprint] = field(default_factory=dict)
    _gone: dict[Fingerprint, float] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: dict, baseline: Optional[dict[str, Fingerprint]]) -> "FolderWatcher":
        events = tuple(e for e in config.get("events") or ("created",) if e in FILE_EVENTS) or ("created",)
        return cls(root=config["path"], patterns=parse_patterns(config.get("pattern", "*")),
                   recursive=bool(config.get("recursive")), events=events, baseline=baseline)

    def poll(self, *, agent_touched: frozenset[str] | set[str] = frozenset(), now: Optional[float] = None,
             ) -> tuple[list[FileChange], dict[str, Fingerprint], list[str]]:
        """Returns (changes to report, baseline upserts, baseline removals). Raises SourceError."""
        ts = now if now is not None else time.time()
        current = scan(self.root, self.patterns, self.recursive)
        if self.baseline is None:  # first look: everything already there is not news
            self.baseline = dict(current)
            return [], dict(current), []

        changes: list[FileChange] = []
        upserts: dict[str, Fingerprint] = {}
        removals: list[str] = []

        for path in [p for p in self.baseline if p not in current]:
            self._gone[self.baseline.pop(path)] = ts
            removals.append(path)
            self._settling.pop(path, None)
            if "deleted" in self.events:
                changes.append(FileChange("deleted", path))
        self._gone = {fp: t for fp, t in self._gone.items() if ts - t <= RENAME_WINDOW_S}

        for path, fp in current.items():
            known = self.baseline.get(path)
            if known == fp:
                self._settling.pop(path, None)
                continue
            if self._settling.get(path) != fp or not _readable(path):
                self._settling[path] = fp  # still changing (or locked): look again next poll
                continue
            del self._settling[path]
            self.baseline[path] = fp
            upserts[path] = fp
            if known is None:
                if fp in self._gone:  # same bytes, same mtime, new name: a rename, not a new file
                    del self._gone[fp]
                    continue
                kind = "created"
            else:
                kind = "modified"
            if os.path.normcase(path) in agent_touched or kind not in self.events:
                continue
            changes.append(FileChange(kind, path, fp[0]))
        return changes, upserts, removals


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


# ---------------------------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------------------------
def clean_text(text: str, limit: int = 300) -> str:
    """Disk-derived text goes into a prompt: strip control characters and cap the length."""
    return "".join(ch if ch.isprintable() else " " for ch in str(text)).strip()[:limit]


# ---------------------------------------------------------------------------------------------
# Drives (USB sticks, SD cards, external disks)
# ---------------------------------------------------------------------------------------------
DRIVE_EVENTS = ("connected", "removed")
MAX_DRIVE_ENTRIES = 25


@dataclass(frozen=True)
class Volume:
    root: str
    label: str = ""


@dataclass
class DriveEvent:
    kind: str  # connected | removed
    root: str
    label: str = ""
    total: Optional[int] = None
    free: Optional[int] = None
    entries: list[str] = field(default_factory=list)
    more: int = 0


def parse_globs(pattern) -> list[str]:
    """Comma/semicolon separated globs (or a list of them), original case kept for display."""
    if isinstance(pattern, (list, tuple)):
        pattern = ",".join(str(p) for p in pattern)
    parts = [p.strip() for p in str(pattern or "").replace(";", ",").split(",")]
    return [p for p in parts if p] or ["*"]


def _listable(root: str) -> bool:
    try:
        with os.scandir(root):
            return True
    except OSError:
        return False


def _windows_volumes() -> list[Volume]:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.SetErrorMode.argtypes = [wintypes.UINT]
    k32.SetErrorMode.restype = wintypes.UINT
    k32.GetLogicalDrives.restype = wintypes.DWORD
    k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    k32.GetDriveTypeW.restype = wintypes.UINT
    k32.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, wintypes.LPDWORD,
                                          wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPWSTR, wintypes.DWORD]
    k32.GetVolumeInformationW.restype = wintypes.BOOL
    # SEM_FAILCRITICALERRORS: an empty card reader returns an error instead of showing "insert a disk".
    previous = k32.SetErrorMode(0)
    k32.SetErrorMode(previous | 1)
    mask = int(k32.GetLogicalDrives())
    found: list[Volume] = []
    for index in range(26):
        if not mask & (1 << index):
            continue
        root = f"{chr(65 + index)}:\\"
        if k32.GetDriveTypeW(root) not in (2, 3):  # removable, fixed
            continue
        if not _listable(root):
            continue
        buf = ctypes.create_unicode_buffer(261)
        ok = k32.GetVolumeInformationW(root, buf, 261, None, None, None, None, 0)
        found.append(Volume(root, buf.value if ok else ""))
    return found


def _unescape_mount(text: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def _linux_volumes() -> list[Volume]:
    prefixes = ("/media/", "/run/media/", "/mnt/")
    found: dict[str, Volume] = {}
    with open("/proc/mounts", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.split()
            if len(parts) < 2:
                continue
            mount = _unescape_mount(parts[1])
            if mount.startswith(prefixes) and mount not in found and _listable(mount):
                found[mount] = Volume(mount, os.path.basename(mount.rstrip("/")))
    return list(found.values())


def _mac_volumes() -> list[Volume]:
    found: list[Volume] = []
    with os.scandir("/Volumes") as entries:
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if _listable(entry.path):
                found.append(Volume(entry.path, entry.name))
    return found


def list_volumes() -> list[Volume]:
    """Volumes that are present right now: only those whose root can actually be listed."""
    try:
        if sys.platform == "win32":
            return _windows_volumes()
        if sys.platform == "darwin":
            return _mac_volumes()
        return _linux_volumes()
    except Exception as exc:
        raise SourceError(f"cannot list drives: {exc}") from exc


def describe_volume(kind: str, volume: Volume) -> DriveEvent:
    """What the agent is told about a volume. Every probe tolerates failure (a card can vanish mid-read)."""
    event = DriveEvent(kind, volume.root, clean_text(volume.label, 100))
    if kind != "connected":
        return event
    try:
        usage = shutil.disk_usage(volume.root)
        event.total, event.free = usage.total, usage.free
    except OSError:
        pass
    try:
        names: list[str] = []
        with os.scandir(volume.root) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) >= 1000:
                    break
        names.sort(key=str.lower)
        event.entries = [clean_text(n) for n in names[:MAX_DRIVE_ENTRIES]]
        event.more = max(0, len(names) - MAX_DRIVE_ENTRIES)
    except OSError:
        pass
    return event


@dataclass
class DriveWatcher:
    """Reports volumes that appear (and optionally disappear).

    The first poll is a silent baseline: drives already connected are not news, and the baseline is
    deliberately not persisted, so a restart does not fire for them either. A new volume fires only
    after it has been present and listable on two consecutive polls, so a card reader that is still
    mounting does not fire half-ready.
    """

    patterns: list[str]
    events: tuple[str, ...] = ("connected",)
    list_volumes: Callable[[], list[Volume]] = list_volumes
    _known: Optional[dict[str, Volume]] = None
    _candidates: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: dict, list_volumes_fn: Optional[Callable[[], list[Volume]]] = None) -> "DriveWatcher":
        events = tuple(e for e in config.get("events") or ("connected",) if e in DRIVE_EVENTS) or ("connected",)
        return cls(patterns=parse_globs(config.get("pattern", "*")), events=events,
                   list_volumes=list_volumes_fn or list_volumes)

    def wants(self, volume: Volume) -> bool:
        label, root = volume.label.lower(), volume.root.lower()
        return any(fnmatch.fnmatchcase(label, p.lower()) or fnmatch.fnmatchcase(root, p.lower()) for p in self.patterns)

    def poll(self, *, now: Optional[float] = None) -> list[DriveEvent]:
        """Raises SourceError when volumes cannot be enumerated."""
        try:
            volumes = list(self.list_volumes())
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"cannot list drives: {exc}") from exc
        current: dict[str, Volume] = {}
        for volume in volumes:
            current.setdefault(os.path.normcase(volume.root), volume)
        if self._known is None:
            self._known = current
            return []
        out: list[DriveEvent] = []
        for key in [k for k in self._known if k not in current]:
            gone = self._known.pop(key)
            if "removed" in self.events and self.wants(gone):
                out.append(describe_volume("removed", gone))
        waiting: dict[str, int] = {}
        for key, volume in current.items():
            if key in self._known:
                continue
            count = self._candidates.get(key, 0) + 1
            if count < 2:
                waiting[key] = count
                continue
            self._known[key] = volume
            if "connected" in self.events and self.wants(volume):
                out.append(describe_volume("connected", volume))
        self._candidates = waiting
        return out


# ---------------------------------------------------------------------------------------------
# Log files
# ---------------------------------------------------------------------------------------------
LOG_MAX_READ = 1 << 20  # bytes read per poll
LOG_BATCH_S = 5.0  # longest a batch of matches waits for the file to go quiet
LOG_MAX_LINES = 20
LOG_LINE_CHARS = 300
_LOG_MAX_PARTIAL = 256 * 1024
_LOG_MATCH_CHARS = 10000


def is_regex_pattern(pattern: str) -> bool:
    pattern = (pattern or "").strip()
    return len(pattern) >= 3 and pattern[0] == "/" and pattern[-1] == "/"


class LogMatcher:
    """Plain text = case-insensitive substring; /.../ = regular expression (as written)."""

    def __init__(self, pattern: str):
        pattern = (pattern or "").strip()
        if not pattern:
            raise ValueError("A pattern is required: text to look for, or /regex/.")
        self.pattern = pattern
        self.regex: Optional["re.Pattern[str]"] = None
        if is_regex_pattern(pattern):
            try:
                self.regex = re.compile(pattern[1:-1])
            except re.error as exc:
                raise ValueError(f"Invalid regular expression {pattern!r}: {exc}") from exc
        self._needle = pattern.lower()

    def __call__(self, line: str) -> bool:
        if self.regex is not None:
            return self.regex.search(line[:_LOG_MATCH_CHARS]) is not None
        return self._needle in line.lower()

    def describe(self) -> str:
        return f"matches {self.pattern}" if self.regex is not None else f'contains "{self.pattern}"'


def compile_log_pattern(pattern: str) -> LogMatcher:
    """Raises ValueError (fit to show the user) for an empty or invalid pattern."""
    return LogMatcher(pattern)


def validate_log_file(raw: str) -> Path:
    """The file may not exist yet, but then its folder must."""
    if not (raw or "").strip():
        raise ValueError("A log file path is required.")
    path = Path(os.path.expandvars(os.path.expanduser(raw.strip().strip('"').strip("'"))))
    if not path.is_absolute():
        raise ValueError(f"Use an absolute file path (got {raw!r}).")
    try:
        path = path.resolve()
    except OSError as exc:
        raise ValueError(f"Cannot resolve {raw!r}: {exc}") from exc
    if path.is_dir():
        raise ValueError(f"{path} is a folder; give the log file itself (or add a folder reflex).")
    if not path.exists() and not path.parent.is_dir():
        raise ValueError(f"Neither {path} nor its folder exists.")
    return path


@dataclass
class LogBatch:
    lines: list[str]
    total: int


@dataclass
class LogWatcher:
    """Reports new lines matching a pattern, appended to one file.

    ``offset`` is how far the file has been read (None = not looked at yet: start at its end).
    ``commit_point`` is what is safe to persist: the start of the oldest match not yet reported, or
    the start of an unfinished last line, so a restart neither loses nor repeats a match.
    """

    path: str
    match: LogMatcher
    offset: Optional[int] = None
    persisted: Optional[int] = None
    _partial: bytes = b""
    _lines: list[str] = field(default_factory=list)
    _count: int = 0
    _pending_start: Optional[int] = None
    _first_at: float = 0.0

    @classmethod
    def from_config(cls, config: dict, offset: Optional[int]) -> "LogWatcher":
        return cls(path=config["file"], match=compile_log_pattern(config.get("pattern", "")),
                   offset=offset, persisted=offset)

    @property
    def commit_point(self) -> Optional[int]:
        if self.offset is None:
            return None
        if self._count and self._pending_start is not None:
            return self._pending_start
        return max(0, self.offset - len(self._partial))

    def _reset(self) -> None:
        self.offset, self._partial = 0, b""
        if self._count:
            self._pending_start = 0

    def poll(self, *, agent_touched: frozenset[str] | set[str] = frozenset(), now: Optional[float] = None,
             ) -> Optional[LogBatch]:
        """Returns a batch once the file has gone quiet (or 5 s passed). Raises SourceError."""
        ts = now if now is not None else time.time()
        try:
            size: Optional[int] = os.stat(self.path).st_size
        except (FileNotFoundError, NotADirectoryError):
            size = None
        except PermissionError as exc:
            raise SourceError("no permission to read the file") from exc
        except OSError as exc:
            raise SourceError(f"cannot read the file: {exc}") from exc

        grew = False
        if size is None:  # wait quietly; a file that comes back is new and is read from the start
            self._reset()
        else:
            if self.offset is None:
                self.offset = size  # existing content never fires
            elif size < self.offset:  # truncated or rotated
                self._reset()
            if size > self.offset:
                grew = self._read(size, ts, self._touched(agent_touched))
        if self._count and (not grew or ts - self._first_at >= LOG_BATCH_S):
            batch = LogBatch(self._lines, self._count)
            self._lines, self._count, self._pending_start = [], 0, None
            return batch
        return None

    def _touched(self, agent_touched) -> bool:
        paths = {os.path.normcase(os.path.abspath(self.path))}
        try:
            paths.add(os.path.normcase(os.path.realpath(self.path)))
        except OSError:
            pass
        return bool(paths & set(agent_touched))

    def _read(self, size: int, ts: float, touched: bool) -> bool:
        assert self.offset is not None
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                data = handle.read(min(size - self.offset, LOG_MAX_READ))
        except PermissionError as exc:
            raise SourceError("no permission to read the file") from exc
        except OSError as exc:
            raise SourceError(f"cannot read the file: {exc}") from exc
        if not data:
            return False
        pos = self.offset - len(self._partial)
        self.offset += len(data)
        parts = (self._partial + data).split(b"\n")
        self._partial = parts.pop()  # an unfinished last line waits for its newline
        if len(self._partial) > _LOG_MAX_PARTIAL:  # one endless line: do not buffer it forever
            parts.append(self._partial)
            self._partial = b""
        for raw in parts:
            start, pos = pos, pos + len(raw) + 1
            if touched:  # the agent wrote this: advance silently
                continue
            text = raw.decode("utf-8", errors="replace").rstrip("\r")
            if not self.match(text):
                continue
            if self._count == 0:
                self._pending_start, self._first_at = start, ts
            self._count += 1
            if len(self._lines) < LOG_MAX_LINES:
                self._lines.append(clean_text(text, LOG_LINE_CHARS))
        return True
