#!/usr/bin/env python3
"""observer.py — 本地活动观察器

Windows / 无第三方依赖 / 数据只写本地 SQLite。

设计原则（对应设计稿「感知层」）：
  1. 只记"你在用什么、在什么窗口"，不记键盘内容；
  2. 3 秒一采样，合并成"片段(segment)"，片段按 (进程, 标题) 变化自动切分；
  3. 空闲超过阈值就断开片段，空隙在日报里显示为"离席"；
  4. 浏览器 URL 用 Chrome DevTools Protocol 补全（可选，只读本地 9222 端口）；
  5. 放一个 PAUSE 文件即可暂停，删掉即恢复。

用法：
  python observer.py                # 常驻，Ctrl+C 退出
  python observer.py --once         # 只采样一次，打印，便于排查
  python observer.py --interval 5   # 采样间隔（秒）
"""

import argparse
import ctypes
import json
import os
import sqlite3
import sys
import time
import urllib.request
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "activity.db"
PAUSE_FLAG = DATA_DIR / "PAUSE"

IDLE_SEC = 120          # 空闲超过这个秒数，判定为离席
CDP_PORT = 9222         # Chrome/Edge 远程调试端口，没有就自动跳过
BROWSERS = {"chrome.exe", "msedge.exe", "brave.exe", "vivaldi.exe", "opera.exe"}

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def foreground_window():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None, ""
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return hwnd, buf.value


def window_pid(hwnd):
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_name(pid):
    if not pid:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(handle)


def idle_seconds():
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    tick = kernel32.GetTickCount()
    return ((tick - info.dwTime) & 0xFFFFFFFF) / 1000.0


def cdp_active_url(window_title):
    """从本机 CDP 端口里挑出和当前窗口标题最匹配的标签页 URL。失敗就返回空。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=0.4) as resp:
            tabs = json.load(resp)
    except Exception:
        return ""
    best, best_score = "", 0
    for tab in tabs:
        if tab.get("type") != "page":
            continue
        title, url = tab.get("title", ""), tab.get("url", "")
        if not url or url.startswith(("chrome://", "edge://", "devtools://")):
            continue
        score = 0
        if title and title[:18] and title[:18] in window_title:
            score = len(title)
        if score > best_score:
            best, best_score = url, score
    return best


SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts  INTEGER NOT NULL,
    end_ts    INTEGER NOT NULL,
    exe       TEXT,
    title     TEXT,
    url       TEXT
);
CREATE INDEX IF NOT EXISTS idx_segments_start ON segments(start_ts);
"""


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


class Segment:
    __slots__ = ("row_id", "exe", "title", "url")

    def __init__(self, row_id, exe, title, url):
        self.row_id, self.exe, self.title, self.url = row_id, exe, title, url


class Observer:
    def __init__(self, conn, interval, verbose=True):
        self.conn = conn
        self.interval = interval
        self.verbose = verbose
        self.current = None
        self.last_report = 0.0

    def close_current(self, now):
        if self.current:
            self.conn.execute(
                "UPDATE segments SET end_ts=? WHERE id=?", (now, self.current.row_id)
            )
            self.conn.commit()
            self.current = None

    def open_segment(self, now, exe, title, url):
        cur = self.conn.execute(
            "INSERT INTO segments (start_ts, end_ts, exe, title, url) VALUES (?,?,?,?,?)",
            (now, now, exe, title, url),
        )
        self.conn.commit()
        self.current = Segment(cur.lastrowid, exe, title, url)

    def tick(self):
        now = int(time.time())

        if PAUSE_FLAG.exists():
            self.close_current(now)
            return

        idle = idle_seconds()
        if idle >= IDLE_SEC:
            self.close_current(now)
            return

        hwnd, title = foreground_window()
        if not hwnd:
            self.close_current(now)
            return

        exe = process_name(window_pid(hwnd))
        url = cdp_active_url(title) if exe.lower() in BROWSERS else ""

        cur = self.current
        if cur and cur.exe == exe and cur.title == title:
            # 同一片段，只延长时间；URL 变了就补记
            self.conn.execute(
                "UPDATE segments SET end_ts=?, url=? WHERE id=?",
                (now, url or cur.url, cur.row_id),
            )
            self.conn.commit()
            cur.url = url or cur.url
        else:
            self.close_current(now)
            self.open_segment(now, exe, title, url)
            if self.verbose:
                stamp = datetime.now().strftime("%H:%M:%S")
                shown = title[:60] + ("…" if len(title) > 60 else "")
                print(f"[{stamp}] {exe:<18} {shown}")
                if url:
                    print(f"{'':>12}↳ {url[:90]}")

    def run(self):
        print(f"观察中 · 每 {self.interval}s 采样 · 数据写入 {DB_PATH}")
        print(f"暂停：在 data/ 下新建 PAUSE 文件  退出：Ctrl+C")
        try:
            while True:
                try:
                    self.tick()
                except Exception as exc:  # 单次采样失敗不该让进程死掉
                    print(f"! 采样异常（已跳过）：{exc}")
                time.sleep(self.interval)
        except KeyboardInterrupt:
            self.close_current(int(time.time()))
            self.conn.commit()
            print("\n已停止，当前片段已落盘。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--once", action="store_true", help="只采样一次并打印")
    args = ap.parse_args()

    if args.once:
        hwnd, title = foreground_window()
        exe = process_name(window_pid(hwnd)) if hwnd else ""
        print(json.dumps(
            {
                "exe": exe,
                "title": title,
                "idle_sec": round(idle_seconds(), 1),
                "url": cdp_active_url(title) if exe.lower() in BROWSERS else "",
            },
            ensure_ascii=False,
            indent=2,
        ))
        return

    Observer(connect(), args.interval).run()


if __name__ == "__main__":
    main()
