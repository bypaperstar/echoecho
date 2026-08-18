#!/usr/bin/env python3
"""Bounded, best-effort capture for detached process stdout/stderr.

The lifecycle script pipes a child's merged output into this program.  Keeping
the producer on the left side of a pipeline (rather than launching it from a
wrapper) matters: existing pgrep/pkill process discovery continues to see only
the real daemon, Electron, Live Writer, or Lume command line.

The active file keeps its original name so ``*-current.log`` symlinks remain
useful.  On rotation, older data becomes ``<stem>.part-001.log`` (newest
archive), then part 002, and so on.  ``MAX_PARTS`` includes the active file.
If the sink becomes unwritable, stdin is still drained until EOF so diagnostics
can never terminate the producer with SIGPIPE.
"""

from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import stat
import sys
from typing import BinaryIO
import uuid


DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_PARTS = 5
DEFAULT_MAX_RUNS = 10
MAX_BYTES_LIMIT = 100 * 1024 * 1024
MAX_PARTS_LIMIT = 100
MAX_RUNS_LIMIT = 200
READ_SIZE = 64 * 1024
MAX_TAIL_LINES = 2000
MAX_TAIL_LINE_BYTES = 64 * 1024
MAX_TAIL_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_TAIL_INPUT_BYTES = 64 * 1024 * 1024
MAX_TAIL_DIRECTORY_ENTRIES = 10000

_ROOT_PREFIX = r"\d{8}T\d{6}Z-[1-9]\d*-[0-9a-f]{32}"
_COMPONENT = r"[a-z0-9_][a-z0-9_-]*"
_ROOT_NAME_RE = re.compile(
    r"^(?P<stem>%s-(?P<component>%s))\.log$" %
    (_ROOT_PREFIX, _COMPONENT))
_PART_NAME_RE = re.compile(
    r"^(?P<stem>%s-(?P<component>%s))\.part-(?P<part>\d{3})\.log$" %
    (_ROOT_PREFIX, _COMPONENT))
_CURRENT_NAME_RE = re.compile(
    r"^(?P<component>%s)-current\.log$" % _COMPONENT)
_TERMINAL_UNSAFE = frozenset({
    0x061C, 0x200E, 0x200F, 0x2028, 0x2029,
    *range(0x202A, 0x202F), *range(0x2066, 0x206A),
})


def _positive_env(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(value, maximum) if value > 0 else default


def _part_path(base: Path, number: int) -> Path:
    return base.with_name(f"{base.stem}.part-{number:03d}{base.suffix}")


def _generated_root(name: str, component: str | None = None) -> bool:
    match = _ROOT_NAME_RE.fullmatch(name)
    return bool(match and (component is None or
                           match.group("component") == component))


def _generated_part_root(name: str, component: str | None = None) -> str | None:
    match = _PART_NAME_RE.fullmatch(name)
    if not match:
        return None
    stem = match.group("stem")
    if component is not None and match.group("component") != component:
        return None
    return stem + ".log"


def _terminal_safe(data: bytes) -> bytes:
    """Escape terminal controls while preserving ordinary UTF-8 log text."""
    text = data.decode("utf-8", "backslashreplace")
    rendered = []
    for char in text:
        code = ord(char)
        if char == "\n":
            rendered.append(char)
        elif code < 0x20 or 0x7F <= code <= 0x9F:
            rendered.append("\\x%02x" % code)
        elif code in _TERMINAL_UNSAFE:
            rendered.append("\\u%04x" % code)
        else:
            rendered.append(char)
    return "".join(rendered).encode("utf-8")


def _prune_runs_fd(directory_fd: int, component: str, max_runs: int,
                   current_name: str | None = None,
                   reserved_runs: int = 0) -> bool:
    """Bound component runs using only the already-verified directory fd."""
    try:
        names = []
        complete = True
        with os.scandir(directory_fd) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_TAIL_DIRECTORY_ENTRIES:
                    complete = False
                    break
                names.append(entry.name)
    except OSError:
        return False
    if not complete:
        return False
    roots = []
    for name in names:
        if not _generated_root(name, component):
            continue
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            roots.append((info.st_mtime_ns, name))
    roots.sort(reverse=True)
    keep = {name for _, name in roots[
        :max(0, max_runs - reserved_runs)]}
    if current_name is not None:
        keep.add(current_name)
    for _, old_name in roots:
        if old_name in keep:
            continue
        try:
            os.unlink(old_name, dir_fd=directory_fd)
        except OSError as exc:
            if not isinstance(exc, FileNotFoundError):
                complete = False
            continue
        for part_name in names:
            if _generated_part_root(part_name, component) != old_name:
                continue
            try:
                info = os.stat(
                    part_name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode):
                    os.unlink(part_name, dir_fd=directory_fd)
            except OSError as exc:
                if not isinstance(exc, FileNotFoundError):
                    complete = False
    # A kill in the tiny rename -> reopen window can leave an archive without
    # its active base. Do not let those crash remnants evade the run cap.
    for part_name in names:
        base_name = _generated_part_root(part_name, component)
        if base_name is None:
            continue
        try:
            base_info = os.stat(
                base_name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            base_info = None
        if base_info is not None and stat.S_ISREG(base_info.st_mode):
            continue
        try:
            part_info = os.stat(
                part_name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISREG(part_info.st_mode):
                os.unlink(part_name, dir_fd=directory_fd)
        except OSError as exc:
            if not isinstance(exc, FileNotFoundError):
                complete = False
    return complete


def _open_console_directory(directory: Path) -> tuple[int, os.stat_result]:
    directory = Path(directory)
    directory_info = directory.lstat()
    if not stat.S_ISDIR(directory_info.st_mode):
        raise OSError("console directory is not a real directory")
    flags = os.O_RDONLY
    for option in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW"):
        flags |= getattr(os, option, 0)
    directory_fd = os.open(str(directory), flags)
    try:
        opened_info = os.fstat(directory_fd)
        if not stat.S_ISDIR(opened_info.st_mode):
            raise OSError("console directory is not a directory")
        if ((directory_info.st_dev, directory_info.st_ino) !=
                (opened_info.st_dev, opened_info.st_ino)):
            raise OSError("console directory changed while opening")
        return directory_fd, opened_info
    except BaseException:
        os.close(directory_fd)
        raise


def create_log(directory: Path, component: str, max_runs: int) -> Path:
    """Exclusively create a private launch log and atomically point at it."""
    max_runs = max(1, min(int(max_runs), MAX_RUNS_LIMIT))
    if not component or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
                            for ch in component):
        raise ValueError("invalid console component")
    directory = Path(directory)
    directory_fd, _opened_info = _open_console_directory(directory)
    try:
        if not _prune_runs_fd(
                directory_fd, component, max_runs=max_runs,
                reserved_runs=1):
            raise OSError("console retention could not be enforced")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"{stamp}-{os.getpid()}-{uuid.uuid4().hex}-{component}.log"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)

        current_name = f"{component}-current.log"
        temporary_name = f".{component}-current.{uuid.uuid4().hex}.tmp"
        try:
            os.symlink(name, temporary_name, dir_fd=directory_fd)
            os.replace(
                temporary_name, current_name,
                src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        except OSError:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except OSError:
                pass
            # The launch log is still usable by its returned path. A hostile
            # real directory at the convenience-link path is never replaced.
        return directory / name
    finally:
        os.close(directory_fd)


def _open_private_append(path: Path) -> tuple[int, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"console log is not a regular file: {path}")
        os.fchmod(fd, 0o600)
        return fd, info.st_size
    except BaseException:
        os.close(fd)
        raise


class RotatingCapture:
    """A size-bounded append sink which permanently degrades to a drain."""

    def __init__(self, path: Path, max_bytes: int, max_parts: int):
        self.path = path
        self.max_bytes = max(1, min(int(max_bytes), MAX_BYTES_LIMIT))
        self.max_parts = max(1, min(int(max_parts), MAX_PARTS_LIMIT))
        self.fd: int | None = None
        self.size = 0
        self.enabled = True
        try:
            self.fd, self.size = _open_private_append(path)
            if self.size >= self.max_bytes:
                self._rotate()
        except (OSError, ValueError):
            self._disable()

    def _disable(self) -> None:
        fd, self.fd = self.fd, None
        self.enabled = False
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _rotate(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

        # MAX_PARTS includes the newly opened active file.  Part 001 is the
        # newest archive; descending renames prevent overwriting source data.
        archive_count = self.max_parts - 1
        if archive_count <= 0:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        else:
            oldest = _part_path(self.path, archive_count)
            try:
                oldest.unlink()
            except FileNotFoundError:
                pass
            for number in range(archive_count - 1, 0, -1):
                source = _part_path(self.path, number)
                if source.exists():
                    os.replace(source, _part_path(self.path, number + 1))
            os.replace(self.path, _part_path(self.path, 1))

        self.fd, self.size = _open_private_append(self.path)

    def write(self, data: bytes) -> None:
        if not self.enabled or not data:
            return
        offset = 0
        try:
            while offset < len(data):
                if self.size >= self.max_bytes:
                    self._rotate()
                remaining = min(len(data) - offset, self.max_bytes - self.size)
                while remaining:
                    assert self.fd is not None
                    written = os.write(self.fd, data[offset:offset + remaining])
                    if written <= 0:
                        raise OSError("console log write made no progress")
                    offset += written
                    remaining -= written
                    self.size += written
        except (OSError, ValueError):
            # Keep consuming the producer's pipe after any filesystem failure.
            self._disable()

    def close(self) -> None:
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def capture(source: BinaryIO, path: Path, max_bytes: int, max_parts: int) -> None:
    sink = RotatingCapture(path, max_bytes=max_bytes, max_parts=max_parts)
    try:
        while True:
            try:
                chunk = source.read(READ_SIZE)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            sink.write(chunk)
    finally:
        sink.close()


def latest_log(directory: Path, component: str) -> Path:
    """Return a safe current/latest component root with bounded discovery."""
    if not component or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
                            for ch in component):
        raise ValueError("invalid console component")
    directory = Path(directory)
    directory_fd, _info = _open_console_directory(directory)
    try:
        current_name = f"{component}-current.log"
        try:
            current_info = os.stat(
                current_name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(current_info.st_mode):
                target = os.readlink(current_name, dir_fd=directory_fd)
                if (target == os.path.basename(target) and
                        _generated_root(target, component)):
                    target_info = os.stat(
                        target, dir_fd=directory_fd, follow_symlinks=False)
                    if stat.S_ISREG(target_info.st_mode):
                        return directory / current_name
        except OSError:
            pass
        newest = None
        with os.scandir(directory_fd) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_TAIL_DIRECTORY_ENTRIES:
                    raise OSError("console directory entry limit exceeded")
                if (not _generated_root(entry.name, component) or
                        not entry.is_file(follow_symlinks=False)):
                    continue
                try:
                    mtime = entry.stat(follow_symlinks=False).st_mtime_ns
                except OSError:
                    continue
                candidate = (mtime, entry.name)
                if newest is None or candidate > newest:
                    newest = candidate
        if newest is None:
            raise OSError("no console log found")
        return directory / newest[1]
    finally:
        os.close(directory_fd)


def _retained_names(directory_fd: int, requested_name: str) -> list[str]:
    """Resolve a current pointer and discover parts with one bounded scan."""
    if requested_name != os.path.basename(requested_name):
        raise ValueError("console log must be a direct child")
    info = os.stat(
        requested_name, dir_fd=directory_fd, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode):
        pointer_match = _CURRENT_NAME_RE.fullmatch(requested_name)
        if pointer_match is None:
            raise ValueError("console symlink is not an owned current pointer")
        base_name = os.readlink(requested_name, dir_fd=directory_fd)
        component = pointer_match.group("component")
        if (base_name != os.path.basename(base_name) or
                not _generated_root(base_name, component)):
            raise ValueError(
                "console current symlink must target an owned sibling file")
        info = os.stat(base_name, dir_fd=directory_fd, follow_symlinks=False)
    else:
        base_name = requested_name
    if not stat.S_ISREG(info.st_mode):
        raise OSError("console log is not a regular file")

    base = Path(base_name)
    prefix = f"{base.stem}.part-"
    archives: dict[int, str] = {}
    with os.scandir(directory_fd) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_TAIL_DIRECTORY_ENTRIES:
                raise OSError("console directory entry limit exceeded")
            if (not entry.name.startswith(prefix) or
                    not entry.name.endswith(base.suffix)):
                continue
            marker = Path(entry.name).stem.rsplit(".part-", 1)
            if (len(marker) != 2 or not marker[1].isdigit() or
                    not entry.is_file(follow_symlinks=False)):
                continue
            number = int(marker[1])
            if 1 <= number < MAX_PARTS_LIMIT:
                archives.setdefault(number, entry.name)
    # A larger part number is older. Read oldest -> newest -> active.
    return [archives[number] for number in sorted(archives, reverse=True)] + [base_name]


def tail(path: Path, lines: int, output: BinaryIO, *, raw: bool = False) -> None:
    lines = max(1, min(int(lines), MAX_TAIL_LINES))
    recent: deque[bytes] = deque()
    recent_bytes = 0
    pending = bytearray()
    pending_truncated = False

    def remember(raw: bytes, truncated: bool = False) -> None:
        nonlocal recent_bytes
        if truncated or len(raw) > MAX_TAIL_LINE_BYTES:
            marker = b"[... console line prefix truncated ...] "
            raw = marker + raw[-(MAX_TAIL_LINE_BYTES - len(marker)):]
        if not raw_mode:
            raw = _terminal_safe(raw)
            if len(raw) > MAX_TAIL_LINE_BYTES:
                marker = b"[... escaped console line truncated ...] "
                suffix = raw[-(MAX_TAIL_LINE_BYTES - len(marker)):]
                # Avoid emitting a partial UTF-8 sequence at the truncation
                # boundary. A partial textual escape remains inert.
                suffix = suffix.decode("utf-8", "ignore").encode("utf-8")
                raw = marker + suffix
        recent.append(raw)
        recent_bytes += len(raw)
        while (len(recent) > lines or
               recent_bytes > MAX_TAIL_OUTPUT_BYTES) and len(recent) > 1:
            recent_bytes -= len(recent.popleft())

    raw_mode = bool(raw)
    directory_fd, _info = _open_console_directory(path.parent)
    selected = []
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        names = _retained_names(directory_fd, path.name)
        remaining_input = MAX_TAIL_INPUT_BYTES
        omitted_prefix = False
        # Select from newest backwards, seeking into the oldest selected file
        # when needed. This reads the actual tail of sparse/oversized files.
        for name_index, name in enumerate(reversed(names)):
            if remaining_input <= 0:
                omitted_prefix = True
                break
            try:
                fd = os.open(name, flags, dir_fd=directory_fd)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    os.close(fd)
                    continue
            except OSError:
                continue
            take = min(info.st_size, remaining_input)
            start = max(0, info.st_size - take)
            if start > 0:
                omitted_prefix = True
            selected.append((fd, start, take))
            remaining_input -= take
            if remaining_input <= 0 and name_index + 1 < len(names):
                omitted_prefix = True

        pending_truncated = omitted_prefix
        for fd, start, length in reversed(selected):
            try:
                os.lseek(fd, start, os.SEEK_SET)
                remaining = length
                while remaining > 0:
                    chunk = os.read(fd, min(READ_SIZE, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    pending.extend(chunk)
                    while True:
                        newline = pending.find(b"\n")
                        if newline < 0:
                            if len(pending) > MAX_TAIL_LINE_BYTES:
                                del pending[:-MAX_TAIL_LINE_BYTES]
                                pending_truncated = True
                            break
                        remember(bytes(pending[:newline + 1]), pending_truncated)
                        del pending[:newline + 1]
                        pending_truncated = False
            except OSError:
                continue
    finally:
        for fd, _start, _length in selected:
            try:
                os.close(fd)
            except OSError:
                pass
        os.close(directory_fd)
    if pending:
        remember(bytes(pending), pending_truncated)
    for line in recent:
        output.write(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--log", type=Path)
    target.add_argument("--create-dir", type=Path,
                        help="exclusively create and print a launch log path")
    target.add_argument("--latest-dir", type=Path,
                        help="safely find the latest component launch log")
    parser.add_argument("--component",
                        help="component name for --create-dir")
    parser.add_argument("--tail", type=int,
                        help="print this many lines across retained parts")
    parser.add_argument(
        "--raw-tail", action="store_true",
        help="emit raw terminal controls when tailing (unsafe; local only)")
    args = parser.parse_args(argv)

    if args.create_dir is not None:
        if args.tail is not None or args.raw_tail:
            parser.error("--tail/--raw-tail cannot be used with --create-dir")
        if not args.component:
            parser.error("--component is required with --create-dir")
        try:
            path = create_log(
                args.create_dir,
                args.component,
                max_runs=_positive_env(
                    "ECHOECHO_CONSOLE_MAX_RUNS", DEFAULT_MAX_RUNS,
                    MAX_RUNS_LIMIT),
            )
        except (OSError, ValueError) as exc:
            print(f"could not create console log: {exc}", file=sys.stderr)
            return 1
        print(path)
        return 0

    if args.latest_dir is not None:
        if args.tail is not None or args.raw_tail:
            parser.error("--tail/--raw-tail cannot be used with --latest-dir")
        if not args.component:
            parser.error("--component is required with --latest-dir")
        try:
            path = latest_log(args.latest_dir, args.component)
        except (OSError, ValueError) as exc:
            print(f"console log unavailable: {exc}", file=sys.stderr)
            return 1
        print(path)
        return 0

    if args.component:
        parser.error("--component requires --create-dir or --latest-dir")
    assert args.log is not None

    if args.tail is not None:
        if args.tail <= 0:
            parser.error("--tail must be positive")
        try:
            tail(args.log, args.tail, sys.stdout.buffer, raw=args.raw_tail)
        except (OSError, ValueError) as exc:
            print(f"console log unavailable: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.raw_tail:
        parser.error("--raw-tail requires --tail")

    max_bytes = _positive_env(
        "ECHOECHO_CONSOLE_MAX_BYTES", DEFAULT_MAX_BYTES, MAX_BYTES_LIMIT)
    max_parts = _positive_env(
        "ECHOECHO_CONSOLE_MAX_PARTS", DEFAULT_MAX_PARTS, MAX_PARTS_LIMIT)
    capture(sys.stdin.buffer, args.log, max_bytes=max_bytes, max_parts=max_parts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
