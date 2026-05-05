#!/usr/bin/env python3
"""Tactical Systems HUD

Lightweight terminal HUD for system telemetry with process targeting and log tailing.
Designed for low overhead / long-running sessions.
"""

from __future__ import annotations

import argparse
import collections
import curses
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Deque, Iterable, Optional

import psutil


@dataclass
class ProcTarget:
    pid: int
    name: str


class LogTailer:
    """Non-blocking line tailer for a file descriptor or subprocess stdout."""

    def __init__(self, file_path: Optional[str] = None, tail_command: Optional[str] = None, max_lines: int = 200) -> None:
        self.max_lines = max_lines
        self.lines: Deque[str] = collections.deque(maxlen=max_lines)
        self.file_path = file_path
        self.tail_command = tail_command
        self._fh = None
        self._proc = None

    def start(self) -> None:
        if self.file_path:
            try:
                self._fh = open(self.file_path, "r", encoding="utf-8", errors="replace")
                self._fh.seek(0, os.SEEK_END)
            except OSError as exc:
                self.lines.append(f"[tail] failed to open file: {exc}")
        if self.tail_command:
            try:
                self._proc = subprocess.Popen(
                    self.tail_command,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except OSError as exc:
                self.lines.append(f"[tail] failed to spawn command: {exc}")

    def poll(self) -> None:
        if self._fh is not None:
            while True:
                line = self._fh.readline()
                if not line:
                    break
                self.lines.append(line.rstrip())

        if self._proc is not None and self._proc.stdout is not None:
            while True:
                line = self._proc.stdout.readline()
                if not line:
                    break
                self.lines.append(line.rstrip())

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        if self._proc is not None:
            self._proc.terminate()
            self._proc = None


class ThermalPredictor:
    """Predictive thermal warning using slope of recent temperature samples."""

    def __init__(self, horizon_seconds: float = 30.0, warning_c: float = 65.0, critical_c: float = 75.0) -> None:
        self.horizon = horizon_seconds
        self.warning = warning_c
        self.critical = critical_c
        self.history: Deque[tuple[float, float]] = collections.deque(maxlen=30)

    def update(self, ts: float, temp_c: Optional[float]) -> tuple[str, Optional[float], float]:
        if temp_c is None:
            return ("UNKNOWN", None, 0.0)

        self.history.append((ts, temp_c))
        if len(self.history) < 2:
            if temp_c >= self.critical:
                return ("CRITICAL", temp_c, 0.0)
            if temp_c >= self.warning:
                return ("WARNING", temp_c, 0.0)
            return ("NOMINAL", temp_c, 0.0)

        t0, v0 = self.history[0]
        t1, v1 = self.history[-1]
        dt = max(1e-6, t1 - t0)
        slope = (v1 - v0) / dt
        projected = temp_c + slope * self.horizon

        if temp_c >= self.critical or projected >= self.critical:
            return ("CRITICAL", projected, slope)
        if temp_c >= self.warning or projected >= self.warning:
            return ("WARNING", projected, slope)
        return ("NOMINAL", projected, slope)


def _mem_fields() -> dict[str, float]:
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    total = vm.total or 1
    used_active = getattr(vm, "used", 0)
    cached = getattr(vm, "cached", 0) + getattr(vm, "buffers", 0)
    freeish = getattr(vm, "available", 0)
    return {
        "ram_total": vm.total,
        "ram_active": used_active,
        "ram_cached_buffers": cached,
        "ram_available": freeish,
        "swap_used": sm.used,
        "swap_total": sm.total,
        "active_pct": (used_active / total) * 100.0,
        "cache_pct": (cached / total) * 100.0,
        "avail_pct": (freeish / total) * 100.0,
    }


def _cpu_temp_c() -> Optional[float]:
    try:
        temps = psutil.sensors_temperatures(fahrenheit=False)
    except Exception:
        return None

    if not temps:
        return None

    preferred = []
    fallback = []
    for _, entries in temps.items():
        for t in entries:
            if t.current is None:
                continue
            label = (t.label or "").lower()
            if any(x in label for x in ("cpu", "soc", "package")):
                preferred.append(float(t.current))
            fallback.append(float(t.current))

    samples = preferred or fallback
    if not samples:
        return None
    return max(samples)


def _find_target(pid: Optional[int], match: Optional[str]) -> Optional[ProcTarget]:
    if pid is not None:
        try:
            p = psutil.Process(pid)
            return ProcTarget(pid=pid, name=p.name())
        except psutil.Error:
            return None

    if not match:
        return None
    m = match.lower()
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = " ".join(p.info.get("cmdline") or [])
            name = p.info.get("name") or ""
            hay = f"{name} {cmd}".lower()
            if m in hay:
                return ProcTarget(pid=p.info["pid"], name=name or "unknown")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def _fmt_bytes(n: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    x = float(n)
    idx = 0
    while x >= 1024 and idx < len(units) - 1:
        x /= 1024
        idx += 1
    return f"{x:5.1f} {units[idx]}"


def _safe_addstr(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    text = text[: max(0, w - x - 1)]
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass


def draw_loop(stdscr, args: argparse.Namespace) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(0)

    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)

    predictor = ThermalPredictor(
        horizon_seconds=args.thermal_horizon,
        warning_c=args.temp_warning,
        critical_c=args.temp_critical,
    )

    tailer = LogTailer(file_path=args.tail_file, tail_command=args.tail_cmd, max_lines=args.max_log_lines)
    tailer.start()

    proc_obj = None
    target = _find_target(args.pid, args.match)
    if target:
        try:
            proc_obj = psutil.Process(target.pid)
            proc_obj.cpu_percent(None)
        except psutil.Error:
            proc_obj = None

    last = time.monotonic()

    while True:
        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            break

        now = time.monotonic()
        if now - last < args.interval:
            time.sleep(0.02)
            continue
        elapsed = now - last
        last = now

        try:
            mem = _mem_fields()
            cpu_total = psutil.cpu_percent(interval=None)
            temp_c = _cpu_temp_c()
            thermal, projected, slope = predictor.update(now, temp_c)
            if tailer:
                tailer.poll()

            proc_line = "Process target: none"
            if proc_obj is not None:
                try:
                    with proc_obj.oneshot():
                        p_cpu = proc_obj.cpu_percent(None)
                        p_mem = proc_obj.memory_info().rss
                        p_stat = proc_obj.status()
                    proc_line = f"Target PID {proc_obj.pid} ({proc_obj.name()}): CPU {p_cpu:5.1f}% | RSS {_fmt_bytes(p_mem)} | {p_stat}"
                except psutil.Error:
                    proc_line = "Target process ended or inaccessible"
                    proc_obj = None

            stdscr.erase()
            h, _ = stdscr.getmaxyx()
            y = 0

            _safe_addstr(stdscr, y, 0, "TACTICAL SYSTEMS HUD  |  q to quit")
            y += 1
            _safe_addstr(stdscr, y, 0, f"Refresh {args.interval:.2f}s | loop dt {elapsed:.2f}s")
            y += 2

            _safe_addstr(stdscr, y, 0, f"CPU Total: {cpu_total:5.1f}%")
            y += 1
            _safe_addstr(stdscr, y, 0, f"RAM Active: {_fmt_bytes(mem['ram_active'])} ({mem['active_pct']:5.1f}%)")
            y += 1
            _safe_addstr(stdscr, y, 0, f"RAM Cached+Buffers: {_fmt_bytes(mem['ram_cached_buffers'])} ({mem['cache_pct']:5.1f}%)")
            y += 1
            _safe_addstr(stdscr, y, 0, f"RAM Available: {_fmt_bytes(mem['ram_available'])} ({mem['avail_pct']:5.1f}%)")
            y += 1
            _safe_addstr(stdscr, y, 0, f"Swap: {_fmt_bytes(mem['swap_used'])} / {_fmt_bytes(mem['swap_total'])}")
            y += 2

            if thermal == "CRITICAL":
                attr = curses.color_pair(3) | curses.A_BOLD
            elif thermal == "WARNING":
                attr = curses.color_pair(2) | curses.A_BOLD
            else:
                attr = curses.color_pair(1)

            temp_txt = "n/a" if temp_c is None else f"{temp_c:.1f}C"
            proj_txt = "n/a" if projected is None else f"{projected:.1f}C"
            _safe_addstr(
                stdscr,
                y,
                0,
                f"Thermal: {thermal} | current {temp_txt} | +{args.thermal_horizon:.0f}s proj {proj_txt} | slope {slope:+.3f}C/s",
                attr,
            )
            y += 2

            _safe_addstr(stdscr, y, 0, proc_line)
            y += 2

            _safe_addstr(stdscr, y, 0, "Log tail:")
            y += 1
            visible = max(0, h - y - 1)
            lines = list(tailer.lines)[-visible:]
            for line in lines:
                _safe_addstr(stdscr, y, 0, line)
                y += 1

            stdscr.refresh()

        except Exception as exc:
            # Graceful degradation: tolerate transient lockups/system read failures.
            stdscr.erase()
            _safe_addstr(stdscr, 0, 0, "Transient telemetry stall detected. Recovering...", curses.A_BOLD)
            _safe_addstr(stdscr, 1, 0, str(exc)[:120])
            stdscr.refresh()
            time.sleep(min(3.0, args.interval * 2))

    tailer.close()


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Tactical Systems HUD")
    p.add_argument("--interval", type=float, default=1.0, help="sampling interval seconds")
    p.add_argument("--pid", type=int, default=None, help="target PID")
    p.add_argument("--match", type=str, default=None, help="target process name/cmd substring")
    p.add_argument("--tail-file", type=str, default=None, help="tail this logfile")
    p.add_argument("--tail-cmd", type=str, default=None, help="execute command and stream output")
    p.add_argument("--max-log-lines", type=int, default=250, help="stored log lines")
    p.add_argument("--thermal-horizon", type=float, default=30.0, help="prediction horizon seconds")
    p.add_argument("--temp-warning", type=float, default=65.0, help="warning threshold in C")
    p.add_argument("--temp-critical", type=float, default=75.0, help="critical threshold in C")
    return p.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)

    def _sigint(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint)

    try:
        curses.wrapper(draw_loop, args)
        return 0
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
