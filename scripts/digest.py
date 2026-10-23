#!/usr/bin/env python3
"""digest.py — 每日提交总览：今天到底做了什么

和 daily_report.py 的分工：
  daily_report.py  面向「交付」——按固定工作日志模板写进 labbol工作日志 仓库。
  digest.py        面向「回顾」——把当天所有仓库的提交汇总成一份总结，
                   不管是不是工作，不套模板，只回答一个问题：今天总共做了什么。

为什么单独一个脚本：工作日志有固定受众人（要年/月/Week 目录、索引、周总结），
而"我今天干了啥"不需要那套结构，硬塞进去只会互相拖累。

只写 git 能证明的事实，不猜、不评价、不编造动机。
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.parse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_report as dr  # noqa: E402  复用 run_git / discover_repos，避免重复实现

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "digest_config.json"
WEEKDAYS = "一二三四五六日"

# conventional commit 类型
TYPE_RE = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert|wip)"
    r"(?:\(([^)]+)\))?[!]?\s*:\s*", re.I)
TYPE_CN = {
    "feat": "新功能", "fix": "修复", "docs": "文档", "style": "格式",
    "refactor": "重构", "perf": "性能", "test": "测试", "build": "构建",
    "ci": "CI", "chore": "杂务", "revert": "回滚", "wip": "半成品",
}

DEFAULT_CONFIG = {
    "_doc": [
        "digest.py 的配置。全部有默认值，不建这个文件也能跑。",
        "search_roots 越大扫描越慢，只填真正放代码的目录。",
        "classify.groups 按关键词把仓库归到「工作 / 个人」等类别，先匹配先得；",
        "拿不准就在 overrides 里按完整路径指定。",
    ],
    "git": {
        "search_roots": ["D:\\yzj"],
        "auto_roots": True,
        "max_depth": 4,
        "exclude": ["labbol工作日志"],
        "author_emails": ["1259700509@qq.com"],
        "only_mine": True,
        "skip_merge": True,
    },
    "classify": {
        "groups": [
            {"name": "工作", "emoji": "🏢", "keywords": [
                "labbol", "mentor", "dcc", "digitalemployee", "digital-employee",
                "portal", "product-yun", "apicurio", "materia", "knowledge-view"]},
            {"name": "个人", "emoji": "🧪", "keywords": ["mastra", "xiaole", "test-", "demo", "play"]},
        ],
        "overrides": {},
        "default_group": {"name": "其他", "emoji": "📦"},
    },
    "output": {
        "dir": "out/digest",
        "filename": "{date}.md",
        # 想归档到某个 git 仓库就填绝对路径，留空则只写本地文件
        "repo": "",
        "subdir": "digest",
        "commit": False,
        "push": False,
        "commit_message": "docs: digest {date}",
    },
}


# ------------------------------------------------------------------ 配置

def deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    if CONFIG_PATH.exists():
        try:
            return deep_merge(DEFAULT_CONFIG, json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as exc:
            print(f"! 配置文件解析失败，用默认值：{exc}")
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def editor_roots(limit=15):
    """从 Trae / VSCode 的「最近打开」里捞项目目录。

    比全盘扫描便宜得多，而且捞到的恰好是你真的在用、真的会提交的那些。
    光靠 search_roots 硬编码，项目一换盘就漏。
    """
    found = []
    for name in ("Trae CN", "Trae", "Code", "Cursor"):
        db = Path(os.path.expandvars(r"%APPDATA%")) / name / "User" / "globalStorage" / "state.vscdb"
        if not db.exists():
            continue
        try:
            uri = "file:" + urllib.parse.quote(db.as_posix()) + "?mode=ro&immutable=1"
            conn = sqlite3.connect(uri, uri=True)
            row = conn.execute("SELECT value FROM ItemTable "
                               "WHERE key='history.recentlyOpenedPathsList'").fetchone()
            conn.close()
        except Exception:
            continue
        if not row:
            continue
        try:
            entries = json.loads(row[0]).get("entries", [])
        except json.JSONDecodeError:
            continue
        for e in entries:
            u = e.get("folderUri") or e.get("fileUri") or ""
            if not u.startswith("file:///"):
                continue
            path = urllib.parse.unquote(u[8:]).replace("/", "\\").rstrip("\\")
            # 只要真实存在的目录；.code-workspace 这类文件对找仓库没帮助
            if os.path.isdir(path) and path not in found:
                found.append(path)
    return found[:limit]


def all_repos(cfg):
    """search_roots 按 max_depth 深扫；编辑器最近项目只扫一层。

    后者可能是个大工程目录，深扫会拖慢整体，而仓库通常就在它本身或下一层。
    """
    ex = cfg["git"].get("exclude", [])
    repos = list(dr.discover_repos(cfg["git"]["search_roots"], cfg["git"]["max_depth"], ex))
    if cfg["git"].get("auto_roots", True):
        extra = editor_roots()
        if extra:
            repos += dr.discover_repos(extra, 1, ex)
    seen, out = set(), []
    for p in repos:
        k = str(p).lower().replace("/", "\\")
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


# ------------------------------------------------------------------ 采集

def scan_repos(repos, since_day, timeout=300):
    """一次 git log 把区间内所有提交连同文件变更量读进来。

    比逐条 git show 快一个数量级，且 numstat 和 pretty 同源，
    不会出现"提交清单和改动对不上"的情况。
    """
    by_day = defaultdict(list)
    for repo in repos:
        raw = dr.run_git(
            repo, "log", "--all", f"--since={since_day.isoformat()} 00:00",
            "--numstat", "--pretty=format:@@%H|%aI|%ae|%s", timeout=timeout)
        if not raw:
            continue
        cur = None
        for line in raw.splitlines():
            if line.startswith("@@"):
                parts = line[2:].split("|", 3)
                if len(parts) < 4:
                    cur = None
                    continue
                sha, iso, email, subject = parts
                cur = {"sha": sha, "iso": iso, "day": iso[:10], "email": email or "",
                       "subject": subject, "files": [], "adds": 0, "dels": 0, "repo": repo}
                by_day[cur["day"]].append(cur)
            elif cur is not None and line.strip():
                p = line.split("\t")
                if len(p) != 3:
                    continue
                a, d, path = p
                cur["files"].append(path)
                cur["adds"] += int(a) if a.isdigit() else 0
                cur["dels"] += int(d) if d.isdigit() else 0
    return by_day


def repo_label(repo, all_repos):
    """同名仓库用「父目录/名字」区分，否则两份 MentorDocwebjs 会混成一个。"""
    names = Counter(Path(p).name for p in all_repos)
    return Path(repo).name if names[Path(repo).name] == 1 else f"{Path(repo).parent.name}/{Path(repo).name}"


def classify(repo_path, cfg):
    low = str(repo_path).lower().replace("/", "\\")
    for path, name in (cfg["classify"].get("overrides") or {}).items():
        if str(path).lower().replace("/", "\\") == low:
            return name
    for g in cfg["classify"].get("groups", []):
        if any(k.lower() in low for k in g.get("keywords", [])):
            return g["name"]
    return cfg["classify"]["default_group"]["name"]


def group_meta(name, cfg):
    for g in cfg["classify"].get("groups", []):
        if g["name"] == name:
            return g
    d = cfg["classify"]["default_group"]
    return {"name": d["name"], "emoji": d.get("emoji", "📦")}


def parse_subject(subject):
    m = TYPE_RE.match(subject.strip())
    if not m:
        return "其他", "", subject.strip()
    typ = m.group(1).lower()
    return typ, (m.group(2) or ""), subject.strip()[m.end():] or subject.strip()


def collect(day, cfg, cache=None):
    """cache = (repos, by_day)。补多天时复用同一次扫描，否则每天重扫一遍 19 个仓库。"""
    if cache is None:
        repos = all_repos(cfg)
        # 往前多扫一天，避免时区/跨零点的边界漏提交
        by_day = scan_repos(repos, day - timedelta(days=1))
    else:
        repos, by_day = cache

    rows = by_day.get(day.isoformat(), [])
    authors = [a.lower() for a in cfg["git"].get("author_emails", []) if a]
    if cfg["git"].get("only_mine") and authors:
        rows = [r for r in rows if any(a in r["email"].lower() for a in authors)]
    if cfg["git"].get("skip_merge"):
        rows = [r for r in rows if not r["subject"].startswith("Merge")]
    rows = [r for r in rows if r["files"]]  # 空提交（--allow-empty）不算干活
    rows.sort(key=lambda r: r["iso"])

    for r in rows:
        r["label"] = repo_label(r["repo"], repos)
        r["group"] = classify(r["repo"], cfg)
        r["type"], r["scope"], r["text"] = parse_subject(r["subject"])

    # 同 remote 的多份clone（你机器上就有两处 product-yun-knowledge-view）
    # 会让同一批提交被算两遍，统计直接翻倍。sha 全局唯一，按它去重最可靠。
    seen, deduped = {}, []
    for r in rows:
        if r["sha"] in seen:
            seen[r["sha"]]["clones"].append(r["label"])
            continue
        r["clones"] = []
        seen[r["sha"]] = r
        deduped.append(r)
    rows = deduped

    # 去重后可能剩下某个仓库的独立提交，此时也要重新归组
    return rows, repos


def uniq_files(rows):
    """按「仓库+路径」去重：不同仓库里的 src/index.ts 是两个文件，不是同一个。"""
    return len({(r["label"], f) for r in rows for f in r["files"]})


# ------------------------------------------------------------------ 渲染

def fmt_period(rows):
    """09:04 → 17:02，分 2 个时段

    不写"跨度 8 小时"：首末提交之间可能空了 8 小时没碰电脑，
    那个数字看着像连续工作，是假的。分几个时段才是真的。
    """
    if not rows:
        return ""
    times = [r["iso"][11:16] for r in rows]
    t0, t1 = times[0], times[-1]
    if t0 == t1:
        return f"提交时间 {t0}"
    n = len(active_ranges(rows))
    return f"{t0} → {t1}" + (f"，分 {n} 个时段" if n > 1 else "")


def hour_slots(rows):
    """按小时聚一次，用来还原一天的节奏。"""
    buckets = defaultdict(list)
    for r in rows:
        buckets[int(r["iso"][11:13])].append(r)
    return [(h, buckets[h]) for h in sorted(buckets)]


def active_ranges(rows, gap=1):
    """把有提交的小时连成区间：09-12、14-18、21-23。"""
    hours = sorted({int(r["iso"][11:13]) for r in rows})
    if not hours:
        return []
    spans, cur = [], [hours[0]]
    for h in hours[1:]:
        if h - cur[-1] <= gap:
            cur.append(h)
        else:
            spans.append(cur)
            cur = [h]
    spans.append(cur)
    return [(s[0], s[-1]) for s in spans]


def trunc(text, width):
    """按语义截断：优先断在标点处，避免出现「（P2 等 5 条」这种半个括号。"""
    s = (text or "").strip()
    if len(s) <= width:
        return s
    cut = s[:width]
    for sep in ("；", "、", "，", "。", "/", "（", "(", " "):
        i = cut.rfind(sep)
        if i >= width * 0.55:
            return cut[:i].rstrip() + "…"
    return cut.rstrip() + "…"


def brief(items, n=3, width=32):
    """取前 n 条并截断，多了就说「等 N 项」——宁可少说，也不要糊成一团。"""
    items = [x for x in items if x]
    shown = [trunc(x, width) for x in items[:n]]
    if len(items) > n:
        return "、".join(shown) + f" 等 {len(items)} 项"
    return "、".join(shown)


VERB = {"feat": "推进", "fix": "修复", "refactor": "重构", "perf": "优化性能",
        "docs": "补文档", "test": "补测试", "style": "格式整理", "chore": "杂务",
        "build": "构建", "ci": "流水线", "revert": "回滚", "wip": "未完成", "其他": "其他"}


def render_narrative(rows):
    """按项目把提交组织成人话。

    只做归类和连接，不加「显著提升了」「成功解决」这类评价 —— 评价是使用者自己的事，
    脚本一旦开始替人下结论，这份总结就不可信了。
    """
    per_repo = defaultdict(list)
    for r in rows:
        per_repo[r["label"]].append(r)

    lines = []
    for label, rs in sorted(per_repo.items(), key=lambda kv: -len(kv[1])):
        by_type = defaultdict(list)
        for r in rs:
            by_type[r["type"]].append(r["text"])
        lines.append(f"**{label}**（{len(rs)} 次）")
        # feat/fix 最值得先说，其余按类型合并成一行
        order = ["feat", "fix", "refactor", "perf", "docs", "test", "style", "chore", "wip", "其他"]
        for t in order:
            if not by_type.get(t):
                continue
            if t in ("style", "chore", "其他"):
                continue  # 收尾工作单独合并
            lines.append(f"- {VERB.get(t, t)}：{brief(by_type[t])}")
        tail = [x for t in ("style", "chore", "其他") for x in by_type.get(t, [])]
        if tail:
            lines.append(f"- 收尾：{brief(tail, n=2)}")
        lines.append("")
    return lines


def render_headline(day, rows):
    """把数字拼成一句人话。只陈述事实，不做评价。"""
    if not rows:
        return "今天没有提交。"

    n = len(rows)
    per_repo = Counter(r["label"] for r in rows)
    per_type = Counter(r["type"] for r in rows)
    per_group = Counter(r["group"] for r in rows)
    top = per_repo.most_common()
    types = [(t, c) for t, c in per_type.most_common(2) if t != "其他"]

    s = ""
    if len(top) == 1:
        s = f"今天 {n} 次提交，全部在 {top[0][0]}。"
    else:
        s = f"今天 {n} 次提交，分布在 {len(per_repo)} 个仓库。"
        s += f"最多的是 {top[0][0]}（{top[0][1]} 次），其次是 {top[1][0]}（{top[1][1]} 次）。"

    if types:
        s += "从类型看以 " + "、".join(f"{TYPE_CN.get(t, t)} {c} 次" for t, c in types) + " 为主。"

    if len(per_group) > 1:
        gs = "、".join(f"{g} {c} 次" for g, c in per_group.most_common())
        s += f"按类别：{gs}。"

    ranges = active_ranges(rows)
    if ranges:
        s += "活跃时段：" + "、".join(f"{a:02d}:00–{b + 1:02d}:00" for a, b in ranges) + "。"

    first, last = rows[0]["iso"][11:16], rows[-1]["iso"][11:16]
    s += f"第一次提交 {first}，最后一次 {last}。"
    return s


def render(day, rows, cfg):
    lines = [f"# {day.isoformat()} 周{WEEKDAYS[day.weekday()]} · 今日总结", ""]

    if not rows:
        lines += ["> 今天没有任何提交。", "",
                  "如果确实做了事但没提交，说明工作在 git 之外（读资料、讨论、设计、调试），"
                  "这部分 git 看不见，也不必为它编造内容。", ""]
        return "\n".join(lines)

    tot_add = sum(r["adds"] for r in rows)
    tot_del = sum(r["dels"] for r in rows)
    period = fmt_period(rows)
    lines += [f"> {len(rows)} 次提交 · {len({r['label'] for r in rows})} 个仓库 · "
              f"{uniq_files(rows)} 个文件 · +{tot_add}/-{tot_del}", f"> {period}", ""]

    # 人话在前，数字在后——先回答"今天做了什么"，再给证据
    lines += ["## 今天做了什么", ""] + render_narrative(rows)
    lines += ["## 概览", "", render_headline(day, rows), ""]

    per_group = defaultdict(list)
    for r in rows:
        per_group[r["group"]].append(r)
    if len(per_group) > 1:
        lines += ["| 类别 | 提交 | 涉及仓库 |", "|------|------|----------|"]
        for g, rs in sorted(per_group.items(), key=lambda kv: -len(kv[1])):
            meta = group_meta(g, cfg)
            names = "、".join(sorted({x["label"] for x in rs}))
            lines.append(f"| {meta.get('emoji', '')} {g} | {len(rs)} | {names} |")
        lines.append("")

    # 分项目明细
    lines += ["## 分项目", ""]
    per_repo = defaultdict(list)
    for r in rows:
        per_repo[r["label"]].append(r)
    ordered = sorted(per_repo.items(), key=lambda kv: -len(kv[1]))
    for label, rs in ordered:
        meta = group_meta(rs[0]["group"], cfg)
        a = sum(x["adds"] for x in rs)
        d = sum(x["dels"] for x in rs)
        lines += [f"### {meta.get('emoji', '')} {label} — {len(rs)} 次提交 · "
                  f"{uniq_files(rs)} 文件 · +{a}/-{d}", "",
                  "| 时间 | 提交 | 文件 | 变更 |", "|------|------|------|------|"]
        for r in rs:
            subj = r["subject"].replace("|", "\\|")
            lines.append(f"| {r['iso'][11:16]} | {subj} | {len(r['files'])} | "
                         f"+{r['adds']}/-{r['dels']} |")
        lines.append("")

    # 时间线
    lines += ["## 全天节奏", "", "| 时段 | 项目 | 做了什么 |", "|------|------|----------|"]
    for h, rs in hour_slots(rows):
        by_repo = defaultdict(list)
        for r in rs:
            by_repo[r["label"]].append(r)
        parts = []
        for label, xrs in by_repo.items():
            heads = "；".join(trunc(x["text"], 30) for x in xrs[:2])
            more = f" 等 {len(xrs)} 条" if len(xrs) > 2 else ""
            parts.append(f"**{label}**：{heads}{more}")
        lines.append(f"| {h:02d}:00 | {len(by_repo)} 个 | {'<br>'.join(parts)} |")
    lines.append("")

    # 统计
    lines += ["## 统计", "", "| 项目 | 类别 | 提交 | 文件 | + | - | 首次 | 末次 |",
              "|------|------|------|------|------|------|------|------|"]
    for label, rs in ordered:
        lines.append(f"| {label} | {rs[0]['group']} | {len(rs)} | "
                     f"{uniq_files(rs)} | "
                     f"{sum(x['adds'] for x in rs)} | {sum(x['dels'] for x in rs)} | "
                     f"{rs[0]['iso'][11:16]} | {rs[-1]['iso'][11:16]} |")
    lines.append("")

    per_type = Counter(r["type"] for r in rows)
    if per_type:
        lines += ["**提交类型**：" + "、".join(
            f"{TYPE_CN.get(t, t)} {c}" for t, c in per_type.most_common()), ""]

    scopes = Counter(r["scope"] for r in rows if r["scope"])
    if scopes:
        lines += ["**高频范围**：" + "、".join(f"`{s}` ×{c}" for s, c in scopes.most_common(6)), ""]

    cloned = [r for r in rows if r.get("clones")]
    if cloned:
        dup = "；".join(f"`{r['sha'][:7]}` 另见于 {'、'.join(r['clones'])}" for r in cloned[:3])
        lines += [f"> 已按提交 sha 去重：{dup}{' 等' if len(cloned) > 3 else ''}"
                  "（同一 remote 的多份本地 clone 不重复计数）。", ""]

    # 单次几万行的变动一般是删除/迁移/归档，混在统计里会让人误判当天产出
    big = [r for r in rows if r["adds"] + r["dels"] > 5000]
    if big:
        sample = "；".join(f"`{b['sha'][:7]}` +{b['adds']}/-{b['dels']}" for b in big[:2])
        lines += [f"> 注意：{len(big)} 次提交单次变更超过 5000 行（{sample}），"
                  "通常是删除、迁移或归档，不宜当作当天的代码产出量。", ""]

    lines += ["---", "",
              f"> 生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}，"
              "数据源为 git log（按作者日期统计，已排除合并提交与空提交）。", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------ 落盘

def write(day, content, cfg, dry_run=False):
    o = cfg["output"]
    rel = o["filename"].format(date=day.isoformat())
    repo = (o.get("repo") or "").strip()
    target = Path(repo) / o["subdir"] / rel if repo else BASE_DIR / o["dir"] / rel

    if dry_run:
        print(f"\n>>> 目标：{target}\n")
        print(content)
        return "dry-run"

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.read_text(encoding="utf-8") == content:
        print(f"  = {target.name} 内容未变，跳过")
        return "unchanged"
    target.write_text(content, encoding="utf-8")
    print(f"  ✓ 已写入 {target}")

    if repo and o.get("commit"):
        dr.run_git(repo, "add", str(target.relative_to(Path(repo))))
        ok, err = dr.run_git_ok(repo, "commit", "-m",
                                o["commit_message"].format(date=day.isoformat()))
        if not ok:
            print(f"  ! 提交失败：{err[:120]}")
            return "written"
        print("  ✓ 已提交")
        if o.get("push"):
            ok, err = dr.run_git_ok(repo, "push")
            print("  ✓ 已推送" if ok else f"  ! 推送失败：{err[:100]}")
    return "written"


# ------------------------------------------------------------------ CLI

def main():
    ap = argparse.ArgumentParser(description="每日提交总览")
    ap.add_argument("--date", help="指定日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--backfill", type=int, default=0, help="补齐过去 N 天（含今天）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    ap.add_argument("--include-empty", action="store_true",
                    help="没有提交的日子也写一份空文档（默认跳过，避免一堆无意义文件）")
    ap.add_argument("--repos", action="store_true", help="列出扫描到的仓库和归类")
    ap.add_argument("--init", action="store_true", help="写出默认配置文件")
    # 定时任务用，避免 bat 停在 pause 上一直挂着
    ap.add_argument("--no-pause", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    cfg = load_config()

    if args.init:
        save_config(cfg)
        print(f"已写出 {CONFIG_PATH}")
        return

    if args.repos:
        repos = all_repos(cfg)
        roots = cfg["git"]["search_roots"]
        extra = editor_roots() if cfg["git"].get("auto_roots", True) else []
        print(f"扫描根：{roots}")
        if extra:
            print(f"编辑器最近项目：{len(extra)} 个（浅扫一层）")
        print(f"共 {len(repos)} 个仓库\n")
        for p in sorted(repos):
            g = classify(p, cfg)
            meta = group_meta(g, cfg)
            print(f"  {meta.get('emoji', '')} [{g}] {p}")
        print("\n归类不对就改 digest_config.json 的 classify.overrides（按完整路径）")
        return

    if args.backfill:
        days = [date.today() - timedelta(days=i) for i in range(args.backfill - 1, -1, -1)]
    elif args.date:
        days = [date.fromisoformat(args.date)]
    else:
        days = [date.today()]

    # 补多天时只扫一次库，否则每天重扫一遍所有仓库（19 个约 8 秒）
    cache = None
    if len(days) > 1:
        repos = all_repos(cfg)
        print(f"扫描 {len(repos)} 个仓库（{days[0].isoformat()} 起）...")
        cache = (repos, scan_repos(repos, days[0]))

    results = {}
    for d in days:
        rows, _ = collect(d, cfg, cache)
        if not rows and not args.include_empty:
            results[d] = (0, "skipped（无提交）")
            continue
        status = write(d, render(d, rows, cfg), cfg, dry_run=args.dry_run)
        results[d] = (len(rows), status)

    print("\n" + "-" * 50)
    for d, (n, s) in results.items():
        print(f"{d.isoformat()}  {n:>3} 次提交  {s}")
    empty = sum(1 for n, _ in results.values() if n == 0)
    if empty:
        print(f"（其中 {empty} 天没有提交）")


if __name__ == "__main__":
    main()
