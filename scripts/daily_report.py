#!/usr/bin/env python3
"""daily_report.py — 自动生成工作日志并提交到 labbol-worklog 仓库

背景：原本靠手动让 AI 生成日志再提交，用久了必然断档（仓库最后一条是 07/17）。
这个脚本做三件事：定时生成、幂等提交、补齐空档。

两档深度，因为 git 能还原"做了什么"，还原不了"为什么这么做、卡在哪"：

  lite（默认，不需要任何 API key）
      只写 git 和活动数据能确证的部分：任务概览、时间轴、产出汇总、明日计划。
      零模型、零成本、永不编造、每天都能跑。日报的价值首先在"连续"，不在"深刻"。

  full（需要模型 + 一天完整的 diff）
      额外补「需求详情」，由 diff 反推实现方案。
      仍然不写「遇到的问题」和「技术要点备忘」——那两节必须来自当时的对话，
      靠 diff 反推只会编得像模像样，反而有害。

用法：
  python daily_report.py --init
  python daily_report.py --dry-run                     # 预览今天，不写不提交
  python daily_report.py                               # 生成今天并提交推送
  python daily_report.py --date 2026-07-18 --depth full
  python daily_report.py --backfill 14                 # 补齐过去 14 天（含今天）
  python daily_report.py --backfill 30 --no-push       # 先只看，不推送
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "report_config.json"
PROMPT_PATH = BASE_DIR / "prompts" / "daily_report.md"
DB_PATH = BASE_DIR / "data" / "activity.db"
OUT_DIR = BASE_DIR / "out"
WEEKDAYS = "一二三四五六日"

DEFAULT_CONFIG = {
    "_doc": {
        "git.search_roots": "在哪几个目录下找 git 仓库（--init 扫描）",
        "git.exclude": "排除的仓库目录名",
        "git.author_emails": "只统计这些作者的提交；留空则用 git config user.email",
        "output.repo": "日志仓库本地路径；留空则只写到 companion/out/",
        "output.path_template": "支持 {year} {month} {date} {mon} {sun} {week}",
        "output.update_index": "是否自动维护月度 README.md 索引表",
        "model.base_url": "任何 OpenAI 兼容端点",
        "model.api_key_env": "从环境变量读 key（推荐）",
        "rules.skip_weekends": "周末不生成",
        "rules.min_minutes": "零提交且使用时间低于此值时跳过，不制造垃圾提交",
        "rules.sanitize": "抹掉本地绝对路径与疑似密钥",
        "full.max_diff_chars": "full 模式下每个提交最多带多少字符 diff",
    },
    "git": {
        "search_roots": ["D:\\yzj"],
        "max_depth": 3,
        "exclude": ["labbol工作日志"],
        "author_emails": ["1259700509@qq.com"],
    },
    "output": {
        "repo": "D:\\yzj\\labbol工作日志",
        "path_template": "{year}/{month}/Week_{mon}-{sun}/{date}-工作日志.md",
        "commit_message": "docs: add {date} work log",
        "update_index": True,
        "push": True,
        "remote": "origin",
    },
    "model": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key_env": "COMPANION_API_KEY",
        "temperature": 0.3,
    },
    "rules": {
        "skip_weekends": False,
        "min_minutes": 20,
        "sanitize": True,
    },
    "full": {"max_diff_chars": 6000},
}


# ------------------------------------------------------------------ 基础

def deep_merge(base, override):
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    if CONFIG_PATH.exists():
        return deep_merge(DEFAULT_CONFIG, json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    save_config(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def run_git(repo, *args, timeout=60):
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def run_git_ok(repo, *args, timeout=180):
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode == 0, (r.stderr or r.stdout).strip()
    except Exception as exc:
        return False, str(exc)


def discover_repos(roots, max_depth=3, exclude=()):
    found = []
    skip = {".git", "node_modules", "dist", "build", ".next", "vendor",
            "__pycache__", "target", ".venv", "coverage"}
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        base = len(root.parts)
        for dirpath, dirnames, _ in os.walk(root):
            p = Path(dirpath)
            if len(p.parts) - base >= max_depth:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d not in skip]
            if (p / ".git").exists():
                if p.name not in exclude:
                    found.append(p)
                dirnames[:] = []
    return sorted(set(found))


# ------------------------------------------------------------------ git 采集

def parse_log(text):
    commits = []
    for chunk in text.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        lines = chunk.splitlines()
        head = lines[0].lstrip("@").split("|")
        if len(head) < 4:
            continue
        sha, when, subject = head[0], head[1], "|".join(head[3:])
        adds = dels = 0
        files = []
        for ln in lines[1:]:
            parts = ln.split("\t")
            if len(parts) != 3:
                continue
            a, d, path = parts
            files.append(path)
            adds += int(a) if a.isdigit() else 0
            dels += int(d) if d.isdigit() else 0
        commits.append({
            "sha": sha, "short": sha[:7], "iso": when,
            "time": when[11:16] if len(when) > 16 else "",
            "subject": subject, "files": files,
            "nfiles": len(files), "adds": adds, "dels": dels,
            "diff": "",
        })
    return commits


class GitIndex:
    """一次性把各仓库的提交清单读进内存，按天索引，供补做时反复筛选。

    补做 14 天如果每天重扫一遍 git log --all，会慢到不可用（实测 5 天就超时）。
    """

    def __init__(self, cfg, since_day):
        self.cfg = cfg
        repos = discover_repos(cfg["git"]["search_roots"], cfg["git"]["max_depth"],
                               cfg["git"].get("exclude", []))
        counts = Counter(p.name for p in repos)
        self.by_day = defaultdict(list)
        since = f"{since_day.isoformat()} 00:00:00"
        for repo in repos:
            # 快速跳过：这个仓库最近一次提交都早于起点，没必要扫它的历史
            latest = run_git(repo, "log", "--all", "-1", "--format=%cI")
            if latest and latest[:10] < since_day.isoformat():
                continue
            name = repo.name if counts[repo.name] == 1 else f"{repo.parent.name}/{repo.name}"
            # --since 作用于 committer date，一定不晚于 author date，
            # 所以用它做下界是安全的剪枝（--until 才不安全）
            raw = run_git(repo, "log", "--all", f"--since={since}",
                          "--pretty=format:%H|%aI|%ae|%s", timeout=180)
            for ln in raw.splitlines():
                parts = ln.split("|", 3)
                if len(parts) < 4:
                    continue
                sha, iso, email, subject = parts
                if subject.startswith("Merge"):
                    continue
                self.by_day[iso[:10]].append((iso, sha, email or "", subject, repo, name))

    def day(self, day, authors, want_diff=False, max_diff=6000):
        rows = sorted(
            r for r in self.by_day.get(day.isoformat(), [])
            if not authors or any(a and a.lower() in r[2].lower() for a in authors)
        )
        grouped = {}
        for iso, sha, _, subject, repo, name in rows:
            key = str(repo)
            if key not in grouped:
                grouped[key] = {"name": name, "path": key, "commits": [],
                                "branch": run_git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
                                "dirty": [], "unpushed": []}
            numstat = run_git(repo, "show", "--numstat", "--format=", sha)
            files, adds, dels = [], 0, 0
            for ln in numstat.splitlines():
                p = ln.split("\t")
                if len(p) != 3:
                    continue
                a, dd, path = p
                files.append(path)
                adds += int(a) if a.isdigit() else 0
                dels += int(dd) if dd.isdigit() else 0
            if not files:
                continue
            diff = run_git(repo, "show", "--format=", "--unified=2", sha)[:max_diff] \
                if want_diff else ""
            grouped[key]["commits"].append({
                "sha": sha, "short": sha[:7], "iso": iso, "time": iso[11:16],
                "subject": subject, "files": files, "nfiles": len(files),
                "adds": adds, "dels": dels, "diff": diff,
            })

        for key, g in grouped.items():
            repo = Path(key)
            g["dirty"] = [x[3:].strip() for x in
                          run_git(repo, "status", "--porcelain").splitlines() if x.strip()]
            g["unpushed"] = [x for x in
                             run_git(repo, "log", "@{u}..HEAD", "--oneline").splitlines() if x]
        return list(grouped.values())


# ------------------------------------------------------------------ 活动采集

def activity_day(day):
    empty = {"total_min": 0, "apps": [], "titles": []}
    if not DB_PATH.exists():
        return empty
    start = int(datetime(day.year, day.month, day.day).timestamp())
    end = start + 86400
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT start_ts,end_ts,exe,title FROM segments WHERE end_ts>=? AND start_ts<?",
        (start, end)).fetchall()
    conn.close()
    if not rows:
        return empty
    per_app, titles = {}, {}
    for s, e, exe, title in rows:
        d = min(e, end) - max(s, start)
        per_app[exe] = per_app.get(exe, 0) + d
        titles[title] = titles.get(title, 0) + d
    return {
        "total_min": sum(per_app.values()) // 60,
        "apps": sorted(per_app.items(), key=lambda kv: -kv[1])[:6],
        "titles": sorted(titles.items(), key=lambda kv: -kv[1])[:30],
    }


# ------------------------------------------------------------------ 生成 lite

def fmt_min(m):
    m = int(m)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def collect(day, cfg, index, want_diff=False):
    return index.day(day, cfg["git"].get("author_emails", []),
                     want_diff=want_diff,
                     max_diff=cfg["full"]["max_diff_chars"]), activity_day(day)


def timeline_rows(repos):
    """一行一个提交，按时间排序。

    不把多个提交合并成"时间段"——机器并不知道每件事花了多久，
    合并出来的区间会让人误以为是工作时长。
    """
    events = []
    for r in repos:
        for c in r["commits"]:
            events.append((c["iso"], c["time"], r["name"], c))
    events.sort()
    rows = []
    for _, t, repo_name, c in events:
        files = "、".join(Path(f).name for f in c["files"][:3])
        more = f" 等 {c['nfiles']} 个文件" if c["nfiles"] > 3 else ""
        rows.append((t, f"[{repo_name}] {c['subject']}",
                     f"commit `{c['short']}`（+{c['adds']}/-{c['dels']}，{files}{more}）"))
    return rows


def render_lite(day, repos, act):
    tasks = []
    for r in repos:
        for c in r["commits"]:
            tasks.append((c["subject"], "✅ 完成", "—", r["name"]))

    lines = [f"# 工作日志 - {day.isoformat()} 周{WEEKDAYS[day.weekday()]}", ""]

    lines += ["## 📋 今日任务概览", "", "| 任务 | 状态 | 优先级 | 所属项目/模块 |",
              "|------|------|--------|--------------|"]
    if tasks:
        for t in tasks:
            lines.append("| {} | {} | {} | {} |".format(*t))
    else:
        lines.append("| 无代码提交 | — | — | — |")

    lines += ["", "## ⏱ 时间轴", "", "| 时间段 | 工作内容 | 备注 |",
              "|--------|---------|------|"]
    rows = timeline_rows(repos)
    if rows:
        for r in rows:
            lines.append(f"| {r[0]} | {r[1]} | {r[2]} |")
    else:
        lines.append("| — | 无提交记录 | — |")

    lines += ["", "## 📦 产出汇总", "", "| 仓库 | 分支 | 提交 | 文件 | 变更 |",
              "|------|------|------|------|------|"]
    tot_c = tot_f = tot_a = tot_d = 0
    for r in repos:
        c_n = len(r["commits"])
        # 用并集，避免同名文件在多个提交里被重复计数
        f_n = len({f for c in r["commits"] for f in c["files"]})
        a_n = sum(c["adds"] for c in r["commits"])
        d_n = sum(c["dels"] for c in r["commits"])
        tot_c, tot_f, tot_a, tot_d = tot_c + c_n, tot_f + f_n, tot_a + a_n, tot_d + d_n
        if c_n:
            lines.append(f"| {r['name']} | {r['branch'] or '?'} | {c_n} | {f_n} | +{a_n}/-{d_n} |")
    if not tot_c:
        lines.append("| — | — | 0 | 0 | — |")
    else:
        lines.append(f"| **合计** | — | **{tot_c}** | **{tot_f}** | **+{tot_a}/-{tot_d}** |")

    if act["total_min"] >= 10:
        top = "、".join(f"{exe.replace('.exe', '')} {fmt_min(m // 60)}"
                       for exe, m in act["apps"][:4] if m // 60 > 0)
        lines += ["", f"> 前台记录 {fmt_min(act['total_min'])}：{top}"]

    lines += ["", "## ⏭️ 明日计划", ""]
    plan = []
    for r in repos:
        if len(plan) >= 4:
            break
        if r["dirty"]:
            names = "、".join(Path(f).name for f in r["dirty"][:3])
            plan.append(f"- [ ] 继续 {r['name']} 的未提交改动"
                        f"（{len(r['dirty'])} 个文件：{names}）")
        if r["unpushed"] and len(plan) < 4:
            plan.append(f"- [ ] 推送 {r['name']} 的 {len(r['unpushed'])} 个待推提交")
    if not plan:
        plan.append("- [ ] （无未完成事项，按当日进度继续）")
    lines += plan

    lines += ["", "---", "",
              "> 本日志由 companion 自动生成：仅含 git 提交与活动记录能确证的内容，"
              "不含需求背景与问题排查细节。需要完整版请用 `--depth full`，"
              "或在 Trae 里基于当日 diff 补写。", ""]
    return "\n".join(lines), tasks


# ------------------------------------------------------------------ 生成 full

FALLBACK_HINT = ("（模型不可用，已回退到自动摘要。需求详情需在 Trae 里补写。）")


def call_llm(prompt, cfg):
    m = cfg["model"]
    key = os.environ.get(m.get("api_key_env", ""), "").strip() or m.get("api_key", "").strip()
    if not key:
        raise RuntimeError(f"缺少 API key（环境变量 {m.get('api_key_env')}），改用 --depth lite")
    payload = json.dumps({
        "model": m["model"], "temperature": m.get("temperature", 0.3),
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        m["base_url"].rstrip("/") + "/chat/completions", data=payload,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"].strip()


def build_full_prompt(day, repos, base_md):
    parts = [f"日期：{day.isoformat()} 周{WEEKDAYS[day.weekday()]}", "",
             "## 当天的 git 提交与 diff"]
    for r in repos:
        if not r["commits"]:
            continue
        parts.append(f"\n### 仓库 {r['name']}（分支 {r['branch']}）")
        for c in r["commits"]:
            parts.append(f"\n#### {c['short']} {c['subject']}（{c['time']}，"
                         f"{c['nfiles']} 文件 +{c['adds']}/-{c['dels']}）")
            parts.append("改动文件：" + "、".join(c["files"][:15]))
            if c["diff"]:
                parts.append("```diff\n" + c["diff"] + "\n```")
    return PROMPT_PATH.read_text(encoding="utf-8").replace(
        "{date}", day.isoformat()
    ).replace("{base}", base_md).replace("{context}", "\n".join(parts))


SECRETS = [
    (re.compile(r"[A-Za-z]:\\[^\s，。）)\"']+"), "<本地路径>"),
    (re.compile(r"\b(?:gh[pousr]_|sk-|AKIA)[A-Za-z0-9_\-]{12,}"), "<已脱敏>"),
]


def sanitize(text):
    for pat, rep in SECRETS:
        text = pat.sub(rep, text)
    return text


# ------------------------------------------------------------------ 索引

def rel_path(day, template):
    monday = day - timedelta(days=day.weekday())
    sunday = monday + timedelta(days=6)
    return template.format(
        year=f"{day:%Y}", month=f"{day:%m}", date=day.isoformat(),
        mon=f"{monday:%m%d}", sun=f"{sunday:%m%d}", week=day.isocalendar()[1])


def update_index(repo, day, summary, rel, dry_run=False):
    """把当日日志追加进月度 README.md 的两张表。失败只提示，不中断主流程。"""
    year_month = f"{day:%Y/%m}"
    readme = Path(repo) / year_month / "README.md"
    week_dir = Path(rel).parent.name
    monday = day - timedelta(days=day.weekday())
    sunday = monday + timedelta(days=6)
    week_no = day.isocalendar()[1]
    row_log = (f"| {day:%m/%d} | [工作日志](./{week_dir}/{day.isoformat()}-工作日志.md) "
               f"| {summary} |")
    row_week = (f"| W{week_no} | {monday:%m/%d} - {sunday:%m/%d} "
                f"| [Week_{monday:%m%d}-{sunday:%m%d}](./{week_dir}/) |")

    try:
        if readme.exists():
            lines = readme.read_text(encoding="utf-8").splitlines()
        else:
            lines = [f"# {day:%Y}年{day:%m}月 工作日志索引", "", "## 📁 周目录", "",
                     "| 周次 | 日期范围 | 目录 |", "|------|---------|------|",
                     "", "## 📄 日志列表", "",
                     "| 日期 | 文件 | 主要工作 |", "|------|------|---------|"]

        if any(day.strftime("%m/%d") in ln and "工作日志" in ln for ln in lines):
            print("  = 索引里已有该日，跳过")
            return

        def table_end(start_idx):
            i = start_idx
            while i < len(lines) and not lines[i].startswith("|"):
                i += 1
            if i >= len(lines):
                return None
            while i < len(lines) and lines[i].startswith("|"):
                i += 1
            return i

        week_at = next((i for i, ln in enumerate(lines) if ln.startswith("## 📁")), None)
        log_at = next((i for i, ln in enumerate(lines) if ln.startswith("## 📄")), None)

        if log_at is not None:
            end = table_end(log_at)
            if end:
                lines.insert(end, row_log)
        # 只在「周目录」这一段里判断，否则刚插入的日志行链接本身也含 Week_xxx，会误判
        if week_at is not None:
            week_region = "\n".join(lines[week_at:log_at if log_at else len(lines)])
            if week_dir not in week_region:
                end = table_end(week_at)
                if end:
                    lines.insert(end, row_week)

        if dry_run:
            print(f"  [dry-run] 将更新索引 {readme}")
            return Path(readme).relative_to(repo).as_posix()
        readme.parent.mkdir(parents=True, exist_ok=True)
        readme.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"  ✓ 已更新索引 {readme.name}")
        return Path(readme).relative_to(repo).as_posix()
    except Exception as exc:
        print(f"  ! 索引更新失败（不影响日志本身）：{exc}")
        return None


# ------------------------------------------------------------------ 落库

def write_and_commit(content, day, cfg, dry_run=False, push=True, index_fn=None):
    o = cfg["output"]
    repo = (o.get("repo") or "").strip()
    rel = Path(rel_path(day, o["path_template"])).as_posix()

    if not repo:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        target = OUT_DIR / Path(rel).name
        target.write_text(content, encoding="utf-8")
        print(f"  → 已写到 {target}（未配置 output.repo）")
        return "written"

    repo_p = Path(repo)
    if not repo_p.is_dir():
        print(f"  ! output.repo 不存在：{repo_p}")
        return "error"

    target = repo_p / rel
    if target.exists() and target.read_text(encoding="utf-8") == content:
        print(f"  = {rel} 内容未变，跳过")
        return "unchanged"

    if dry_run:
        print(f"  [dry-run] 目标：{rel}\n")
        print("\n".join("    " + ln for ln in content.splitlines()[:45]))
        if index_fn:
            index_fn(dry_run=True)
        return "dry-run"

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    extra = index_fn(dry_run=False) if index_fn else None

    staged = [rel] + ([extra] if extra else [])
    for p in staged:
        run_git(repo_p, "add", p)
    if not run_git(repo_p, "diff", "--cached", "--name-only"):
        print("  = 暂存区无变化，跳过提交")
        return "unchanged"

    msg = o["commit_message"].format(date=day.isoformat())
    ok, err = run_git_ok(repo_p, "commit", "-m", msg)
    if not ok:
        print(f"  ! 提交失败：{err[:120]}")
        return "error"
    print(f"  ✓ 已提交：{msg}")

    if push and o.get("push", True):
        ok, err = run_git_ok(repo_p, "push", o.get("remote", "origin"))
        print("  ✓ 已推送" if ok else f"  ! 推送失败（{err[:100]}），稍后手动 push")
    return "committed"


# ------------------------------------------------------------------ 单日

def do_day(day, cfg, index=None, depth="lite", dry_run=False, push=True):
    print(f"\n[{day.isoformat()} 周{WEEKDAYS[day.weekday()]}]")
    if cfg["rules"].get("skip_weekends") and day.weekday() >= 5:
        print("  周末，跳过")
        return "skipped"

    repos, act = collect(day, cfg, index, want_diff=(depth == "full"))
    n_commits = sum(len(r["commits"]) for r in repos)
    if n_commits == 0 and act["total_min"] < cfg["rules"].get("min_minutes", 20):
        print(f"  零提交且使用 < {cfg['rules'].get('min_minutes')} 分钟，跳过（不制造垃圾提交）")
        return "skipped"

    base_md, tasks = render_lite(day, repos, act)

    if depth == "full" and n_commits:
        try:
            content = call_llm(build_full_prompt(day, repos, base_md), cfg)
        except Exception as exc:
            print(f"  ! {exc}\n  → 回退 lite")
            content = base_md + "\n" + FALLBACK_HINT + "\n"
    else:
        content = base_md

    if cfg["rules"].get("sanitize", True):
        content = sanitize(content)
    content = content.rstrip() + "\n"

    index_fn = None
    if cfg["output"].get("update_index") and (cfg["output"].get("repo") or "").strip():
        summary = "、".join(t[0] for t in tasks[:3]) or "无代码提交"
        if len(summary) > 90:
            summary = summary[:88] + "…"
        rp = rel_path(day, cfg["output"]["path_template"])
        index_fn = lambda dry_run=False, _s=summary, _r=rp, _d=day: update_index(
            cfg["output"]["repo"], _d, _s, _r, dry_run=dry_run)

    return write_and_commit(content, day, cfg, dry_run=dry_run, push=push,
                            index_fn=index_fn)


def cmd_init(cfg):
    print(f"扫描 {cfg['git']['search_roots']} ...")
    repos = discover_repos(cfg["git"]["search_roots"], cfg["git"]["max_depth"],
                           cfg["git"].get("exclude", []))
    print(f"发现 {len(repos)} 个仓库：")
    for r in repos:
        print(f"  - {r}")
    email = run_git(Path.cwd(), "config", "user.email")
    if email and email not in cfg["git"]["author_emails"]:
        cfg["git"]["author_emails"].append(email)
    save_config(cfg)
    print(f"\n配置：{CONFIG_PATH}")
    print(f"作者：{cfg['git']['author_emails']}")
    print(f"日志仓库：{cfg['output']['repo'] or '(未配置，只写 out/)'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--date")
    ap.add_argument("--backfill", type=int, default=0, help="补齐过去 N 天（含今天）")
    ap.add_argument("--depth", choices=["lite", "full"], default="lite")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    if args.init:
        cmd_init(cfg)
        return

    if args.backfill:
        days = [date.today() - timedelta(days=i)
                for i in range(args.backfill - 1, -1, -1)]
    elif args.date:
        days = [date.fromisoformat(args.date)]
    else:
        days = [date.today()]

    print(f"读取 git 历史（{days[0].isoformat()} 起）...")
    index = GitIndex(cfg, days[0])

    results = {}
    for i, d in enumerate(days):
        last = i == len(days) - 1
        results[d] = do_day(d, cfg, index=index, depth=args.depth,
                            dry_run=args.dry_run, push=(not args.no_push) and last)

    print("\n" + "-" * 56)
    for d, r in results.items():
        print(f"{d.isoformat()}  {r}")
    skipped = sum(1 for r in results.values() if r == "skipped")
    if skipped:
        print(f"（{skipped} 天无提交或使用过短，按设计跳过）")


if __name__ == "__main__":
    main()
