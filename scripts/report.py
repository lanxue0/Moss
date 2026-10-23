#!/usr/bin/env python3
"""report.py — 把片段整理成人能读的日报，并生成给模型的分析提示词

输出三样东西（都在 reports/ 下）：
  1. 终端摘要
  2. YYYY-MM-DD.md        —— 你自己看的当日原始结构（谁用了多久、在哪些窗口、什么时候离席）
  3. YYYY-MM-DD.prompt.md —— 拼好 goals.md + 当日数据 + 输出要求，粘给任何模型即可

用法：
  python report.py                # 今天
  python report.py --date 2026-10-22
"""

import argparse
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "activity.db"
REPORT_DIR = BASE_DIR / "reports"
GOALS = BASE_DIR / "goals.md"

GAP_SPLIT = 90          # 同应用内间隔超过这个秒数就算两次会话
AWAY = 300              # 间隔超过这个秒数算"离席"
PREVIEW_CHARS = 72


def fmt_dur(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    h, m = divmod(sec // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def hhmm(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def load_segments(day: date):
    start = int(datetime(day.year, day.month, day.day).timestamp())
    end = start + 86400
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT start_ts, end_ts, exe, title, url FROM segments
           WHERE end_ts >= ? AND start_ts < ? ORDER BY start_ts""",
        (start, end),
    ).fetchall()
    conn.close()
    out = []
    for s, e, exe, title, url in rows:
        s, e = max(s, start), min(e, end)
        if e > s:
            out.append((s, e, exe or "", title or "", url or ""))
    return out


def build_sessions(segs):
    """按 应用 + 间隔 切会话，保留会话内出现过的窗口标题和 URL。"""
    sessions = []
    for s, e, exe, title, url in segs:
        last = sessions[-1] if sessions else None
        if last and last["exe"] == exe and s - last["end"] <= GAP_SPLIT:
            last["end"] = max(last["end"], e)
            last["titles"][title] += e - s
            if url:
                last["urls"][url] += e - s
        else:
            sessions.append({
                "exe": exe, "start": s, "end": e,
                "titles": Counter({title: e - s}),
                "urls": Counter({url: e - s} if url else {}),
            })
    return sessions


def domain_of(url):
    try:
        host = url.split("//", 1)[-1].split("/")[0]
        return host.replace("www.", "")
    except Exception:
        return url


def render(day, segs, sessions):
    lines = [f"# {day.isoformat()} 原始记录", ""]
    if not segs:
        lines += ["今天没有采到任何数据。", ""]
        return "\n".join(lines), {}

    tracked = sum(e - s for s, e, *_ in segs)
    first, last = segs[0][0], segs[-1][1]

    per_app = defaultdict(int)
    for s, e, exe, *_ in segs:
        per_app[exe] += e - s
    per_domain = Counter()
    for s, e, exe, title, url in segs:
        if url:
            per_domain[domain_of(url)] += e - s

    lines += [
        f"- 首次活动 {hhmm(first)}，最后活动 {hhmm(last)}",
        f"- 有记录的时长 {fmt_dur(tracked)}（不含离席）",
        "",
        "## 按应用",
        "",
        "| 应用 | 时长 | 占比 |",
        "|---|---|---|",
    ]
    for exe, d in sorted(per_app.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {exe} | {fmt_dur(d)} | {d / tracked:.0%} |")

    if per_domain:
        lines += ["", "## 按网站", "", "| 域名 | 时长 |", "|---|---|"]
        for host, d in per_domain.most_common(12):
            lines.append(f"| {host} | {fmt_dur(d)} |")

    lines += ["", "## 时间线", ""]
    prev_end = None
    for ss in sessions:
        if prev_end is not None and ss["start"] - prev_end >= AWAY:
            lines.append(f"· 离席 {fmt_dur(ss['start'] - prev_end)}"
                         f"（{hhmm(prev_end)} → {hhmm(ss['start'])}）")
            lines.append("")
        top_title, top_dur = ss["titles"].most_common(1)[0]
        preview = top_title[:PREVIEW_CHARS] + ("…" if len(top_title) > PREVIEW_CHARS else "")
        extra = f"，还有 {len(ss['titles']) - 1} 个窗口" if len(ss["titles"]) > 1 else ""
        lines.append(f"**{hhmm(ss['start'])}–{hhmm(ss['end'])}** "
                     f"{ss['exe']} · {fmt_dur(ss['end'] - ss['start'])}")
        lines.append(f"　　{preview}{extra}")
        for url, _ in ss["urls"].most_common(2):
            lines.append(f"　　↳ {url[:100]}")
        lines.append("")
        prev_end = ss["end"]

    stats = {"tracked": tracked, "first": first, "last": last, "per_app": dict(per_app)}
    return "\n".join(lines), stats


PROMPT_TEMPLATE = """你是我的个人助手。下面是我今天在电脑上实际做过的事（自动采集，可能有误差），以及我自己写的目标。

请严格按下面规则写日报：

1. 只根据数据说话。数据里看不出来的事，就写「看不出来」，绝对不要推测或编造。
2. 不评价我，不说「你应该」「你需要加强」。只陈述事实，需要提建议时给选项。
3. 时长的判断留有余量，不要精确到分钟地指责我。
4. 如果今天有效数据不足 30 分钟，只回一句「今天数据太少，跳过」，不要硬凑。

输出格式（Markdown，不要额外开场白）：

## 今天实际做了什么
（3–5 条，每条：时间 + 在做什么 + 时长。按真实数据）

## 和我的目标的关系
（逐个目标对照：今天有没有推进、推进了多少。数据不支持就明确说「今天没看到相关活动」）

## 一个我可能没注意到的点
（最多一条，必须是数据里能看出来的模式，比如「同一个页面开了三次」、「集中在下午」、「切换太碎」。没有就写「今天没什么异常」）

## 一句话
（不超过 25 字，像朋友随口说的，不要打鸡血）

---

## 我的目标（手写）

{goals}

## 今天的数据

{data}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()
    day = date.fromisoformat(args.date)

    segs = load_segments(day)
    sessions = build_sessions(segs)
    body, stats = render(day, segs, sessions)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / f"{day.isoformat()}.md").write_text(body, encoding="utf-8")

    goals = GOALS.read_text(encoding="utf-8") if GOALS.exists() else "（还没写目标）"
    prompt = PROMPT_TEMPLATE.format(goals=goals.strip(), data=body)
    (REPORT_DIR / f"{day.isoformat()}.prompt.md").write_text(prompt, encoding="utf-8")

    print(body)
    print("-" * 60)
    if stats:
        print(f"已生成：reports/{day.isoformat()}.md")
        print(f"        reports/{day.isoformat()}.prompt.md  ← 粘给模型出日报")


if __name__ == "__main__":
    main()
