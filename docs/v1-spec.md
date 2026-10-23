# Moss v1 实现设计规范

> 目标：把 `design.md` 里那套想法，收敛成一份**能照着写代码**的规范。
> 只覆盖第一期：屏幕角落一个会发光的球 + 它能看到你在做什么 + 一个能设目标和配 API 的面板 + 它凭什么开口。
>
> 前置阅读：`design.md`（为什么这么做）、`build.md`（现在有什么）。
> 本文只管**怎么搭**。凡是"为什么"的问题，能引用的就引用，不重复论证。

---

## 1. 范围

### 1.1 v1 必须能跑通的四件事

| # | 能力 | 验收方式 |
|---|------|----------|
| 1 | **球在桌面上，有动作时发光** | 开机自启，一整天不关；不挡操作、不抢焦点、进出全屏不闪 |
| 2 | **它知道你每天在做什么** | 每天归因准确率 ≥ 80%（你自己抽查 20 段），需要你纠正的 ≤ 5 段 |
| 3 | **有目标，且目标能改变它的回应** | 同一天同样的行为，换个目标，它说的话不一样 |
| 4 | **面板能配 API、设目标、看它看到了什么** | 不用改配置文件、不用看命令行就能完成全部配置 |

### 1.2 v1 明确不做

| 不做 | 原因 |
|------|------|
| 截图 / OCR / 录屏 | 隐私成本远大于收益；窗口标题 + URL 已经能覆盖 80% 的归因 |
| 键盘内容、剪贴板内容 | 做了就是键盘记录器。这条永远不做，不是"v1 不做" |
| 语音、对话式多轮 | v1 只有"它说一句 + 给你 2–3 个选项"，不做聊天框 |
| 手机端 | 见 `design.md` §1：iOS 拿不到内容，做了也是半成品 |
| Live2D / 3D / 数字人 | 球已经足够承载状态；换皮是后期的事，且接口已隔离 |
| 效率分、排行榜、连续打卡 | 见 `design.md` §0：拿焦虑换日活，不碰 |
| 全自动提交代码 | 会覆盖你的工作，风险不对等 |

### 1.3 一条贯穿全文的硬约束

**球不思考，球只渲染。** 所有判断在 Python 侧，球拿到的是一个已经决定好的 `{state, color, text}`。
这样你可以随时关掉球、重写球、换成别的壳，采集和判断一行都不用改。

这不是洁癖，是**可维护性**：这个项目会持续改半年以上，画面和逻辑耦合的代码，改三次就没人敢动了。

---

## 2. 技术选型

### 2.1 结论

| 层 | 选型 | 一句话理由 |
|---|---|---|
| 壳体（窗口宿主） | **Electron 30+** | 透明窗 + 置顶 + 穿透在 Windows 上最稳，热重载最舒服 |
| 渲染 | **React 18 + TypeScript + Vite** | 你已有的技术栈；球就是几个 SVG + CSS 动画，两小时能出效果 |
| 采集与判断（大脑） | **Python 3.13，标准库优先** | 已有的 `observer.py` / `digest.py` 可以直接复用，不重写 |
| 通信 | **127.0.0.1 HTTP + SSE** | 面板要查历史、要写回，纯文件轮询做不到请求/响应 |
| 存储 | **SQLite（单文件）** | 你已经会查了；一天 3 万条事件无压力 |
| 模型调用 | **OpenAI 兼容端点（DeepSeek 默认）** | 只用来把一句事实写成一句人话，见 §9 |

### 2.2 为什么不是 Tauri

评审时一定会问，所以先答：

| | Electron | Tauri v2 |
|---|---|---|
| 内存 | ~120MB | ~25MB |
| 透明窗 / 穿透 | 成熟，坑已踩完 | 可用，但 `set_ignore_cursor_events` + 透明在 WebView2 上偶有闪烁 |
| 上手成本 | 低（你熟 Node） | 中（Rust 工具链、编译等待） |
| 换壳成本 | — | — |

**v1 选 Electron，因为第一期的风险应该在"它该不该说话"，不在"窗口能不能透明"。**

但选型上留了后路：壳体里**不含任何业务逻辑**（只有 `createWindow`、`spawnBrain`、`setIgnoreMouseEvents` 三件事），
所以将来换 Tauri，是替换 `apps/ball` 一个包，`packages/ui` 和 `services/brain` 一行不改。

> 触发换壳的条件写死在这里：**常驻内存 > 250MB 或空闲 CPU > 2% 持续一周**，就换。否则不要动。

### 2.3 为什么大脑不做成 Node

三条：

1. `observer.py` 用 ctypes 调 Win32 拿前台窗口，已经在跑；Node 侧要么用 `ffi-napi`（脆弱、随 Electron 版本炸），要么写 native 模块（没必要）。
2. `digest.py` 的 git 采集逻辑（author date 筛、sha 去重、多 clone 合并）踩过坑，重写等于重踩。
3. Python 侧做数据聚合、后面接本地小模型，都比 Node 顺手。

代价是**两个运行时**。解决办法只有一个，且必须做：**Electron 主进程负责启动和守护 Python 子进程**，用户只看到"打开 Moss"。

---

## 3. 进程与窗口拓扑

三个窗口，一个后台进程。

```
┌─────────────────────────── Electron 主进程（Node） ───────────────────────────┐
│  职责：只能做三件事                                                          │
│  ① 创建三个窗口  ② 启动/守护 brain 子进程  ③ 透传鼠标穿透开关               │
│                                                                              │
│  ┌─────────────┐   ┌─────────────┐   ┌──────────────────────────────┐       │
│  │ 球窗口       │   │ 气泡窗口     │   │ 面板窗口                      │       │
│  │ 160×160     │   │ 420×auto    │   │ 1080×760，普通窗口            │       │
│  │ 透明/置顶/   │   │ 透明/置顶/   │   │ 有标题栏、任务栏可见、可缩放   │       │
│  │ 无边框/穿透  │   │ 不抢焦点     │   │                              │       │
│  └──────┬──────┘   └──────┬──────┘   └──────────────┬───────────────┘       │
└─────────┼─────────────────┼─────────────────────────┼──────────────────────┘
          │  SSE /api/v1/stream（球订阅）              │  REST（面板读写）
          ▼                                           ▼
┌──────────────────────── Python brain（127.0.0.1:47102） ─────────────────────┐
│  采集线程 ──► SQLite ──► 归因 ──► 事实 ──► 检测器 ──► 闸门 ──► 生成 ──► 推送  │
│      ▲                                                                        │
│      └─ 前台窗口 / 空闲时长 / git 提交（3 秒一次采样，60 秒一次聚合）         │
└──────────────┬───────────────────────────────────────────────┬───────────────┘
               │                                               │
         ~/.moss/moss.db                                 LLM API（仅在生成时）
         ~/.moss/goals.yaml
         ~/.moss/config.json
```

### 3.1 三窗口职责边界

| 窗口 | 尺寸 | 特性 | 不该做什么 |
|------|------|------|-----------|
| **球** | 160×160（球本身 56px，其余留白给发光） | `transparent, frame:false, alwaysOnTop:'screen-saver', skipTaskbar, resizable:false`，默认鼠标穿透 | 不显示文字、不接业务请求 |
| **气泡** | 360 宽，高自适应 ≤ 180 | `transparent, frame:false, alwaysOnTop, focusable:false`，用 `showInactive()` 显示 | 不抢焦点、不互相遮挡、不同时开两个 |
| **面板** | 1080×760，最小 900×600 | 普通窗口，唯一可聚焦的窗口 | 不做常驻、不自动弹出 |

### 3.2 崩溃与降级

| 情形 | 行为 |
|------|------|
| brain 启动失败 / 心跳丢失 > 30s | 球进入 `offline`：呼吸变慢、整体压到 35% 不透明度、**不发光**。面板顶部黄条："采集未运行" |
| brain 崩溃 | 主进程指数退避重启（1s→2s→4s→…→60s，最多 10 次），日志写 `~/.moss/logs/` |
| LLM 不可用 | 走模板兜底（§9.4），**不静默丢弃**，也不降级成沉默 |
| 数据库损坏 | 建新库，旧库改名 `.corrupt`，面板提示一次。不自动删除 |
| 球窗口被系统杀掉（如 DWM 重启） | 主进程监听 `win.isDestroyed()`，5 秒后重建 |

**原则：任何故障都要在界面上可见。** 一个坏了但看起来正常的助手，比一个报错的助手坏十倍。

---

## 4. 目录结构

### 4.1 仓库树

```
moss/
├── apps/
│   └── ball/                          # Electron 壳体。唯一允许调用 Electron API 的地方
│       ├── src/
│       │   ├── main/
│       │   │   ├── index.ts           # app 生命周期、单实例锁、开机自启
│       │   │   ├── windows.ts         # 三个窗口的创建与定位（唯一设置 BrowserWindow 的地方）
│       │   │   ├── brain-process.ts   # spawn python、健康检查、退避重启、优雅退出
│       │   │   ├── mouse.ts           # 鼠标穿透开关（见 §5.4）
│       │   │   └── ipc.ts             # 主进程 ↔ 渲染进程的桥
│       │   ├── preload/
│       │   │   └── index.ts           # contextBridge，只暴露 window.moss 三个方法
│       │   └── renderer/
│       │       ├── ball.tsx           # 挂载 <Ball/>
│       │       ├── bubble.tsx         # 挂载 <Bubble/>
│       │       └── panel.tsx          # 挂载 <Panel/>
│       ├── index.html                 # 三份入口用 ?win=ball|bubble|panel 区分
│       ├── electron.vite.config.ts
│       └── package.json
│
├── packages/
│   ├── ui/                            # 纯展示层。禁止 import fetch / fs / electron
│   │   ├── src/
│   │   │   ├── ball/
│   │   │   │   ├── Ball.tsx           # 状态 → 视觉，纯函数
│   │   │   │   ├── ball.css           # 呼吸、发光、脉冲的全部参数
│   │   │   │   └── palette.ts         # 发光语义色表（§5.2）
│   │   │   ├── bubble/
│   │   │   │   ├── Bubble.tsx
│   │   │   │   └── placement.ts       # 贴边翻转算法
│   │   │   ├── panel/
│   │   │   │   ├── Panel.tsx          # 路由 + 侧栏
│   │   │   │   └── pages/
│   │   │   │       ├── Today.tsx      # 今天（时间轴 + 事实 + 纠正）
│   │   │   │       ├── Goals.tsx      # 目标编辑
│   │   │   │       ├── Messages.tsx   # 它说了什么 + 为什么 + 反馈
│   │   │   │       ├── Settings.tsx   # API、预算、免打扰、黑名单
│   │   │   │       └── Data.tsx      # 导出 / 删除 / 暂停
│   │   │   ├── components/            # Timeline、GoalCard、EvidenceList 等复用件
│   │   │   └── theme.css              # 颜色、字号、间距的唯一来源
│   │   └── package.json
│   │
│   ├── contracts/                     # 前后端唯一的共享契约
│   │   ├── state.schema.json          # state.json 的 JSON Schema（生成 TS 类型 + Python 校验）
│   │   ├── api.schema.json            # REST 请求/响应
│   │   └── goals.schema.json          # goals.yaml 结构
│   └── tsconfig.base.json
│
├── services/
│   └── brain/
│       ├── moss_brain/
│       │   ├── __main__.py            # 命令行入口：serve / once / report / goal
│       │   ├── app.py                 # HTTP 服务装配（标准库 http.server 够用）
│       │   ├── api.py                 # 路由表，一函数一路由
│       │   ├── sse.py                 # 事件推送（轮询 ready_queue 的生成器）
│       │   ├── config.py              # 读 ~/.moss/config.json，带默认值与迁移
│       │   ├── paths.py               # 所有路径常量的唯一来源
│       │   ├── observe/
│       │   │   ├── windows.py         # 前台窗口：标题/进程/路径（ctypes）
│       │   │   ├── idle.py            # GetLastInputInfo 空闲秒数
│       │   │   ├── browser.py         # CDP 9222 拿 URL（可选，可用则用）
│       │   │   ├── git.py             # 从 daily_report/digest 迁移来的采集逻辑
│       │   │   └── loop.py            # 3s 采样 → 会话切分 → 落库
│       │   ├── attribute/
│       │   │   ├── matcher.py         # 窗口/URL/路径 → repo/goal，含置信度
│       │   │   ├── rules.py           # 用户纠正沉淀下来的规则，优先级最高
│       │   │   └── classify.py        # 应用级兜底分类（读/聊/影音/终端/编辑器）
│       │   ├── facts/
│       │   │   ├── daily.py           # 分钟级事实：专注、碎片、未归因、最长块
│       │   │   └── baseline.py        # 14 日个人基线，供"比你自己"用
│       │   ├── detect/
│       │   │   ├── detectors.py       # D1–D6（§9.1），每个是纯函数
│       │   │   └── budget.py          # 配额、冷却、免打扰、信任余额（§9.2）
│       │   ├── compose/
│       │   │   ├── llm.py             # OpenAI 兼容客户端，超时 3s，一次重试
│       │   │   ├── prompt.py          # 提示词组装
│       │   │   ├── validate.py        # 输出校验：长度、禁词、必须含证据数字
│       │   │   └── templates.py       # 兜底话术（每个检测器一条）
│       │   ├── store/
│       │   │   ├── db.py              # 连接、WAL、事务封装
│       │   │   ├── schema.sql         # §11.3 的 DDL
│       │   │   └── migrations/        # 001_init.sql, 002_*.sql
│       │   └── goals/
│       │       ├── model.py           # Goal 数据类 + 校验
│       │       └── loader.py          # 读 yaml、监听 mtime、热重载
│       ├── tests/
│       │   ├── test_attribute.py      # 用真实标题样本做表驱动测试
│       │   ├── test_detectors.py
│       │   ├── test_validate.py       # 禁词、长度、数字必含
│       │   └── fixtures/              # 一段真实的一天会话序列
│       └── pyproject.toml
│
├── scripts/                           # 【保留】v1 之前的原型脚本，见 §14
│   ├── observer.py  digest.py  report.py  daily_report.py
│   └── ...
│
├── examples/                          # 真实产物样例（已有 7 份）
├── docs/
│   ├── design.md                      # 为什么
│   ├── build.md                       # 现在有什么
│   └── v1-spec.md                     # 本文：怎么做
│
├── .gitattributes  .gitignore  README.md
└── package.json                       # 根：pnpm workspace + 统一脚本
```

### 4.2 依赖方向（唯一合法方向，违反就 review 打回）

```
apps/ball  ──►  packages/ui  ──►  packages/contracts
    │                                    ▲
    └──────────HTTP/SSE──────────────────┘
                                         │
                          services/brain ┘
```

具体四条禁令：

1. **`packages/ui` 不许 import `electron`、`fs`、`fetch`。** 数据一律通过 props 传入；请求由 `apps/ball` 的 preload 注入一个 `api` 对象。
2. **`packages/ui` 不许出现业务判断。** 不许写 `if (focusMin < 25) showWarning()`。UI 只负责"这个数显示成什么样"。
3. **`services/brain` 不许知道屏幕宽度、窗口位置、像素。** 定位是壳体的事。
4. **`contracts` 只能被引用，不许反向 import 任何包。** 它是叶子。

### 4.3 命名与格式

| 范围 | 规则 | 例 |
|------|------|-----|
| Python 模块/包 | `snake_case` | `facts/daily.py` |
| Python 类 | `PascalCase` | `class Goal` |
| React 组件文件 | `PascalCase.tsx` | `Today.tsx` |
| 其他 TS 文件 | `camelCase.ts` | `placement.ts` |
| 目录 | `kebab-case` | `services/brain` |
| JSON / API 字段 | `snake_case`（与 Python 一致，避免两侧转换） | `focus_min` |
| 数据库表 | 单数 `snake_case` | `session`、`facts_daily` |
| API 路径 | `/api/v1/<资源复数>` | `/api/v1/sessions` |
| 提交信息 | `<type>: <中文描述>`，type ∈ feat/fix/refactor/docs/chore | `feat: 增加漂移检测器` |

根 `package.json` 的三个脚本，其他人 clone 下来只需要记这三条：

```bash
pnpm dev        # 同时起 brain（python -m moss_brain serve）和 ball（electron-vite dev）
pnpm test       # pytest + vitest
pnpm build      # 打 exe（electron-builder + pyinstaller，v1 末期再做）
```

### 4.4 数据放哪：代码和数据必须分家

**所有运行时数据在 `%USERPROFILE%\.moss\`，不在仓库里。**

```
~/.moss/
├── moss.db            SQLite（事件、会话、事实、消息、纠正）
├── goals.yaml         目标（人可读可手改）
├── config.json        API key、预算、免打扰、黑名单
├── logs/              按天滚动的日志，保留 7 天
├── runtime.json       端口、token（0600）
└── backup/            每日自动压缩一份 db，保留 7 天
```

理由有三条，都很实际：

1. **仓库永远干净。** 跑一天不会多出 20 个未跟踪文件，`git status` 永远可直接读。
2. **`rm -rf ~/.moss` 就等于重置。** 出问题时的第一反应可以放心执行，不用担心删掉代码。
3. **代码可以公开，数据不能。** 这个仓库是 public，`.gitignore` 只是第一道保险，物理分开才是根治。

> 迁移提示：现有 `scripts/digest.py` 写到 `scripts/out/digest/`。v1 不改它（见 §14），新代码一律进 `~/.moss/`。

---

## 5. 球

### 5.1 视觉规格

| 项 | 值 | 说明 |
|---|---|---|
| 画布 | 160×160 | 多余空间留给发光扩散，避免被窗口边缘切掉 |
| 球体 | 直径 56px，居中 | |
| 形状 | 正圆 + 单一非对称高光（左上 30%、半径 40%、白色 12%） | **一个高光**就能让它读作球而不是圆点 |
| 主色 | `#6E7F73`（灰绿，苔色）纵向渐变到 `#525F56` | 与名字一致，低饱和，不抢眼 |
| 阴影 | 外层 120px 径向渐变，`rgba(110,127,115,.18)` | 模拟"落在桌面上的软影"，不是描边 |
| 禁止 | 眼睛、脸、文字、数字、贴纸、加载动画 | 见 `design.md` §4.2：不要写实人脸 |

### 5.2 发光语义表（这是"动作"的全部词汇）

球"有动作"只有一个形式：**发光**。但发光必须有语义，否则就是装饰。

| 语义 | 色 | 什么时候 | 持续 |
|---|---|---|---|
| `win` 进展 | `#4C9A6A` 绿 | 里程碑达成、连续专注破纪录 | 脉冲 2 次，之后 30% 常亮 8s |
| `notice` 注意到 | `#E8A33D` 琥珀 | 有件事值得说，但还在等合适时机（Tier 0） | 脉冲 1 次，回落到 0 |
| `queued` 有话说 | `#5B7FA6` 蓝 | 消息已生成，等你主动点它 | 缓慢呼吸式明暗，20%↔45%，周期 3s，直到被点或超时 |
| `alert` 出问题 | `#C4623F` 砖红 | 采集失败、API 失败、仓库冲突 | 脉冲 3 次，之后 15% 常亮 |
| 无 | — | 其余全部时间 | 关闭 |

**三条硬规则：**

1. **发光不伴随声音、不伴随位移、不弹窗。** 它只是"变了一下颜色"，是否查看由你决定。
2. **同一天被忽略 3 次发光，当天不再发光**（只进日报）。这是把"别烦我"做进物理层，而不是做进文案层——文案层永远会失守。
3. **发光总时长一天上限 10 分钟。** 超过就降级为"面板里一条未读"。

### 5.3 状态机

沿用 `design.md` §4.1 的六个状态，v1 全部实现（都是 CSS 动画，不需要图片资源）：

| 状态 | 触发条件 | 视觉参数 |
|------|----------|----------|
| `sleep` | 空闲 > 10 分钟 | 呼吸周期 9s，缩放 1.00↔1.015，整体不透明度 35%，色偏冷 |
| `idle` | 有活动，但当前活动未归因到任何目标 | 呼吸 4.5s，缩放 1.00↔1.03，不透明度 100% |
| `alongside` | 当前活动已归因到某目标 | 呼吸 6.5s（更慢更稳），微微向屏幕内侧偏移 2px（"坐在你旁边"） |
| `curious` | 检测器命中，闸门放行到 Tier 0 | 先停呼吸 0.4s（"顿一下"），然后发光脉冲 |
| `speaking` | 你点了球 / 气泡已展开 | 呼吸暂停，球体轻微放大到 1.06，气泡同时出现 |
| `thinking` | 每日整理（默认 22:30） | 呼吸周期 1.8s（快而浅），持续到整理结束，最长 60s |
| `offline` | brain 失联 | 呼吸 9s，不透明度 35%，灰阶（`filter: grayscale(1)`），不发光 |

> 注意 `alongside` 是**默认状态**，不是特殊状态。一天里它 80% 的时间应该在这里——在你旁边，但不说话。

### 5.4 Windows 上真正会出问题的地方

这一节是给实现者的，写具体到能照抄：

**① 鼠标穿透必须做，且必须做对。**
纯 `setIgnoreMouseEvents(true)` 会让球点不到；纯 `false` 会让 160×160 的透明区域吞掉你所有点击（最烦人的 bug）。正确做法：

```ts
// 默认穿透，但转发移动事件，这样渲染进程能收到 mousemove
win.setIgnoreMouseEvents(true, { forward: true })

// 渲染进程检测指针进出球体半径 32px
// 进入 → ipc → win.setIgnoreMouseEvents(false)
// 离开 → ipc → win.setIgnoreMouseEvents(true, { forward: true })
```

**② 不要给透明窗口开 `backgroundColor` 和 `hasShadow`。** 会得到一圈毛边或不消失的黑底。

**③ 动画只能用 `transform` 和 `opacity`。**
`filter: drop-shadow()` 每帧重算，透明窗口上 GPU 开销极高，笔记本风扇会响。发光用一张预先做好的径向渐变层调 `opacity`，不要动态算模糊。

**④ 不要用 `requestAnimationFrame` 驱动呼吸。** 用 CSS `@keyframes` + `animation-play-state`，空闲时 GPU 能真正停下来。实测这是 CPU 从 2% 降到 0.2% 的唯一有效手段。

**⑤ 定位按 `screen.getDisplayNearestPoint(cursor)` 算，且要监听显示器变化。**
否则插拔外接屏后，球会跑到不存在的坐标上（表现为"球不见了"）。

**⑥ `alwaysOnTop: 'screen-saver'` 会让它盖在全屏视频/游戏上。**
必须监听全屏窗口并临时隐藏：检测到前台窗口尺寸 == 显示器尺寸且非本进程，就把球隐藏（`setOpacity` 不行，要 `hide()`），退出全屏再显示。

**⑦ 开机自启不要用启动文件夹快捷方式，用 `app.setLoginItemSettings`。** 快捷方式在中文路径下会失效。

---

## 6. 消息显示

### 6.1 三级递进

**核心原则：默认什么都不显示。** 显示是一种成本，不是一种能力。

| 级别 | 形式 | 何时用 | 自动消失 |
|---|---|---|---|
| **Tier 0** | 只有发光，没有字 | 它注意到了什么，但不确定值不值得说 | 6s 后自然暗下去 |
| **Tier 1** | 球旁一行字，无按钮、无图标 | 一句事实或一个观察，你不需要回应 | 8s，鼠标移上去暂停计时 |
| **Tier 2** | 气泡，1–3 行 + 最多 3 个选项按钮 | 需要你做选择（改目标、砍范围、延期限） | 不自动消失，直到你点或点别处 |

**Tier 2 只能由你触发。** 系统永远不主动展开气泡——这条是防止这个产品变成弹窗广告的最后一道闸。

### 6.2 文本规格

| 项 | 规格 |
|---|---|
| 长度 | Tier 1 ≤ 34 字；Tier 2 每行 ≤ 30 字，最多 3 行 |
| 字体 | 系统 UI（`"Microsoft YaHei UI", -apple-system`），13px / 行高 1.6 |
| 颜色 | 深色底卡片 `rgba(28,32,30,.92)`，文字 `#EDEFEA`，模糊背景 `backdrop-filter: blur(12px)` |
| 圆角 | 10px；距球 12px |
| Markdown | **不支持**。不渲染链接、不加粗、不列表——只有纯文本，避免歧义 |
| 按钮 | 纯文字 chip，`#E8A33D` 描边，最多 3 个，第四个功能必须是"别管了" |

### 6.3 位置

气泡锚定在球的上方，距球 12px。空间不足时按顺序翻转：**上 → 下 → 左 → 右**，允许部分移出工作区但保持球可见。
跟随球移动时用 `transform: translate` 补间（120ms），不要重建窗口。

### 6.4 文案纪律（写进代码，不写进规范就是废话）

以下规则在 `compose/validate.py` 里硬校验，不合格就重试或走模板：

| 禁止 | 原因 | 例子 |
|---|---|---|
| 感叹号 | 热情是压力 | ~~"今天效率很高！"~~ |
| "你应该 / 你需要 / 建议你" | 裁判口吻 | 改成"要不要…" |
| "加油 / 辛苦了 / 很棒" | 情绪代糖，一次就腻 | 全部删除 |
| 不含具体数字 | 无数字 = 无证据 = 不可信 | 必须含至少一个来自 evidence 的数字 |
| 一次问两个问题 | 没法回答 | 拆成两次 |
| emoji | 与球的克制感冲突 | — |

**唯一允许的三种句式：**

1. **陈述事实**：`dcc 今天 14:20 之后没动过。`
2. **给选项**：`按现在这个节奏 11/20 能完成，你原本想 11/15。要不要 ①这周多两个晚上 ②砍掉第二个场景 ③改期限？`
3. **问一句**：`这一段你回来三次了，要不要换个讲法？`

---

## 7. 观察层（v1 最重要的功能）

### 7.1 采什么

| 数据 | 频率 | 来源 | 拿不到的降级 |
|------|------|------|-------------|
| 前台窗口标题 | 3s | `GetForegroundWindow` + `GetWindowTextW` | 已实现（`observer.py`） |
| 进程名 / exe 路径 | 3s | `GetWindowThreadProcessId` + `QueryFullProcessImageName` | 已实现 |
| 空闲秒数 | 3s | `GetLastInputInfo` | 已实现 |
| 浏览器 URL | 3s（仅浏览器在前台时） | CDP `127.0.0.1:9222/json` | 拿不到就只用标题（准确率从 ~90% 降到 ~75%） |
| git 提交 | 60s | `git log --all`（按 author date 筛） | 已实现（`digest.py`） |
| 全屏状态 | 3s | 窗口矩形 == 显示器矩形 | 拿不到就跳过（宁可漏报不误报） |

**不采**：键盘内容、剪贴板、截图、麦克风、摄像头、文件内容。这条写进 `docs/` 也写进代码注释，避免半年后自己心软。

### 7.2 会话切分

不是按固定时长切，按**行为边界**切：

```
新会话开始的条件（任一）：
  ① 前台窗口进程变了
  ② 同一进程，但标题变化超过"标题相似度 < 0.6"
  ③ 空闲 ≥ 90 秒后重新有输入
  ④ 跨过零点
单会话最短 5 秒（更短的并入上一段，避免刷新页面刷出 200 段）
一天预期 80–150 段
```

标题相似度用最简单的词集合 Jaccard（去数字后比较），不引入任何模型。

### 7.3 归因算法（准确率的全部来源）

对每个会话，按优先级依次尝试，命中即停：

| 序 | 依据 | 置信度 | 例 |
|---|---|---|---|
| 1 | **用户纠正规则**（`attribute/rules.py`，持久化） | 1.00 | 你上次说"这个标题属于 dcc-i18n" |
| 2 | **仓库路径**：标题/URL 里出现已知 repo 的绝对路径或 slug | 0.95 | `D:\yzj\dcc-workspace` |
| 3 | **URL 域名 + 路径**：`github.com/lanxue0/Moss` → repo `Moss` | 0.90 | |
| 4 | **目标信号词**：`goal.signals.patterns` 任一命中标题（不区分大小写） | 0.85 | `i18n`、`MentorDoc` |
| 5 | **关联仓库名**：`goal.signals.repos` 命中 | 0.85 | |
| 6 | **应用级兜底分类**（读/聊/影音/终端/编辑器） | 0.40 | 只用于统计"未归因"，不用于归因 |
| — | 全部未命中 | 0 | **判为"未归类"，不猜** |

**铁律：置信度 < 0.7 一律不归因，进"未归类"。**

理由：一次错误的归因，会让"你在这个目标上花了多久"整个数字不可信；而"未归类 38 分钟"是一个诚实的、可以改进的数。宁可少知道，也不要让用户怀疑——`design.md` §7 里已经写过这条。

**纠正即学习：** 你在面板上把一段"未归类"拖到某个目标，同时生成一条规则：

```json
{"kind":"title_pattern","pattern":"*知识库*","goal_id":"dcc-i18n","created_at":1750000000,"hits":0}
```

规则按 `hits` 排序展示，`hits` 为 0 且 30 天未被用到的规则自动进入"可清理"列表，但**不自动删**。

### 7.4 每日事实（SQL 能算出来的全部）

这些是"它凭什么说你有进展"的唯一依据。**全部由 SQL 计算，不含模型。**

| 指标 | 定义 | 用途 |
|------|------|------|
| `focus_min[goal]` | 该目标下所有会话时长之和，且排除空闲 | 进度 |
| `blocks_over_25` | 连续归因同一目标 ≥ 25 分钟的次数 | 深度工作 |
| `longest_block_min` | 当天最长连续块 | 破纪录检测 |
| `switches_per_hour` | 会话切换次数 / 活跃小时 | 碎片度 |
| `unattributed_min` | 归因为 0 的会话总时长 | 漂移检测 |
| `first_active` / `last_active` | 首个/末个有输入的会话 | 作息 |
| `git_commits[repo]` | 来自 git 采集 | 产出旁证 |
| `idle_ratio` | 空闲时长 / 在座时长 | 卡住检测 |

> **绝不产出"效率分"。** 没有 0–100 的分数，没有 A/B/C 评级。原因见 `design.md` §0：分数会变成你对抗它、而不是用它。

### 7.5 个人基线

`facts/baseline.py` 维护滚动 14 日的分位数（p25/p50/p75），只做一件事：**把"你今天怎么样"换成"你比你自己怎么样"。**

```
不说：你今天专注了 74 分钟（少得可怜）
而说：74 分钟，比你这半个月的中位数 132 分钟少一半
```

这句话的力量完全来自基线。**所以 v1 前 14 天的数据必须攒住，不能重置**——这也是为什么 `~/.moss` 和代码仓库分开，删代码不等于删数据。

---

## 8. 目标

### 8.1 存在哪

**`~/.moss/goals.yaml`，人和程序都能读写。**

- 面板里的"目标"页写它
- 你也可以直接用记事本改，`loader.py` 监听 mtime，改完 2 秒内生效
- 每次写入前备份到 `~/.moss/backup/goals-{ts}.yaml`（保留 10 份）

选 YAML 而不是数据库，理由很实在：目标是你最需要"想清楚"的东西，纯 GUI 表单会逼着你迁就字段。文本文件里可以写长句、可以注释、可以纠结。

### 8.2 Schema

```yaml
version: 1
goals:
  - id: dcc-i18n                  # 唯一、稳定、永不改（改了历史数据就断）
    title: dcc-workspace 国际化上线
    kind: project                 # project | learning | habit
    status: active                # active | paused | done | dropped
    deadline: 2026-11-15
    why: 海外客户要用，是今年最硬的节点        # 只影响语气，不参与判断
    success: i18n 分支合并进 main 并通过验收    # 必须可判定
    pace:
      hours_per_week: 12          # 你自认为的节奏，判断"慢了"的分母
    signals:                      # ★ 最关键字段：它怎么知道这段工作算这个目标
      repos: [dcc-workspace]
      patterns: ["i18n", "国际化", "dcc"]
      paths: ["D:\\yzj\\dcc-workspace"]
    milestones:
      - { id: m1, title: 文案抽取完成, target: 2026-10-28 }
      - { id: m2, title: 全量替换, target: 2026-11-05 }
```

**`signals` 是整个系统准确率的命门。** 没有它，归因只能靠猜；有了它，80% 的时间能确定归属。
所以第一次运行的引导里，必须强制填这一项（可以少，不能空）。

### 8.3 目标怎么影响回应：只用三个量

目标不是给 LLM 的背景故事，它被折算成三个可计算的数：

| 量 | 公式 | 用途 |
|----|------|------|
| **期望进度** `expected` | `(今天 - 开始日) / (期限 - 开始日)`，按 `pace.hours_per_week` 折算成小时 | 分母 |
| **实际进度** `actual` | 该目标全部 `focus_min` 累计（含 git 提交加成） | 分子 |
| **缺口** `gap` | `expected - actual`，正数表示落后 | 触发检测器 |

然后：

```
gap < 10%        → 不主动提进度。只在你自己点开时说"和计划差不多"。
10% ≤ gap < 30%  → Tier 1 提一次，陈述事实，不给建议。
gap ≥ 30%        → Tier 2（必须等自然断点），给三个选项。
```

**里程碑是另一条独立的判断线**，不看 gap 只看日期：距 `milestone.target` 剩 3 天且无相关提交 → Tier 1。

### 8.4 第一跑引导（决定这个产品能不能活过第一天）

第一次启动不弹欢迎页，只做三步，全部可跳过：

1. **选 1–3 个目标**（不给模板，就问"这周你想推进什么"，加期限）
2. **每个目标补 signals**（面板会列出扫描到的仓库，你勾选 + 输入 2 个关键词）
3. **填 API**（base_url、model、key）→ 点"测一下"，失败也能继续（模板兜底可用）

**不许超过 3 步，不许超过 2 分钟。** 第一天最有价值的事是让它开始采集，不是让它懂你。

---

## 9. 从目标 + 事实到回应

这是整套设计里唯一需要动脑的部分。分三步，**顺序不能变**，因为每步的职责必须单一：

```
事实(SQL) ──► ① 检测器（纯函数，0 个模型） ──► ② 闸门（纯规则，0 个模型） ──► ③ 生成（1 次 LLM）
                  产出"候选"+"证据"              放行 / 排队 / 丢弃            产出一句人话
```

### 9.1 ① 检测器：只找"值得说的事"

每个检测器是一个纯函数：`(facts, goal, history) -> Candidate | None`。
`Candidate = {id, severity, evidence, intent_hint}`，**不含任何文本**。

| ID | 触发条件 | severity | 默认层级 | 证据 |
|----|----------|----------|----------|------|
| **D1 漂移** | `unattributed_min ≥ 45` 且当前有 `gap > 10%` 的活跃目标 | warn | Tier 1 | 未归因分钟数 + 主要应用 |
| **D2 卡住** | 同一标题 90 分钟内出现 ≥ 3 次；或单会话 ≥ 30 分钟且 `idle_ratio > 0.6` | info | Tier 0 → 1 | 标题 + 出现次数 + 时间跨度 |
| **D3 落后** | `gap ≥ 30%` 且距期限 ≤ 14 天 | warn | Tier 2 | gap 小时数 + 按期完成所需节奏 |
| **D4 停滞** | 活跃目标连续 2 个工作日 `focus_min = 0` | info | Tier 1 | 天数 + 上次活动时间 |
| **D5 进展** | 里程碑达成 / `longest_block` 破 14 日记录 / 连续 3 天达标 | good | Tier 1 | 具体数字 |
| **D6 该歇了** | 连续 3 个 ≥ 90 分钟块之间无 ≥ 5 分钟间隔 | info | Tier 1（仅自然断点） | 连续时长 |

**检测器要能单独关。** 面板里 D1–D6 各一个开关，默认全开但 D5 可关（有人不喜欢被夸）。

### 9.2 ② 闸门：决定"现在该不该说"

按顺序过五道，任何一道不过就**不发声**（但仍记一条 `suppressed`，面板里可见"它本来想说但没说"，这本身是信任的来源）。

| 闸 | 规则 | 不过的后果 |
|----|------|-----------|
| **G1 时机** | 必须落在自然断点：会话刚切换 / 空闲 ≥ 90s / 保存文件后 30s 内 / 合盖前 | 入队，等下一个断点（队列 TTL 2 小时） |
| **G2 免打扰** | 不在 `quiet_hours`（默认 22:30–08:30）；不在全屏应用；不在用户明确"暂停"期间 | 丢弃或推迟到次日 |
| **G3 冷却** | 距上一次发声 ≥ 45 分钟（Tier 2 与 Tier 1 独立计） | 入队 |
| **G4 配额** | Tier 1 ≤ 3 次/天，Tier 2 ≤ 1 次/天（沿用 `design.md` §3.6 的 3 次） | 降级为 Tier 0 发光；若发光也已超限 → 只进日报 |
| **G5 信任余额** | 余额 = Σ(有用 +1 / 烦人 −3)。余额 < 0 → 静默 7 天，只出日报 | 关闭全部主动发声 |

**再加一条运行时的自我约束（v1 新增，比上面五条都重要）：**

> **同一检测器在一周内被你忽略 2 次，自动静音 7 天，并在面板里明确告诉你"我把 D1 关了 7 天，因为前两次你没理它"。**

这条的价值不在静音，在于**它承认自己被无视了**。一个会自我收敛的助手，才配继续待着。

### 9.3 ③ 生成：只负责把事实写成一句话

**LLM 不参与判断**，只做"事实 → 人话"的翻译。这是本设计最重要的一条边界。

**输入（全部结构化，不塞上下文）：**

```json
{
  "candidate": {"id": "D3", "severity": "warn"},
  "evidence": {"gap_hours": 6.2, "days_left": 23, "required_per_week": 18, "current_per_week": 11.8},
  "goal": {"title": "dcc-workspace 国际化上线", "why": "海外客户要用", "deadline": "2026-11-15"},
  "recent_messages": ["dcc 今天 14:20 之后没动过。", "74 分钟，比中位数少一半。"],
  "constraints": {"max_chars": 34, "max_questions": 1, "no_exclamation": true, "must_include_number": true}
}
```

**输出约束（`validate.py` 硬校验，不合格重试 1 次，再不合格走模板）：**

| 检查 | 规则 |
|------|------|
| 长度 | ≤ 34 字（Tier 2 允许 3 行 × 30 字） |
| 数字 | 必须包含至少一个来自 `evidence` 的数字（子串匹配） |
| 禁词 | `！!`、`你应该`、`你需要`、`建议你`、`加油`、`辛苦`、`很棒`、`优秀`、`效率` |
| 问号 | ≤ 1 个 |
| 重复 | 与 `recent_messages` 任一条的词重合度 > 0.7 则重试 |
| emoji | 正则拒绝 |

**超时 3 秒。** 超过就直接走模板——用户等 8 秒等来一句话，那句话再好也是负担。

### 9.4 模板兜底（必须存在，且质量不能太差）

每个检测器一条，用 evidence 填空：

```python
TEMPLATES = {
  "D1": "{minutes} 分钟没归到任何目标，主要在 {app}。",
  "D2": "这段你回来 {n} 次了。",
  "D3": "按现在的节奏 {estimated} 能完成，你原本想 {deadline}。",
  "D4": "{goal} 已经 {days} 天没动了。",
  "D5": "{goal}：{milestone} 完成，比计划早 {days} 天。",
  "D6": "连续 {hours} 小时了，要不要起来一下。",
}
```

模板版读起来是"机器在报数"，但**诚实、可验证、不会尬**。这比一句润色过但晚了 6 秒的话好。

### 9.5 完整链路示例（真实时间戳，可照着写测试）

```
14:20  最后一条 dcc-workspace 提交
14:22~15:04  会话 12 段，全部未归因（B 站 / 知乎 / 微信），共 42 分钟
15:06  会话切换到 VSCode（自然断点 ✓）
15:06  事实重算：unattributed_min=42, dcc 今日 focus_min=118, gap=6.2h
15:06  D1 命中（42 ≥ 45? 否）→ 不触发
15:07  再算：unattributed_min=44 → 不触发（差 1 分钟，这就是阈值该有的样子）
15:20  unattributed_min=57 → D1 命中，severity=warn
15:20  G1 时机：当前在 VSCode 连续打字 14 分钟 → 不是断点 → 入队
15:31  空闲 95 秒 → 断点到了，出队
15:31  G2 免打扰：15:31 不在 quiet_hours，非全屏 → 过
15:31  G3 冷却：今天最后一次说话是 11:40，间隔 3h51m → 过
15:31  G4 配额：今日 Tier1 已用 1/3 → 过
15:31  G5 余额：本周 +2 → 过
15:31  ③ 生成（383ms）→ "57 分钟没归到目标，主要在 B 站和知乎。"
15:31  validate：含数字 57 ✓，无禁词 ✓，长度 22 ✓ → 通过
15:31  球：curious（停呼吸 0.4s）→ notice 琥珀脉冲 1 次 → Tier 1 一行字，8s 后消失
15:31  你点了一下 → Tier 2：证据列表（12 段会话）+ 三个选项：
        ① 这段算「学习」  ② 这类以后都别提醒  ③ 只是休息，别管
```

注意这个例子里 **D1 在 15:06 和 15:07 都命中了阈值边缘但没触发**——这不是 bug，是设计：阈值附近反复抖动会产生噪音，所以指标每 60 秒才重算一次，且同一检测器有 10 分钟最小静默。

---

## 10. 控制面板

### 10.1 五个页面

| 页 | 回答的问题 | 关键要求 |
|----|-----------|----------|
| **今天** | 我今天到底在干什么？ | 时间轴（横向 24h，块宽按时长）+ 事实卡片 + **每块可点击纠正** |
| **目标** | 我在往哪走？ | 卡片式编辑，`signals` 用仓库勾选器而非纯文本输入 |
| **它说了什么** | 它凭什么这么说？ | 每条消息一条记录：原文 / 检测器 / 证据 / 当时是否被忽略 / 👍👎 |
| **设置** | 怎么配？ | API（含"测一下"）、配额、免打扰时段、检测器开关、应用黑名单 |
| **数据** | 我的数据在哪？ | 库大小、事件条数、导出 JSON、清空某天、彻底删除 |

### 10.2 三条信息架构原则

**① 一切可溯源。** 面板上任何一个数字，都必须能展开看到它由哪几段会话算出。这决定了它是否可信。

**② 纠正必须是一等公民。** 时间轴上选中一段 → 拖到目标卡片上，两秒完成。这是唯一能让归因准确率真的变高的机制，也是最强的信任仪式。

**③ 设置项不超过 20 个。** 超了说明设计没想清楚。v1 的完整设置清单就是：API 三项、配额两项、免打扰一段、检测器六个开关、黑名单一个列表、存在感一个滑杆。**就这些。**

### 10.3 存在感滑杆（唯一的"总开关"）

三个档，改的是配额和阈值，不是关功能：

| 档 | Tier1 配额 | Tier2 配额 | 冷却 | 说明 |
|----|-----------|-----------|------|------|
| 隐形 | 0 | 0 | — | 只记录，只在日报出现 |
| 安静陪伴（默认） | 3 | 1 | 45min | |
| 主动教练 | 8 | 3 | 20min | 会明显更烦，慎用 |

---

## 11. 接口与数据契约

### 11.1 HTTP（仅 127.0.0.1:47102）

```
GET  /api/v1/health                     → {ok, version, uptime, db_size}
GET  /api/v1/state                      → 当前 state（球启动时拉一次做初值）
GET  /api/v1/stream                     → SSE，每 1s 一次心跳，状态变化立即推
GET  /api/v1/today?date=YYYY-MM-DD      → 事实 + 时间轴 + 消息
GET  /api/v1/sessions?date=&goal=&unattributed=
POST /api/v1/sessions/{id}/attribute    → {goal_id} 或 {unattributed:true}，同时沉淀规则
GET  /api/v1/goals
PUT  /api/v1/goals                      → 整份覆盖写，返回 diff
GET  /api/v1/messages?date=
POST /api/v1/messages/{id}/feedback     → {"score": 1|-1}
GET  /api/v1/settings
PUT  /api/v1/settings
POST /api/v1/pause                      → {"minutes": 30}
POST /api/v1/settings/test-llm          → 用最小提示词试一次，返回延迟与错误
```

**安全（虽然是本地）：** 端口和 token 写在 `~/.moss/runtime.json`（权限 0600），所有请求带 `X-Moss-Token`。
理由：本机任何进程都能访问 localhost，而这里存的是你的完整工作记录。不是防黑客，是防"另一个脚本顺手读一下"。

### 11.2 `state` 契约（球只认这个）

```json
{
  "version": 1,
  "ts": 1761212913,
  "state": "curious",
  "since": 1761212900,
  "intent": {
    "tier": 1,
    "text": "57 分钟没归到目标，主要在 B 站和知乎。",
    "color": "notice",
    "message_id": 82
  },
  "today": { "focus_min": 118, "goal_id": "dcc-i18n", "unattributed_min": 57 },
  "paused": false,
  "queue": 1,
  "online": true
}
```

球对它的处理是纯映射：`state` → CSS class，`intent.color` → 发光色，`intent.text` → Tier 1 文字（为 `null` 则不显示）。

### 11.3 数据库 DDL（v1 全量）

```sql
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS event (
  id       INTEGER PRIMARY KEY,
  ts       INTEGER NOT NULL,
  kind     TEXT NOT NULL,      -- focus_change | idle_start | idle_end | heartbeat
  exe      TEXT, title TEXT, url TEXT, path TEXT,
  idle_sec INTEGER
);
CREATE INDEX IF NOT EXISTS idx_event_ts ON event(ts);

CREATE TABLE IF NOT EXISTS session (
  id           INTEGER PRIMARY KEY,
  started_at   INTEGER NOT NULL,
  ended_at     INTEGER NOT NULL,
  exe          TEXT, title TEXT, url TEXT, path TEXT,
  repo         TEXT,
  goal_id      TEXT,              -- NULL = 未归类
  confidence   REAL,
  corrected    INTEGER DEFAULT 0  -- 1 = 用户手动指定
);
CREATE INDEX IF NOT EXISTS idx_session_day ON session(started_at);
CREATE INDEX IF NOT EXISTS idx_session_goal ON session(goal_id);

CREATE TABLE IF NOT EXISTS facts_daily (
  day             TEXT PRIMARY KEY,
  focus_min       INTEGER, blocks_over_25 INTEGER,
  switches        INTEGER, unattributed_min INTEGER,
  longest_block_min INTEGER, idle_ratio REAL,
  first_active    TEXT, last_active TEXT,
  per_goal_json   TEXT,          -- {"dcc-i18n": {"focus_min":118,"blocks":3}}
  computed_at     INTEGER
);

CREATE TABLE IF NOT EXISTS message (
  id           INTEGER PRIMARY KEY,
  ts           INTEGER NOT NULL,
  detector     TEXT NOT NULL,     -- D1..D6
  tier         INTEGER NOT NULL,
  text         TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  goal_id      TEXT,
  state        TEXT NOT NULL,     -- queued | shown | dismissed | suppressed
  suppress_reason TEXT,           -- G1..G5
  feedback     INTEGER            -- 1 | -1 | NULL
);
CREATE INDEX IF NOT EXISTS idx_message_day ON message(ts);

CREATE TABLE IF NOT EXISTS attribute_rule (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL,       -- title_pattern | url_pattern | path_prefix
  pattern    TEXT NOT NULL,
  goal_id    TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  hits       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
```

**保留策略**：`event` 只留 7 天（一天约 2.8 万条），`session` 及以上永久保留。每天 23:50 清理一次。

---

## 12. 分期与验收

每一期都必须有**中止条件**。没有中止条件的路线图，等于承诺。

### P0 · 球（2–3 天）

做：三窗口骨架 + 球 + 六状态 + 发光 + 鼠标穿透 + 开机自启 + 一个假的状态源（手动切 `state`）。
不做：采集、面板、LLM。

**验收**：连续挂机 8 小时，CPU < 1%、内存 < 200MB，没有一次挡住你的点击，你没想关掉它。
**中止条件**：如果你一整天里主动看它的次数为 0，且第二天觉得"碍事" → 停下来改形态，别往下走。

### P1 · 看见（5–7 天）

做：`observe/` + 会话切分 + `attribute/` + `facts/` + 面板「今天」页（时间轴 + 纠正）+ SQLite。
复用：`scripts/observer.py` 的 Win32 调用、`scripts/digest.py` 的 git 采集。

**验收**：抽查 20 个会话段，归因正确 ≥ 16 段；需要纠正的 ≤ 5 段；纠正后同类会话下次自动归对。
**中止条件**：如果一周后你打开「今天」页的频率 < 每周 2 次 → 说明"回顾"这件事对你没有价值，那么"主动提醒"更不会有价值。

### P2 · 说话（5–7 天）

做：`goals/` + `detect/` + `budget/` + `compose/`（含模板兜底）+ 面板「目标」「它说了什么」「设置」。

**验收**：连续 5 个工作日，每天主动发声 ≤ 3 次；其中至少 1 次你觉得"有用"，最多 1 次觉得"烦"。
**中止条件**：如果连着 3 天你都点了"以后别提醒" → 检测器设计错了，回去改阈值，不要加功能。

### P3 · 收口（视情况）

做：Tier 2 的选项动作真正生效（改期限、砍范围）、周报、导出、打包 exe、数据迁移。

### 顺序不能反的原因

P0 验证"你愿不愿意让它待在屏幕上"，P1 验证"你会不会看它记的东西"，P2 验证"它说话你烦不烦"。
**这三件事任何一件是否定的，后面所有功能都是放大一个错误。** `build.md` 里那句"先验证你会不会看，再验证它该不该说"就是这个意思。

---

## 13. 风险与已知坑

| 风险 | 概率 | 影响 | 应对 |
|------|------|------|------|
| 透明窗在某些显卡/DPI 下黑底或闪烁 | 中 | 高 | P0 第一天就在真机上验证；退化方案：不透明小圆角窗口 |
| 球挡住全屏视频/游戏 | 高 | 中 | §5.4 ⑥ 的全屏检测，必须做 |
| 归因准确率上不去 | 中 | 高 | `signals` 强制填写 + 纠正规则优先 + 低置信度不归因 |
| **太烦，两周后被关掉** | **高** | **致命** | 配额 + 冷却 + 自动静音 + 存在感滑杆，四道闸 |
| 每天盯着"我今天专注多少"变成新的焦虑源 | 中 | 高 | 不产出分数；默认只在点开时显示数字；`design.md` §0 已定 |
| `~/.moss` 被误删，基线归零 | 低 | 中 | 每日自动备份 db 保留 7 天 |
| 两个运行时（Node + Python）启动顺序出错 | 中 | 低 | 主进程健康检查 + 球进入 `offline` 明确可见 |
| 范围蔓延到"帮我写代码" | 高 | 中 | v1 不做任何动作执行，只观察和说话 |

---

## 14. 和现有 `scripts/` 的关系

**不删除、不重写、不搬进 `services/`。** 它们是三样东西，各有各的用处：

| 现有文件 | v1 里的角色 |
|---|---|
| `scripts/observer.py` | **代码来源**：`observe/windows.py` 和 `observe/idle.py` 直接搬 ctypes 部分，加上会话切分 |
| `scripts/digest.py` | **代码来源**：`observe/git.py` 搬 `GitIndex` / `scan_repos` / sha 去重逻辑；同时它是 P1 验收时的对照答案 |
| `scripts/daily_report.py` | **独立工具**：仍然可以手动跑，生成提交到日志仓库的文档。v1 不接管它 |
| `scripts/report.py` | 保留，当作"如果不做实时、只做批处理"的参照实现 |

新增一条约束：**`services/brain` 不许 import `scripts/` 下的任何模块。** 要复用就复制过来，允许暂时重复。
理由：`scripts/` 是无测试、依赖全局状态的脚本；被长期服务 import 进去，第二天就会因为一个 `print` 或 `sys.exit()` 挂掉整个 brain。

---

## 15. 待你决定的四件事

这四件事定了，其余细节才有唯一解：

1. **球的默认位置**：右下角（贴任务栏）还是左上角？我倾向右下角——右下是"人在旁边做事"的位置，左上像通知。
2. **`alongside` 要不要有"陪你一起"的暗示**，比如专注时球体颜色微微向目标色偏移？这会让它更像伙伴，也更可能让人觉得被评判。
3. **D5（夸你）默认开还是关？** 我倾向**关**——主动夸奖比主动提醒更容易变得廉价。
4. **发声中不发声的边界**：D1 漂移（刷手机 45 分钟）算不算"值得说"？这是最容易被当成监控的一条，需要你自己判断能接受到什么程度。

---

> 这份文档只描述第一期。第二期（学习巩固、周复盘、真正的记忆）等 P2 验收通过再写——因为 P2 的结论会改写它。
