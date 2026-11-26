# 本地 LaTeX 论文返修伴侣（Rebuttal Sidecar）开发文档

## 1. 项目概述与核心指标

### 1.1 产品定位

**Rebuttal Sidecar** 是一款专为科研人员设计的轻量级本地桌面软件，挂载于现有科研项目的 `docs/manuscripts/` 目录下。软件不接管文本编辑与 Git 版本控制，而是作为“只读历史提取 + 增量修订对比 + 返修进度看板”的独立伴侣工具，与用户本地的 AI Agent（Cursor、Claude Code 等）及 MiKTeX / TeX Live 环境形成零耦合协作闭环。

### 1.2 核心非功能性指标（性能与体积）

- **冷启动时间**：$< 0.8\text{ s}$（双击即开，无黑框控制台闪烁）。
- **运行时内存占用**：$< 60\text{ MB}$（复用系统原生 WebView2 内核，不捆绑 Chromium）。
- **分发包体积**：单文件 `.exe` 控制在 $15\text{ MB} \sim 25\text{ MB}$ 以内。
- **构建方式**：使用 Python 开发业务逻辑，通过 **Nuitka** 将 Python 源码转译为 C 代码并调用 MSVC/MinGW64 编译为原生机器码二进制文件，兼顾开发效率与 C 级执行性能。
- **默认克制编译原则**：软件默认**关闭自动编译**（`auto_compile = false`），避免后台频繁拉起 LaTeX 引擎占用 CPU；用户可随时在界面中一键开启自动编译开关，并自由配置 LaTeX 编译器路径、编译引擎、构建配方与附加参数。

## 2. 系统架构与技术选型

### 2.1 技术栈选型表

| **架构层级**     | **选型方案**                               | **选型理由（轻量化与速度考量）**                             |
| ---------------- | ------------------------------------------ | ------------------------------------------------------------ |
| **桌面容器层**   | **PyWebView** (`pywebview`)                | 直接调用 Windows 系统内置的 Edge WebView2 渲染界面，省去 Electron 动辄 150MB+ 的体积与高内存开销。 |
| **本地后端服务** | **Bottle + ThreadingWSGIServer**           | 相比 FastAPI 省去 Pydantic/Starlette 的大量依赖导入时间；多线程 WSGI 保证 SSE 长连接不阻塞并发 HTTP/PDF 请求。 |
| **文件系统监听** | **Watchdog** (`watchdog`)                  | 基于 Windows 原生 `ReadDirectoryChangesW` API 实现零轮询事件驱动监听，CPU 闲置占用为 0%。 |
| **实时通信机制** | **SSE (Server-Sent Events)**               | 单向推送源码变更提示、Todo 同步、编译状态与 PDF 刷新信号，浏览器原生 `EventSource` 直接支持。 |
| **前端界面层**   | **原生 HTML5 + 离线 Alpine.js + PDF.js**   | 零 Node.js 构建依赖，前端第三方库全部本地化（vendored）打入静态资源目录，100% 离线可用。 |
| **C 转译与打包** | **Nuitka** + **MSVC / MinGW64**            | 将 Python AST 转译为 C 代码并静态链接，消除解释器启动开销；优先使用 MSVC，缺失时自动回退 MinGW64。 |

### 2.2 系统拓扑与数据流

```text
+-----------------------------------------------------------------------+
|                   Rebuttal Sidecar (Nuitka C-Binary)                  |
|  +---------------------------------+  +----------------------------+  |
|  |   PyWebView 原生桌面窗口         |  | 本地多线程服务 (127.0.0.1) |  |
|  |  - 顶部：工程/编译器/自动编译开关|<-|  - 扫描器 (Scanner)        |  |
|  |  - 左侧：审稿意见 TodoList       |  |  - 监听调度器 (Watcher)    |  |
|  |  - 右侧：Clean / Diff PDF.js     |->|  - 沙盒编译器 (DiffEngine) |  |
|  +---------------------------------+  +----------------------------+  |
+-----------------------------------------------|-----------------------+
                                                | 监听变更 / 只读 Git / 调用编译
                                                v
+-----------------------------------------------------------------------+
|                 宿主科研项目文件系统 (Host Workspace)                 |
|  <ProjectRoot>/                                                       |
|  ├── .git/                        <-- 只读查询 Commit / Tag / Archive |
|  └── docs/manuscripts/            <-- 唯一扫描与监听作用域            |
|      ├── references.bib           <-- 共享参考文献库                  |
|      ├── _archive/                <-- 历史归档基准 (自动过滤)         |
|      └── eswa_mainline/           <-- 活跃稿件 (Agent 直接修改)       |
|          ├── main.tex                                                 |
|          ├── sections/*.tex                                           |
|          └── .rebuttal_todo.json  <-- 独立维护的返修任务数据          |
+-----------------------------------------------------------------------+
```

## 3. 目录规范、自动发现与配置持久化

### 3.1 目标目录结构约定

软件启动时接收目标工程根目录（支持命令行传参、拖拽文件夹打开、历史记录记忆或界面目录选择），按以下优先级解析工作区：

1. 若 `<SelectedDir>/docs/manuscripts/` 存在，以 `<SelectedDir>` 作为 `project_root`，以 `<SelectedDir>/docs/manuscripts/` 作为扫描根 `manuscripts_dir`。
2. 若用户直接选中了 `docs/manuscripts` 目录本身，向上识别父级 Git 根目录（若存在），并以 `<SelectedDir>` 作为 `manuscripts_dir`。
3. 若上述路径不存在，回退将 `<SelectedDir>` 本身作为 `manuscripts_dir` 扫描其下含 `.tex` 入口的子目录。

```text
docs/manuscripts/
├── references.bib              # 跨稿件共享文献库
├── _archive/                   # 历史版本存档（仅作为对比基准候选，不列入活跃编辑列表）
│   └── eswa_v1_submitted/
│       └── main.tex
├── eswa_mainline/              # 活跃稿件 A
│   ├── main.tex
│   ├── sections/
│   └── .rebuttal_todo.json
└── ipm_manuscript/             # 活跃稿件 B
    ├── main.tex
    └── .rebuttal_todo.json
```

### 3.2 自动扫描与过滤规则 (`scanner.py`)

扫描 `manuscripts_dir` 下的一级子目录时，按以下漏斗模型过滤干扰项：

1. **前缀过滤**：忽略所有以 `_` 或 `.` 开头的目录（如 `_archive`、`.git`、`.venv`）。
2. **Git 忽略规则过滤**：若存在父级 Git 仓库，调用 `git check-ignore -q <dir_path>`，自动剔除被 `.gitignore` 屏蔽的原始期刊模板解压包或临时目录。
3. **入口文件校验**：目录内必须存在至少一个在非注释行包含 `\documentclass` 声明的 `.tex` 文件（优先匹配 `main.tex`，否则按文件名排序自动识别主文件）。

### 3.3 首次启动与本地应用配置持久化 (`config.py`)

软件绝不向用户科研项目目录写入应用级全局配置。工作区历史与编译器配置统一以原子写入方式保存在 `%APPDATA%/RebuttalSidecar/config.json`：

- 记录 `last_workspace`、`recent_workspaces`（最多保留 10 条有效路径）与 `compiler` 全局设置。
- 首次启动且无命令行入参或历史工程时，前端进入清晰的空工作区引导态，提供“打开工程目录”原生文件夹选择按钮（`POST /api/workspace/select`）与路径输入切换（`POST /api/workspace/open`）。

## 4. 核心模块详细设计

### 4.1 沙盒化 `latexdiff` 编译引擎 (`diff_engine.py`)

由于实际论文存在多层 `\input{sections/...}` 嵌套及跨层级引用 `../references.bib`、图片目录 `figures/`，直接在原目录生成 `diff.tex` 会污染工作区，且简单复制目录极易导致相对路径丢失。本模块采用**隔离式目录镜像 + 父级共享映射沙盒**：

1. **隔离沙盒目录布局**：
   在系统临时目录创建独立工作区 `%TEMP%/rebuttal_sidecar/<workspace_hash>/<manuscript>/`：
   ```text
   <sandbox>/<manuscript>/
   ├── references.bib      <-- 从 docs/manuscripts/*.{bib,bst,cls,sty} 同步，保证 ../references.bib 有效
   ├── old/                <-- 基准版本原始文件及 flat_old.tex
   ├── new/                <-- 当前版本 flat_new.tex
   ├── build_clean/        <-- Clean 版编译目录（输出 clean.pdf），与 diff 完全隔离防止 .aux/.bbl 互踩
   └── build_diff/         <-- Diff 版编译目录（输出 diff.pdf）
   ```
   - 严禁将 `build_clean/` 或 `build_diff/` 自身创建为指向宿主稿件目录的 Junction（否则编译产物会污染宿主目录）。
   - 仅将当前稿件的 `.tex`/`.bib`/`.bst`/`.cls`/`.sty` 源码复制到 `build_clean/` 与 `build_diff/`；对于只读资源子目录（如 `figures/`、`images/`、`figs/`、`assets/`、`plots/`），优先使用 Windows 目录联接（`cmd /c mklink /J`），失败时回退为目录复制。

2. **纯 Python 递归展平 (`flatten_tex`)**：
   - 使用内置纯 Python 解析器递归展开 `\input{...}` 与 `\include{...}`（自动忽略 `%` 注释内容、自动补全 `.tex` 后缀、为 `\include` 补充 `\clearpage` 边界、内置环路检测防死循环），避免部分环境下 `latexpand` 等待标准输入挂起的问题。
   - 生成 `new/flat_new.tex` 与 `old/flat_old.tex`。

3. **基准版本提取 (`extract_baseline`)**：
   - **模式 A（Git 历史基准）**：计算稿件目录相对 Git 根目录的 POSIX 路径，执行 `git archive --format=tar <ref>:<posix_rel_path>`，通过 Python `tarfile` 从内存字节流直接解压至 `old/` 根目录（避免嵌套 `docs/manuscripts/<name>/` 前缀），再执行 `flatten_tex`。
   - **模式 B（`_archive` 文件夹基准）**：复制 `_archive/<sub_dir>/` 源码至 `old/`，识别入口 `.tex` 并展平。
   - **模式 C（无基准 / Clean-Only 模式）**：当新项目尚无 Git 历史或 `_archive` 基准时，允许 `active_baseline = null`，仅编译 `Clean` 版 PDF，界面清晰提示当前无对比基准。

4. **生成差异、引擎选择与增量编译**：
   - 优先执行 `latexdiff --encoding=utf8 --math-markup=coarse old/flat_old.tex new/flat_new.tex` 生成 `build_diff/diff.tex`；当宿主环境未安装 `latexdiff`（含 MiKTeX 未安装占位桩）或缺少 Perl 时，自动回退至内置纯 Python 词元级差异生成器（`builtin_tex_diff`），保证零依赖产出可编译的高亮差异稿。
   - 根据配置和源码自动确定编译引擎（稿件覆盖 -> 全局默认引擎 -> `% !TEX program` 魔法注释 -> 检测 `ctex`/`xeCJK`/`fontspec` 选用 `xelatex`，否则 `pdflatex`）与构建配方（`latexmk` 或 `<engine> + bibtex/biber` 多遍编译）。
   - 捕获编译输出与 `.log` 文件，提取 `-file-line-error`（`file.tex:行号: 错误信息`）及 `! ` 关键错误段，通过 SSE 推送至前端错误面板。

5. **Windows 子进程与单队列并发约束**：
   - 所有外部子进程（`git`、`latexdiff`、`latexmk`、`xelatex`、`pdflatex`、`lualatex`、`bibtex`、`biber`）必须传入 `stdin=subprocess.DEVNULL` 与 `creationflags=subprocess.CREATE_NO_WINDOW`，并设置明确超时，杜绝黑框闪烁和挂起。
   - 针对 MiKTeX 发行版默认注入 `--miktex-disable-installer`（除非用户在配置中显式开启 `miktex_auto_install`），彻底杜绝隐藏窗口下的交互式包安装弹窗阻塞。
   - `CompileCoordinator` 使用**单工作线程 + Latest-Wins 合并队列**：同一时刻最多运行一个编译任务，防止连续触发导致多份 LaTeX 进程竞争沙盒文件。

### 4.2 独立返修 TodoList 模块 (`todo_manager.py`)

每个活跃稿件目录下独立维护一份 `.rebuttal_todo.json`，完全脱离外部文档解析。

- **并发安全写入**：使用原子写入策略（先写入 `.rebuttal_todo.json.tmp`，再调用 `os.replace()` 覆盖原文件），并记录最近自写时间戳窗口（500ms），防止软件界面操作与本地 AI Agent 同时读写时产生文件损坏或触发 Watchdog 无效刷新。
- **状态流转与导出**：支持 `pending`（待修改）、`agent_done`（已改待核）、`verified`（已核验通过）三种状态，支持按审稿人（`Reviewer #1`、`Reviewer #2`）分组筛选、增删改操作，以及一键导出 Markdown / LaTeX 格式的点对点审稿回复信（Response Letter）。

### 4.3 可选自动编译与文件监听调度器 (`watcher.py`)

- **默认关闭自动编译**：`auto_compile` 默认为 `false`。打开工程、切换稿件、切换基准或外部 AI Agent 保存 `.tex` / `.bib` 文件时，**默认不会自动启动 LaTeX 编译**，确保软件轻量化且可控。
- **实时变更感知**：仅监听当前选中稿件目录下的 `.tex`、`.bib`、`.cls`、`.sty`、`.bst` 与 `.rebuttal_todo.json` 文件变更，严格排除 `.pdf`、`.aux`、`.log`、`.bbl`、`.blg`、`.fls`、`.fdb_latexmk` 及 `.tmp` 临时文件：
  - 当源码变更且 `auto_compile == false` 时，触发防抖后的 `FILES_CHANGED` 事件，界面状态条提示“源码已更新，点击立即编译”，由用户决定何时编译。
  - 当用户在界面开启 `auto_compile == true` 时，启用 **滑动窗口防抖（默认 800ms，可配置 `debounce_ms`）**，在 AI Agent 连续保存多个文件静默后再自动触发一次沙盒编译；若用户关闭开关，立即取消尚未触发的防抖定时器。
  - `.rebuttal_todo.json` 的外部变更始终即时监听（200ms 防抖），通过 SSE 推送 `TODO_UPDATED` 同步刷新看板。

### 4.4 可配置 LaTeX 编译器与工具链探测 (`config.py`, `proc_utils.py`)

为适配不同用户的 TeX 发行版（MiKTeX / TeX Live 多版本共存、便携版安装未写入系统 PATH）及特殊宏包（如 `minted` 需要 `-shell-escape`），软件提供完整的编译器配置能力：

- **可配置项（持久化于 `config.json`）**：
  - `auto_compile`（`bool`，默认 `false`）：是否开启保存自动编译。
  - `debounce_ms`（`int`，默认 `800`）：自动编译开启时的防抖延迟（毫秒）。
  - `tex_bin_dir`（`str`，默认 `""`）：自定义 TeX `bin` 目录（如 `E:\MiKTeX\miktex\bin\x64` 或 `C:\texlive\2025\bin\windows`）。非空时置于子进程 `PATH` 最前方。
  - `perl_path`（`str`，默认 `""`）：自定义 `perl.exe` 路径或所在目录（用于 `latexdiff` / `latexmk`）。留空时优先使用系统 `PATH`，若缺失则自动探测 Git 自带的 `<GitRoot>\usr\bin\perl.exe` 并**追加到 `PATH` 末尾**（避免遮蔽 Windows 系统命令）。
  - `default_engine`（`str`，默认 `"auto"`）：全局默认引擎，可选 `auto` | `xelatex` | `pdflatex` | `lualatex`（同时支持在 `.rebuttal_todo.json` 中为单篇稿件覆盖引擎）。
  - `recipe`（`str`，默认 `"auto"`）：编译配方，可选：
    - `auto`：优先调用 `latexmk`，若未安装则自动回退为 `<engine> + bibtex/biber` 多遍编译；
    - `latexmk`：强制使用 `latexmk`；
    - `engine_bibtex`：`<engine>` -> `bibtex` -> `<engine>` x2；
    - `engine_biber`：`<engine>` -> `biber` -> `<engine>` x2；
    - `engine_only`：仅执行单次/两次 `<engine>` 快速编译（跳过参考文献重编以提速）。
  - `extra_args`（`str`，默认 `""`）：附加编译参数（如 `-shell-escape`、`--synctex=1`）。
  - `compile_timeout`（`int`，默认 `180`）：单次编译超时秒数。
  - `miktex_auto_install`（`bool`，默认 `false`）：是否允许 MiKTeX 在编译时自动静默安装缺失宏包（默认关闭 `--miktex-disable-installer` 防止无界面阻塞）。
- **工具链健康检测**：提供 `GET /api/settings/compiler/check`，即时返回 `xelatex`、`pdflatex`、`lualatex`、`latexmk`、`latexdiff`、`bibtex`、`biber`、`perl`、`git` 的解析路径与可用状态。

## 5. 数据结构与接口定义

### 5.1 `.rebuttal_todo.json` 数据规范

```json
{
  "manuscript": "eswa_mainline",
  "active_baseline": {
    "type": "git",
    "ref": "paper/eswa/v1-sub"
  },
  "engine": "auto",
  "items": [
    {
      "id": "R1-C1",
      "reviewer": "Reviewer #1",
      "summary": "补充与 2025 年最新基线方法的消融实验对比",
      "target_location": "Section 4.3, Table 3",
      "status": "agent_done",
      "response_draft": "We thank the reviewer for this constructive suggestion. We have added...",
      "updated_at": "2026-11-25T21:15:00Z"
    }
  ]
}
```

> 说明：`active_baseline` 可为 `null`（表示仅编译 Clean 净版，不执行 Diff 对比）；`engine` 可为 `"auto"`、`"xelatex"`、`"pdflatex"` 或 `"lualatex"`。

### 5.2 应用配置文件规范 (`%APPDATA%/RebuttalSidecar/config.json`)

```json
{
  "last_workspace": "E:/Research/MyPaperRepo",
  "recent_workspaces": ["E:/Research/MyPaperRepo"],
  "compiler": {
    "auto_compile": false,
    "debounce_ms": 800,
    "tex_bin_dir": "",
    "perl_path": "",
    "default_engine": "auto",
    "recipe": "auto",
    "extra_args": "",
    "compile_timeout": 180
  }
}
```

### 5.3 本地多线程 HTTP / SSE 接口清单

后端采用 **Bottle + `ThreadingWSGIServer`（`socketserver.ThreadingMixIn` + `daemon_threads = True`）**，确保 `/api/events` 长连接不阻塞其余并发 API 与 PDF 流读取；静态资源显式映射 `.js`/`.mjs` 为 `application/javascript; charset=utf-8`，绕开部分 Windows 注册表将 `.js` 识别为 `text/plain` 导致 ES Module 拒绝加载的缺陷。

| **方法** | **路径**                                 | **功能说明**                                                 |
| -------- | ---------------------------------------- | ------------------------------------------------------------ |
| `GET`    | `/api/workspace`                         | 返回当前工作区、最近工程列表、活跃稿件及 `_archive` 基准     |
| `POST`   | `/api/workspace/open`                    | 切换工作区路径，重绑 Watchdog 监听并持久化                   |
| `POST`   | `/api/workspace/select`                  | 拉起系统原生文件夹选择框并切换工作区                         |
| `GET`    | `/api/baselines?manuscript=<name>`       | 查询指定稿件的 Git Tag / Commit 历史与本地归档基准           |
| `GET`    | `/api/todos?manuscript=<name>`           | 读取 `.rebuttal_todo.json` 并切换监听上下文                  |
| `POST`   | `/api/todos?manuscript=<name>`           | 新增审稿意见条目或整体更新                                   |
| `PATCH`  | `/api/todos/<item_id>?manuscript=<name>` | 原子更新指定条目的状态、位置、摘要或回复草稿                 |
| `DELETE` | `/api/todos/<item_id>?manuscript=<name>` | 删除指定审稿意见条目                                         |
| `PUT`    | `/api/baseline?manuscript=<name>`        | 持久化稿件的 `active_baseline` 与 `engine` 选择              |
| `GET`    | `/api/todos/export?manuscript=<name>&format=md\|tex` | 导出按审稿人分组的 Response Letter 草稿          |
| `GET`    | `/api/settings`                          | 读取当前自动编译开关与 LaTeX 编译器配置                      |
| `PUT`    | `/api/settings`                          | 保存配置并实时更新 Watcher 自动编译状态与防抖时间            |
| `GET`    | `/api/settings/compiler/check`           | 探测各 TeX 编译器、Perl 与 Git 可执行路径及可用状态          |
| `POST`   | `/api/settings/select-dir`               | 拉起原生文件夹选择器，用于选择 `tex_bin_dir` 或 `perl_path`  |
| `POST`   | `/api/compile`                           | 手动触发一次沙盒编译（支持传入 `manuscript`、`baseline`、`engine`） |
| `GET`    | `/api/compile/status?manuscript=<name>`  | 查询当前编译状态、PDF 可用性、结构化错误及日志尾部           |
| `GET`    | `/api/pdf/<kind>?manuscript=<name>`      | 流式返回 `clean.pdf` 或 `diff.pdf`（响应头 `Cache-Control: no-store`） |
| `GET`    | `/api/events`                            | SSE 事件流：`WORKSPACE_UPDATED`、`TODO_UPDATED`、`FILES_CHANGED`、`SETTINGS_UPDATED`、`COMPILE_START`、`COMPILE_SUCCESS`、`COMPILE_ERROR` |

## 6. Nuitka 转译 C 语言与原生打包方案

为满足“轻量化、启动快、转译为 C 语言打包”的要求，严禁使用 PyInstaller。采用 **Nuitka Standalone + Onefile** 编译管线，并将前端依赖（`web/vendor/alpine.min.js`、`web/pdfjs/pdf.min.mjs`、`web/pdfjs/pdf.worker.min.mjs`）离线内嵌至 `web/` 目录。

### 6.1 工程目录布局

```text
rebuttal-sidecar/
├── src/
│   ├── __init__.py
│   ├── main.py             # 程序入口：解析参数、启动多线程本地服务与 PyWebView
│   ├── config.py           # %APPDATA%/RebuttalSidecar/config.json 原子读写与默认配置
│   ├── proc_utils.py       # 无黑框子进程封装、Git Perl / 自定义 TeX PATH 装配与探测
│   ├── scanner.py          # 工作区解析、稿件扫描与 .gitignore / Git 基准查询
│   ├── diff_engine.py      # 源码展平、Git/归档提取、latexdiff 与可配置沙盒编译队列
│   ├── todo_manager.py     # .rebuttal_todo.json 原子 CRUD 与回复信导出
│   ├── watcher.py          # Watchdog 监听（可选自动编译防抖 + Todo 实时同步）
│   └── server.py           # Bottle + ThreadingWSGIServer 路由与 SSE 推送中心
├── web/                    # 100% 离线前端静态资源
│   ├── index.html
│   ├── styles.css
│   ├── app.js
│   ├── vendor/alpine.min.js
│   └── pdfjs/
│       ├── pdf.min.mjs
│       └── pdf.worker.min.mjs
├── tests/                  # pytest 自动化测试套件
├── pyproject.toml
├── README.md
└── build_nuitka.ps1        # Windows 一键 C 转译编译脚本（自动适配 MSVC / MinGW64）
```

### 6.2 关键构建配置 (`build_nuitka.ps1`)

脚本自动检测 `vswhere.exe` / `cl.exe`：若存在 Visual Studio Build Tools 则使用 `--msvc=latest`，否则自动切换 `--mingw64 --assume-yes-for-downloads`。同时支持可选 `-RegisterContextMenu` 参数，免管理员权限向当前用户注册表 `HKCU\Software\Classes\Directory\shell\RebuttalSidecar` 写入“用 Rebuttal Sidecar 打开”右键菜单。

```powershell
python -m nuitka `
    --standalone `
    --onefile `
    $compilerFlag `
    --windows-console-mode=disable `
    --enable-plugin=anti-bloat `
    --include-package-data=webview `
    --include-data-dir=web=web `
    --nofollow-import-to=pytest,unittest,tkinter,PIL,numpy `
    --output-dir=dist `
    --output-filename=RebuttalSidecar.exe `
    src/main.py
```

## 7. 开发实施路线图

1. **阶段一：项目骨架与核心扫描/Todo 模块**：完成 `.venv` 初始化、`config.py`、`proc_utils.py`、`scanner.py` 与 `todo_manager.py`，通过单元测试验证过滤规则与原子读写。
2. **阶段二：沙盒展平与可配置编译管线**：完成 `diff_engine.py`，结合本地 MiKTeX 与 Git Perl 验证 `../references.bib`、多层 `\input` 展平、自定义编译器参数、`Clean` 与 `latexdiff` `Diff` 全链路。
3. **阶段三：按需监听与多线程 SSE 服务**：完成 `watcher.py` 与 `server.py`，验证“默认关闭自动编译 + 可开关防抖 + 编译器配置 API + SSE 非阻塞推送”。
4. **阶段四：双栏前端、PyWebView 集成与打包脚本**：完成离线 Alpine.js + PDF.js 双栏界面（含页码/滚动位置记忆、自动编译开关与编译器设置弹窗）、`main.py` 桌面入口及 `build_nuitka.ps1`。

## 8. 自动化测试与验收标准

使用 `.venv\Scripts\python.exe -m pytest -v` 覆盖以下核心验收点：

- `tests/test_scanner.py`：验证 `_`/`.'` 前缀过滤、`.gitignore` 屏蔽、入口 `.tex` 识别及 Git Tag/Commit 基线查询。
- `tests/test_todo_manager.py`：验证原子写入无 `.tmp` 残留、状态流转、局部更新及 Markdown/LaTeX 回复信导出。
- `tests/test_diff_engine.py`：验证递归展平、引擎/配方选择与额外参数解析，并通过真实 MiKTeX 完成含 `../references.bib` 的 `Clean + Diff` 双 PDF 沙盒编译（确保宿主稿件目录零污染）。
- `tests/test_server_and_watcher.py`：验证 `auto_compile=False` 时修改 `.tex` 仅发送 `FILES_CHANGED` 而不触发编译；开启 `auto_compile=True` 后 800ms 滑动窗口防抖合并多次保存为一次编译；验证 SSE 长连接不阻塞并发 HTTP 接口与 `/api/settings` 持久化。
