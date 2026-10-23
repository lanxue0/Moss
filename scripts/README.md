# 自用版实施方案

单人自用，权限全开。所以这个方案的目标只有一个：**用最少的工程，换到"它真的懂我在干嘛"**。

---

## 今天就能跑

### 一、每日提交总览（`digest.py`）— 现在的主线

把你所有 git 仓库当天提交汇总成一份总结，不管是不是工作。

```bash
cd scripts
python digest.py --repos           # 先看扫到哪些仓库、自动归到哪一类
python digest.py --dry-run         # 预览今天的总结，不写文件
python digest.py                   # 写文件到 out/digest/
python digest.py --backfill 30     # 补齐过去 30 天（只写有提交的日子）
python digest.py --date 2026-10-19
```

产出长这样：`out/digest/2026-10-19.md`，含「今天做了什么 / 概览 / 分项目 / 全天节奏 / 统计」。
详细说明见文末「十一、每日提交总览」。

### 二、桌面活动记录（`observer.py` + `report.py`）

```bash
python observer.py                 # 开始记录（3 秒一采样）
python report.py                   # 当天结构 + 生成分析提示词
python report.py --date 2026-10-22
```

或者双击 `start.bat` 常驻、`report.bat` 出日报。

数据全在 `data/activity.db`（SQLite，单文件，随便查、随便删）。
暂停记录：在 `data/` 下新建一个叫 `PAUSE` 的空文件；删掉即恢复。

已经实现的能力：前台窗口标题、进程名、空闲判定、片段合并、浏览器 URL（走 Chrome/Edge 的 CDP 9222 端口，没有就自动跳过）。
浏览器 URL 想生效，需要这样启动浏览器：

```bash
chrome.exe --remote-debugging-port=9222
```

> 这一条值很多：URL 一到位，你 70% 的时间就有了明确语义，比窗口标题强十倍。

---

## 一、三条工程判断

先定这三条，能省三个月。

**① 不做实时流式分析，做批处理。**
- 3 秒一次：采样，纯 CPU，写 SQLite
- 10 分钟一次：把这一批片段归类到项目（规则优先，剩下的一次模型调用）
- 每晚一次：日报 + 写记忆
- 你点小人时：才走大模型对话

实时流式看起来酷，但会带来耗电、成本、状态机复杂度，而且**你根本不需要秒级反应**。批处理的另一个好处是容错：这批归错了，下一批纠正，不用回滚。

**② 单人项目的第一性原理是"半年后还能一键启动"。**
所以：标准库 + SQLite + 一个 .bat。不要一上来就 Docker、向量库、本地大模型。那些等你确认"日报我每天都想看"之后再上。

**③ 先做看不懂的东西，后做可爱的东西。**
小人是最后做的。原因很简单：动画做两周很爽，但两周后你会发现核心功能一行没写。

---

## 二、采集清单（按性价比排序）

| 数据源 | 拿到什么 | 难度 | 价值 | 现状 |
|---|---|---|---|---|
| 前台窗口 + 进程名 | 在用哪个软件、在哪个文档 | 极低 | 高 | ✅ 已实现 |
| 浏览器 URL + 标题 | **在具体哪个页面** | 低 | 极高 | ✅ 已实现（CDP） |
| 界面无障碍树正文 | 页面/文档里在讲什么 | 中 | 高 | 待做（`uiautomation`） |
| 截图 + OCR | 兜底一切非浏览器应用 | 中 | 高 | 待做 |
| 文件路径 / git 仓库 | 在改哪个文件、哪个分支 | 低 | 中高 | 可从标题拿 |
| 输入节奏 / 空闲 | 专注度、离席 | 极低 | 中 | ✅ 已实现 |
| 手机（adb） | 手机上前台 App | 低 | 中 | 待做 |
| 剪贴板 | 复制了什么 | 低 | 低 | 噪音大，不急 |
| 键盘内容 | —— | —— | —— | **不做**。噪音大、价值低、还会让你自己不舒服 |

**采集的目标不是"多"，是"能回答这个问题：我这 20 分钟在为什么目标做事"。** 窗口 + URL 就够回答 80%。

---

## 三、数据模型

四张表。核心原则只有一条：**每一条结论都要能指回原始记录**，否则你没法推翻它。

```sql
-- 1. 原始片段（已有）
segments(id, start_ts, end_ts, exe, title, url)

-- 2. 十分钟聚合块（归类结果）
CREATE TABLE blocks (
    id          INTEGER PRIMARY KEY,
    start_ts    INTEGER, end_ts INTEGER,
    goal_id     INTEGER,          -- 归到哪个目标，NULL = 未归类
    confidence  REAL,             -- 0..1，低于 0.6 一律当未归类
    summary     TEXT,             -- 一句话："在读 RAG 的 chunking 部分"
    evidence    TEXT,             -- JSON：参与判断的 segment id 列表
    method      TEXT              -- rule / embedding / llm / manual
);

-- 3. 语义记忆（可删，可溯源）
CREATE TABLE memories (
    id          INTEGER PRIMARY KEY,
    kind        TEXT,             -- fact / preference / knowledge / progress
    content     TEXT,             -- "他对 embedding 维度这块反复卡住"
    source_ids  TEXT,             -- 支撑它的 block id
    confidence  REAL,
    created_ts  INTEGER,
    expires_ts  INTEGER           -- NULL = 不过期
);

-- 4. 目标（对应 goals.md 的结构化版本）
CREATE TABLE goals (
    id INTEGER PRIMARY KEY, name TEXT, why TEXT,
    hours_per_week REAL, deadline TEXT, status TEXT
);
```

`confidence < 0.6 → goal_id = NULL`，这条铁律别破。宁可显示"这 1 小时我不知道你在干嘛"，也不要猜错。

---

## 四、每日管线

| 时机 | 做什么 | 实现 |
|---|---|---|
| 每 3 秒 | 采样、切片段 | `observer.py` 常驻 |
| 每 10 分钟 | 归类最近片段 → 写 blocks | 待做（cron / 定时线程） |
| 22:30 | 汇总 → 日报 → 写 memories | `report.py` + 一次模型调用 |
| 点小人时 | 组装上下文对话 | 待做 |

放进 Windows 任务计划程序：

```
schtasks /create /tn "companion-report" /tr "D:\yzj\moss\scripts\report.bat" /sc daily /st 22:30
```

---

## 五、三个功能，按实现成本排序（重要）

**这个排序能让你两周内就看到价值，而不是两个月。**

### ① 日报 —— 成本最低，已可用
`report.py` 生成 `YYYY-MM-DD.prompt.md`，粘给任何模型（包括我），它按固定格式出日报。先用这个跑一周，看你会不会主动打开它。

### ② 卡住检测 —— **纯 SQL，不需要任何模型**

这是我最想让你先做的一个。它成本极低，但情感价值最高。四条查询就够：

```sql
-- A. 同一天反复回到同一个页面（≥3 次）= 在找什么没找到
SELECT url, COUNT(*) n, SUM(end_ts-start_ts) total
FROM segments
WHERE url <> '' AND start_ts >= :today
GROUP BY url HAVING n >= 3
ORDER BY n DESC LIMIT 10;

-- B. 同一个文档/文件反复打开（回来 ≥4 次）= 读不下去或反复核对
SELECT title, COUNT(*) n FROM segments
WHERE start_ts >= :today
GROUP BY title HAVING n >= 4 ORDER BY n DESC;

-- C. 超长停留（单段 ≥ 25 分钟）在同一页 = 可能在硬啃
SELECT title, url, end_ts-start_ts d FROM segments
WHERE start_ts >= :today AND end_ts-start_ts >= 1500
ORDER BY d DESC;

-- D. 碎片化：一小时内进程切换 > 40 次 = 被打断了，或在自己瞎切
SELECT COUNT(*) switches FROM segments
WHERE start_ts BETWEEN :hour_start AND :hour_start+3600;
```

跑一遍，把命中的东西做成"小人歪头"的触发条件。**不需要 LLM，不需要向量库，今晚就能有。**

### ③ 间隔召回 —— 成本最高，最后做
要先把"知识原子"抽出来（从你读的正文里提概念），再做 FSRS 调度。等 ①② 用顺了再说。

---

## 六、小人怎么最省力地做出来

**技术选型**：Electron 一个透明无边框置顶窗口，`transparent: true, frame: false, alwaysOnTop: true, skipTaskbar: true`。里面就是 HTML + CSS 动画。不用 Live2D，不用 3D。

**关键架构：进程解耦，用 `state.json` 通信。**

```
observer.py  ──写──▶  data/state.json  ◀──轮询(500ms)──  小人窗口
```

主程序只写一个文件：

```json
{ "state": "curious", "reason": "同一页第 3 次", "since": 1761200000 }
```

小人每 500ms 读一次，状态变了就切动画。好处：**你可以随时重做小人、随时换皮、随时关掉它，主程序完全不受影响。**

五个状态先够用，每个状态一张 GIF 就行：

| state | 表现 |
|---|---|
| `sleep` | 趴着，屏幕变暗 |
| `idle` | 坐着，偶尔眨眼 |
| `alongside` | **默认**。和你一起面朝屏幕，不看你 |
| `curious` | 放下手里的东西、歪头（**不开口**） |
| `speaking` | 展开对话气泡 |

注意 `alongside` 是默认态，不是 `curious`。一个大部分时间都在"想跟你说话"的小人，三天就会被关掉。

---

## 七、四周计划

| 周 | 做什么 | 验收 |
|---|---|---|
| W1 | 采集打满（URL + 无障碍树正文）、日报连跑 7 天 | **你自己每天会主动打开日报** |
| W2 | blocks 归类 + goals 认领 + 卡住检测 SQL | 日报能说出"你在为哪个目标做事" |
| W3 | 小人（state.json 解耦，5 个状态） | 它歪头的时候你会去看它 |
| W4 | 点小人对话（带当天上下文）+ 手机 adb | 能问"我今天干嘛了"并得到有证据的回答 |

**W1 结束时如果日报你都不想看，就别往下做了**——后面三周都是在放大一个你自己都不用的东西。

---

## 八、三个陷阱

1. **一开始就追求完美归类。** 永远做不完。先允许大量"未归类"，让模型在日报里问你"这段算哪个目标"，你答一次，它记一条规则。
2. **一开始就上本地大模型 / 向量库。** 环境、显存、量化、加载时间，能拖死一个单人项目。先用 SQLite + 云端 API 跑通，再考虑本地化。
3. **小人做得太早、太精致。** 见上。

---

## 九、下一步我可以帮你做

- 把 `blocks` 归类那一层写出来（规则 + 一次模型调用，含低置信度降级）
- 把卡住检测的四条 SQL 接成"curious 触发器"，直接驱动 `state.json`
- 写小人那个 Electron 窗口（含五个状态的 CSS 动画骨架）
- 写"点小人对话"的上下文组装（今天 blocks + goals + memories → 提示词）

---

## 十、自动工作日志（第二个痛点）

**背景**：`labbol-worklog` 仓库最后一条是 07-17，之后有 **50 个有提交的工作日**没写。断的原因不是不想写，是"每次都要手动让 AI 生成、再手动提交"，中间被打断一次就断了。

`daily_report.py` 把这条链做自动：生成 → 写入 `2026/MM/Week_MMDD-MMDD/` → 维护月度 README 索引 → 提交 → 推送。

### 为什么素材用 git，而不是去解析 Trae 的会话

| | git | Trae 会话 |
|---|---|---|
| 可靠性 | 权威、结构化 | 存在服务端/状态库，解析脆弱 |
| 可回溯 | 几个月前也能补 | 过期即无 |
| 覆盖 | 你实际改了什么 | 你的意图和卡点 |

取舍：git 能还原"做了什么"，还原不了"为什么这么做、卡在哪"。而你原来那份日志里最值钱的后 5 节（需求详情 / 遇到的问题 / 技术要点备忘）恰好属于后者——那些来自对话上下文，不在 diff 里。

所以做成两档，而不是假装一档能全包：

| 深度 | 内容 | 需要 API key | 说明 |
|---|---|---|---|
| `lite`（默认） | 今日任务概览、时间轴、产出汇总、明日计划 | 否 | 零模型、零成本、永不编造 |
| `full` | 额外补「需求详情」（由 diff 反推实现方案） | 是 | **仍不写「遇到的问题」「技术要点」**，靠 diff 反推只会编得像模像样 |

**建议用法：平时 lite 保连续性，想深挖的那天用 full。** 日报的价值首先在"连续"，其次才在"深刻"。

### 命令

```bash
python daily_report.py --init                    # 扫描仓库、写配置
python daily_report.py --dry-run                 # 预览今天，不写不提交
python daily_report.py                           # 生成今天 → 提交 → 推送
python daily_report.py --depth full              # 带需求详情（需 COMPANION_API_KEY）
python daily_report.py --backfill 20 --dry-run   # 先看再决定
python daily_report.py --backfill 70             # 一次补齐 7/20 以来的 50 个工作日
```

API key 走环境变量，不要写进配置文件：

```bash
setx COMPANION_API_KEY "sk-xxxx"
```

### 定时

```
schtasks /create /tn "companion-daily" /tr "D:\yzj\moss\scripts\daily.bat" /sc daily /st 22:30
```

### 三条已知边界（别踩）

1. **被 squash 掉的历史补不回来。** 实测 07-07 那天，原日志里有 8 个提交，可达历史里只剩 3 个——其余 7 个在 07-17 合并 `feature/i18n` 时被 squash，对象还在库里（`git cat-file` 查得到）但已不在任何 ref 上，`git log --all` 找不到。这类日期补出来的日志会偏薄。
2. **不能用 `git log --since/--until`。** 它们按 committer date 过滤，一旦 rebase 过，那天的提交 committer date 就变成合并日，整天被漏掉。已改成一次读入 + 按 author date 在内存里筛。
3. **未提交改动只对当天有提交的仓库有参考价值。** 否则会把十几个仓库压了半年的 dirty 文件全列进"明日计划"，纯噪音。已收紧。

### 它还有个小福利

月度 `README.md` 索引以前也是手写的。现在每次生成日志会顺手把「日志列表」和「周目录」两张表补齐，同一次提交里完成。

---

## 十一、每日提交总览（`digest.py`）

### 它回答的问题

不是"工作日志"，是**"今天总共干了什么"**——包括那些不为了交付、只是想试试的提交。

只读 git，不读窗口、不读截图、不读对话。理由很实际：git 是唯一
**不会过期、不会记错、也不需要任何权限**的事实来源。你今天半夜关掉电脑，
明天再来看，数据一个字节都不会少；反过来，靠"回忆今天做了什么"的东西，
隔一天就失真。

### 命令

```bash
python digest.py --repos            # 列出所有仓库 + 自动归类，先确认这份清单对不对
python digest.py --dry-run          # 只打印不写文件
python digest.py                    # 写今天的
python digest.py --date 2026-10-19  # 指定某天
python digest.py --backfill 30      # 补齐过去 30 天
python digest.py --include-empty    # 没提交的日子也写一份（默认跳过）
python digest.py --init             # 生成 digest_config.json 便于修改
```

输出到 `out/digest/{date}.md`。

### 文档结构

| 段落 | 作用 |
|------|------|
| 今天做了什么 | 按项目把提交归成「推进 / 修复 / 重构 / 收尾」，人话 |
| 概览 | 一句话讲清分布、类型、活跃时段 |
| 分项目 | 每个项目的完整提交表（时间、标题、文件数、增删） |
| 全天节奏 | 按小时还原一天怎么过的 |
| 统计 | 项目 × 类别 × 提交 × 文件 × 增删表 |

### 四个必须这么做的判断

**① 按作者日期统计，不用 `--since/--until`。**
`--since` 过滤的是 committer date。你 rebase 过一次，那天的提交 committer date
就变成合并日，整天凭空消失。实测 07-07 有 8 个提交，用日期过滤只捞到 2 个。
现在是一次 `git log --all` 读进内存，再按 author date 筛。

**② 按提交 sha 去重。**
你机器上有两份 `product-yun-knowledge-view`（remote 完全相同，两个本地 clone）。
不去重的话，同一天 3 个提交会变成 6 个，改动量直接翻倍。sha 全局唯一，按它去重最可靠。

**③ 文件数按「仓库 + 路径」去重。**
不同仓库里的 `src/index.ts` 是两个文件，不是同一个。按路径去重会把 578 算成 400 多。

**④ 不做评价。**
不写"显著提升了效率"、"成功解决了难题"，也不给效率分、不算专注时长。
脚本一开口评价，这份总结就不能用来做决定了——你没法判断是事实还是它替你下的结论。
它只陈述：几个仓库、几次提交、什么时候、改了哪些文件。

### 归类（工作 / 个人）

默认按路径关键词猜：命中 `labbol`/`dcc`/`mentor`/`portal` 等算「工作」，
命中 `mastra`/`xiaole`/`test-` 等算「个人」，其余归「其他」。

猜错了就写 `digest_config.json`，按完整路径硬指定，优先级最高：

```json
{
  "classify": {
    "overrides": {
      "D://yzj//labbol工作日志": "个人",
      "D://yzj//dcctemp": "草稿"
    }
  }
}
```

先跑 `python digest.py --repos` 看它怎么归的，比自己想象一遍快。

### 想归档到仓库

默认只写本地。要提交到某个仓库（比如 `labbol工作日志`），改 `digest_config.json`：

```json
{
  "output": {
    "repo": "D://yzj//labbol工作日志",
    "subdir": "digest",
    "commit": true,
    "push": true
  }
}
```

它会写到 `<repo>/digest/{date}.md`，然后 add / commit / push。
内容没变就跳过，不会天天制造空提交。

### 定时

```
schtasks /create /tn "companion-digest" /tr "D:\yzj\moss\scripts\digest.bat" /sc daily /st 22:30
```

### 已知边界

1. **被 squash 掉的提交补不回来。** 对象还在库里，但不在任何 ref 上，`git log --all` 看不见。这类日子会偏薄，是硬限制，不要试图"估算"。
2. **代码写了但没提交，git 看不见。** 这是设计上的取舍——宁可漏掉"没落地的努力"，也不猜。
3. **补多天时只扫一次库。** 19 个仓库扫一次约 8 秒，补 30 天不会变成 4 分钟。
