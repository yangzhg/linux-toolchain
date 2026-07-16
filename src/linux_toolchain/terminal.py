from __future__ import annotations

import os
import shutil
import time
from typing import TextIO

BOLD = "1"
RED = "31"
GREEN = "32"
YELLOW = "33"
CYAN = "36"
_NON_LIVE_PROGRESS_INTERVAL = 30.0


def supports_color(stream: TextIO) -> bool:
    if "NO_COLOR" in os.environ:
        return False
    return supports_live_output(stream)


def supports_live_output(stream: TextIO) -> bool:
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return stream.isatty()
    except (AttributeError, OSError):
        return False


def style(text: str, *codes: str, enabled: bool) -> str:
    if not enabled or not codes:
        return text
    return f"\033[{';'.join(codes)}m{text}\033[0m"


def progress_line(message: str, *, color: bool) -> str:
    phase, separator, detail = message.partition(":")
    marker = style("==>", BOLD, CYAN, enabled=color)
    if not separator:
        return f"{marker} {message}"
    label = style(f"{phase}:", BOLD, enabled=color)
    detail = detail.lstrip()
    for status in ("DONE", "PASS"):
        if detail == status:
            result = style(status, BOLD, GREEN, enabled=color)
            return f"{marker} {label} {result}"
        suffix = f" {status}"
        if detail.endswith(suffix):
            detail = detail[: -len(suffix)]
            result = style(status, BOLD, GREEN, enabled=color)
            return f"{marker} {label} {detail} {result}"
    return f"{marker} {label} {detail}"


def _format_duration(seconds: float) -> str:
    value = max(0, int(seconds + 0.5))
    hours, remainder = divmod(value, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


class TerminalProgressDisplay:
    """Render durable progress lines and replace repeated live status on a TTY."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._live = supports_live_output(stream)
        self._color = supports_color(stream)
        self._live_active = False
        self._live_line_count = 0
        self._last_non_live_update: float | None = None

    def uses(self, stream: TextIO) -> bool:
        return self._stream is stream

    def write(
        self,
        message: str,
        *,
        replace: bool = False,
        live_message: str | None = None,
    ) -> None:
        if replace and self._live:
            self._clear_live()
            lines = self._render_live(live_message or message)
            self._stream.write("\n".join(lines))
            self._stream.flush()
            self._live_active = True
            self._live_line_count = len(lines)
            return

        if replace:
            now = time.monotonic()
            if (
                self._last_non_live_update is not None
                and now - self._last_non_live_update < _NON_LIVE_PROGRESS_INTERVAL
            ):
                return
            self._last_non_live_update = now
        else:
            self._last_non_live_update = None
        self._clear_live()
        self._stream.write(progress_line(message, color=self._color) + "\n")
        self._stream.flush()

    def finish(self) -> None:
        if self._live_active:
            self._stream.write("\n")
            self._stream.flush()
            self._live_active = False
            self._live_line_count = 0

    def _clear_live(self) -> None:
        for index in range(self._live_line_count):
            self._stream.write("\r\033[2K")
            if index + 1 < self._live_line_count:
                self._stream.write("\033[1A")
        self._live_active = False
        self._live_line_count = 0

    @staticmethod
    def _render_live(message: str) -> tuple[str, ...]:
        source_lines = message.splitlines() or [message]
        lines = [progress_line(source_lines[0], color=False)]
        lines.extend(f"    {line}" for line in source_lines[1:])
        width = max(1, shutil.get_terminal_size(fallback=(80, 24)).columns)
        return tuple(_clip_terminal_line(line, width) for line in lines)


def _clip_terminal_line(line: str, width: int) -> str:
    expanded = line.expandtabs(4)
    printable = "".join(
        character if character.isprintable() else " " for character in expanded
    )
    if len(printable) <= width:
        return printable
    if width == 1:
        return "…"
    return f"{printable[: width - 1]}…"


class TerminalProgressBar:
    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._enabled = supports_live_output(stream)
        self._color = supports_color(stream)
        self._last_percent = -1
        self._active = False
        self._finished = False
        self._started_at: float | None = None

    def update(self, completed: int, total: int) -> None:
        if not self._enabled:
            return
        if self._finished:
            if completed != 0:
                return
            self._last_percent = -1
            self._finished = False
            self._started_at = None
        bounded = max(0, min(completed, total))
        percent = 100 if total <= 0 else bounded * 100 // total
        if percent == self._last_percent:
            return
        self._last_percent = percent

        now = time.monotonic()
        if self._started_at is None:
            self._started_at = now
        elapsed = now - self._started_at
        if percent == 100:
            eta = "00:00"
        elif bounded > 0 and elapsed > 0:
            eta = _format_duration(elapsed * (total - bounded) / bounded)
        else:
            eta = "--:--"
        speed = (
            f"{bounded / elapsed / (1024 * 1024):,.1f} MiB/s"
            if bounded > 0 and elapsed > 0
            else "-- MiB/s"
        )

        width = 24
        filled_width = percent * width // 100
        indicator = ">" if filled_width < width else ""
        filled = "=" * filled_width + indicator
        empty = " " * (width - len(filled))
        rendered = style(filled, CYAN, enabled=self._color) + empty
        current_mib = bounded / (1024 * 1024)
        total_mib = total / (1024 * 1024)
        self._stream.write(
            f"\r    [{rendered}] {percent:3d}% "
            f"{current_mib:,.0f}/{total_mib:,.0f} MiB  {speed}  ETA {eta}"
        )
        self._stream.flush()
        self._active = True
        if percent == 100:
            self._stream.write("\n")
            self._stream.flush()
            self._active = False
            self._finished = True

    def close(self) -> None:
        if self._active:
            self._stream.write("\n")
            self._stream.flush()
            self._active = False
