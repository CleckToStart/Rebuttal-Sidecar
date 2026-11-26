# Rebuttal Sidecar

轻量级本地 LaTeX 论文返修伴侣：提供只读历史基准提取（Git / `_archive`）、沙盒化 `latexdiff` 编译对比、审稿意见 TodoList 看板与可选自动编译。

## 核心特性

- **零工作区污染**：所有 `latexdiff` 与 LaTeX 编译产物全部隔离于 `%TEMP%/rebuttal_sidecar/`，不向稿件目录写入 `.aux`/`.log`/`diff.tex`。
- **默认关闭自动编译**：启动或文件修改时默认仅提示“源码已变更”，不会后台抢占 CPU；可随时一键开启 800ms 防抖自动编译。
- **可配置 LaTeX 编译器**：支持自定义 TeX `bin` 目录、Perl 路径、全局/单稿件编译引擎（`auto` / `xelatex` / `pdflatex` / `lualatex`）、构建配方（`auto` / `latexmk` / `bibtex` / `biber` / `engine_only`）及附加参数（如 `-shell-escape`），并内置工具链检测。
- **独立审稿进度看板**：在各稿件目录维护 `.rebuttal_todo.json`，原子写入防并发损坏，支持一键导出 Markdown / LaTeX 点对点回复信。

## 开发环境初始化 (`.venv`)

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## 运行与测试

```powershell
# 启动桌面端（可选传入目标科研工程目录）
.venv\Scripts\python.exe src\main.py "E:\path\to\your\paper_repo"

# 仅启动本地 HTTP 服务（无桌面窗口，适合调试）
.venv\Scripts\python.exe src\main.py --headless --port 8765

# 运行自动化测试
.venv\Scripts\python.exe -m pytest -v
```

## 打包单文件 `.exe` (Nuitka)

```powershell
.venv\Scripts\python.exe -m pip install -e ".[build]"
.\build_nuitka.ps1
```
