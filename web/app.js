import Alpine from "/vendor/alpine.esm.min.js";

let _pdfjsPromise = null;
async function getPdfJs() {
  if (!_pdfjsPromise) {
    _pdfjsPromise = import("/pdfjs/pdf.min.mjs").then((lib) => {
      lib.GlobalWorkerOptions.workerSrc = "/pdfjs/pdf.worker.min.mjs";
      return lib;
    });
  }
  return _pdfjsPromise;
}

const STATUS_CYCLE = ["pending", "agent_done", "verified"];
const COMPANION_DEFAULTS = {
  title_page: "title_page.tex",
  highlights: "highlights.tex",
  cover_letter: "cover_letter.tex",
  declaration: "declaration.tex",
};

async function apiJson(url, options = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

function readBoolPref(key, fallback) {
  try {
    const val = localStorage.getItem(key);
    return val === null ? fallback : val === "1";
  } catch {
    return fallback;
  }
}

function writeBoolPref(key, value) {
  try {
    localStorage.setItem(key, value ? "1" : "0");
  } catch {
    // Ignore storage errors in restricted contexts.
  }
}

function sidecarApp() {
  return {
    workspace: {
      project_root: "",
      manuscripts_dir: "",
      manuscripts: [],
      archive_baselines: [],
      recent_workspaces: [],
      workspace_history: [],
    },
    activeManuscript: "",
    activeDocTarget: { type: "main", name: "", role: "Main", compilable: true, kind: "tex" },
    companionViewMode: "pdf",
    companionData: { name: "", content: "", bullets: [] },

    baselines: { archive: [], git: [], meta: {} },
    selectedBaselineKey: "",
    _lastValidBaselineKey: "",
    includeAllGitCommits: false,
    showCustomRefModal: false,
    customGitRef: "",
    isVerifyingRef: false,
    manuscriptEngine: "auto",

    todos: [],
    reviewerFilter: "ALL",
    statusFilter: "ALL",

    settings: {
      auto_compile: false,
      debounce_ms: 800,
      tex_bin_dir: "",
      perl_path: "",
      default_engine: "auto",
      recipe: "auto",
      extra_args: "",
      compile_timeout: 180,
      miktex_auto_install: false,
    },
    compilerForm: {},
    toolchain: {},
    compilerTab: "config",
    discoveredTexDirs: [],
    texMirrors: {},
    texLiveForm: { mirror: "tuna", mode: "gui" },
    texInstallStatus: { state: "idle", action: "", progress: "", logs: [], detected_bin_dir: "", message: "" },
    isDesktopShell: false,
    isWindowMaximized: false,

    compileStatus: {
      state: "idle",
      dirty: false,
      has_clean: false,
      has_diff: false,
      companion_pdfs: {},
      compiling_target: "",
      errors: [],
      log_tail: "",
      message: "",
    },
    isTriggeringCompile: false,

    sidebarExpanded: readBoolPref("rs_sidebar_expanded", true),
    todoCollapsed: readBoolPref("rs_todo_collapsed", false),
    historySearch: "",

    viewMode: "diff",
    currentPage: 1,
    totalPages: 0,
    zoomScale: 1.15,
    _renderToken: 0,
    _sse: null,

    showCompilerModal: false,
    showWorkspaceModal: false,
    workspaceInputPath: "",
    workspaceCustomName: "",
    showRenameModal: false,
    renameTarget: { path: "", name: "" },
    showCompanionModal: false,
    companionForm: { template: "title_page", filename: "title_page.tex" },
    confirmModal: { open: false, title: "", message: "", confirmText: "确认", onConfirm: null },

    showTodoModal: false,
    showAdvancedTodoFields: false,
    todoForm: {
      isEdit: false,
      id: "",
      reviewer: "Reviewer #1",
      summary: "",
      target_location: "",
      status: "pending",
      response_draft: "",
    },
    showExportModal: false,
    exportFormat: "md",
    exportContent: "",
    copyFeedback: "",
    showHelpModal: false,
    helpTab: "init",
    showAiTodoPopover: false,
    _popoverTimer: null,
    aiCopyTag: "",
    showErrorDrawer: false,
    toastMessage: "",

    async init() {
      await Promise.all([this.loadSettings(), this.loadWorkspace()]);
      await this.syncDesktopShellState();
      this.connectSSE();
    },

    async syncDesktopShellState() {
      if (this.isDesktopShell && window.pywebview?.api?.get_state) {
        try {
          const st = await window.pywebview.api.get_state();
          this.isWindowMaximized = Boolean(st?.maximized);
        } catch {
          // Ignore before bridge initialization completes.
        }
      }
    },

    async minimizeWindow() {
      await window.pywebview?.api?.minimize?.();
    },

    async toggleMaximizeWindow() {
      const res = await window.pywebview?.api?.toggle_maximize?.();
      if (res && typeof res.maximized === "boolean") {
        this.isWindowMaximized = res.maximized;
      }
    },

    async closeWindow() {
      await window.pywebview?.api?.close?.();
    },

    async startWindowResize(edge) {
      const api = window.pywebview?.api;
      if (!api?.begin_resize) return;
      const ok = await api.begin_resize(edge);
      if (!ok) return;
      let pending = false;
      const onMove = () => {
        if (pending) return;
        pending = true;
        requestAnimationFrame(async () => {
          await api.step_resize();
          pending = false;
        });
      };
      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        api.end_resize();
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },

    showToast(msg) {
      this.toastMessage = msg;
      setTimeout(() => {
        if (this.toastMessage === msg) this.toastMessage = "";
      }, 2800);
    },

    toggleSidebar(next) {
      this.sidebarExpanded = typeof next === "boolean" ? next : !this.sidebarExpanded;
      writeBoolPref("rs_sidebar_expanded", this.sidebarExpanded);
    },

    toggleTodoCollapse() {
      this.todoCollapsed = !this.todoCollapsed;
      writeBoolPref("rs_todo_collapsed", this.todoCollapsed);
    },

    isCompiling() {
      return this.isTriggeringCompile || this.compileStatus.state === "compiling";
    },

    canCompileCurrentTarget() {
      if (!this.activeManuscript) return false;
      if (this.activeDocTarget.type === "companion") return Boolean(this.activeDocTarget.compilable);
      return true;
    },

    connectSSE() {
      if (this._sse) this._sse.close();
      const es = new EventSource("/api/events");
      es.addEventListener("WORKSPACE_UPDATED", async (e) => {
        this.applyWorkspace(JSON.parse(e.data));
        if (this.activeManuscript && this.activeDocTarget.type === "main") {
          await this.loadManuscriptState(this.activeManuscript);
        }
      });
      es.addEventListener("TODO_UPDATED", (e) => {
        const doc = JSON.parse(e.data);
        if (doc.manuscript === this.activeManuscript) {
          this.applyTodoDoc(doc);
        }
      });
      es.addEventListener("SETTINGS_UPDATED", (e) => {
        const data = JSON.parse(e.data);
        this.settings = data.compiler;
        if (this.showCompilerModal && data.compiler?.tex_bin_dir && !this.compilerForm.tex_bin_dir) {
          this.compilerForm.tex_bin_dir = data.compiler.tex_bin_dir;
        }
      });
      es.addEventListener("TEX_INSTALL_STATUS", (e) => {
        const st = JSON.parse(e.data);
        this.texInstallStatus = st;
        if (st.state === "success" && st.detected_bin_dir) {
          this.compilerForm.tex_bin_dir = st.detected_bin_dir;
          this.probeToolchain();
        }
      });
      es.addEventListener("FILES_CHANGED", (e) => {
        const info = JSON.parse(e.data);
        if (info.manuscript === this.activeManuscript) {
          this.compileStatus.dirty = true;
          this.compileStatus.message = `已修改: ${info.file}`;
        }
      });
      es.addEventListener("COMPILE_START", (e) => {
        const info = JSON.parse(e.data);
        if (info.manuscript === this.activeManuscript) {
          this.compileStatus.state = "compiling";
          this.compileStatus.errors = [];
          this.showErrorDrawer = false;
        }
      });
      es.addEventListener("COMPILE_SUCCESS", async (e) => {
        const st = JSON.parse(e.data);
        if (st.manuscript === this.activeManuscript) {
          this.isTriggeringCompile = false;
          this.compileStatus = st;
          this.showErrorDrawer = false;
          if (this.activeDocTarget.type === "main") {
            if (!st.has_diff && this.viewMode === "diff") this.viewMode = "clean";
            await this.renderCurrentView(true);
          } else if (this.companionViewMode === "pdf") {
            await this.renderCompanionPdf(true);
          }
          this.showToast("编译完成");
        }
      });
      es.addEventListener("COMPILE_ERROR", (e) => {
        const st = JSON.parse(e.data);
        if (st.manuscript === this.activeManuscript) {
          this.isTriggeringCompile = false;
          this.compileStatus = st;
          this.showErrorDrawer = true;
        }
      });
      this._sse = es;
    },

    workspaceHistory() {
      if (Array.isArray(this.workspace.workspace_history) && this.workspace.workspace_history.length > 0) {
        return this.workspace.workspace_history;
      }
      return (this.workspace.recent_workspaces || []).map((p) => ({
        path: p,
        name: p.replace(/[/\\]+$/, "").split(/[/\\]/).pop() || p,
        last_manuscript: "",
        manuscript_count: 0,
        pinned: false,
      }));
    },

    filteredHistory() {
      const q = this.historySearch.trim().toLowerCase();
      const list = this.workspaceHistory();
      if (!q) return list;
      return list.filter((i) => i.name.toLowerCase().includes(q) || i.path.toLowerCase().includes(q));
    },

    isCurrentWorkspace(path) {
      return Boolean(this.workspace.project_root) && this.workspace.project_root.toLowerCase() === String(path).toLowerCase();
    },

    historyMeta(item) {
      if (item.last_manuscript) return `上次: ${item.last_manuscript}`;
      if (item.manuscript_count > 0) return `${item.manuscript_count} 篇稿件`;
      return item.path;
    },

    currentCompanions() {
      const m = (this.workspace.manuscripts || []).find((x) => x.name === this.activeManuscript);
      return m?.companions || [];
    },

    async togglePinWorkspace(item) {
      const res = await apiJson("/api/workspace/history", {
        method: "PATCH",
        body: JSON.stringify({ path: item.path, pinned: !item.pinned }),
      });
      this.workspace.workspace_history = res.workspace_history || [];
    },

    openRenameModal(item) {
      this.renameTarget = { path: item.path, name: item.name };
      this.showRenameModal = true;
    },

    async confirmRenameWorkspace() {
      if (!this.renameTarget.name.trim()) return;
      const res = await apiJson("/api/workspace/history", {
        method: "PATCH",
        body: JSON.stringify({ path: this.renameTarget.path, name: this.renameTarget.name.trim() }),
      });
      this.workspace.workspace_history = res.workspace_history || [];
      this.showRenameModal = false;
      this.showToast("已更新项目名称");
    },

    askConfirm({ title, message, confirmText = "确认", onConfirm }) {
      this.confirmModal = { open: true, title, message, confirmText, onConfirm };
    },

    async runConfirmAction() {
      const fn = this.confirmModal.onConfirm;
      this.confirmModal.open = false;
      if (fn) await fn();
    },

    requestRemoveWorkspace(item) {
      this.askConfirm({
        title: "移除历史项目",
        message: `确认将「${item.name}」从历史工程列表移除？仅移除记录，不会删除磁盘上的任何论文文件。`,
        confirmText: "确认移除",
        onConfirm: async () => {
          const res = await apiJson("/api/workspace/history", {
            method: "DELETE",
            body: JSON.stringify({ path: item.path }),
          });
          this.workspace.workspace_history = res.workspace_history || [];
          if (this.isCurrentWorkspace(item.path)) {
            if (this.workspace.workspace_history.length > 0) {
              await this.openWorkspacePath(this.workspace.workspace_history[0].path);
            } else {
              this.workspace = { ...this.workspace, project_root: "", manuscripts: [], active_manuscript: "" };
              this.activeManuscript = "";
            }
          }
          this.showToast("已从历史记录移除");
        },
      });
    },

    async loadWorkspace() {
      const ws = await apiJson("/api/workspace");
      this.applyWorkspace(ws);
      if (this.activeManuscript) {
        await this.loadManuscriptState(this.activeManuscript);
      }
    },

    applyWorkspace(ws) {
      this.workspace = ws;
      this.activeManuscript = ws.active_manuscript || (ws.manuscripts[0]?.name ?? "");
      const m = (ws.manuscripts || []).find((x) => x.name === this.activeManuscript);
      this.activeDocTarget = {
        type: "main",
        name: m?.entry_tex || "main.tex",
        role: "Main",
        compilable: true,
        kind: "tex",
      };
    },

    async selectMainManuscript(name) {
      this.activeManuscript = name;
      const m = (this.workspace.manuscripts || []).find((x) => x.name === name);
      this.activeDocTarget = {
        type: "main",
        name: m?.entry_tex || "main.tex",
        role: "Main",
        compilable: true,
        kind: "tex",
      };
      await this.loadManuscriptState(name);
    },

    async loadManuscriptState(name) {
      const allFlag = this.includeAllGitCommits ? "&all_commits=1" : "";
      const [baselines, todoDoc, status] = await Promise.all([
        apiJson(`/api/baselines?manuscript=${encodeURIComponent(name)}${allFlag}`),
        apiJson(`/api/todos?manuscript=${encodeURIComponent(name)}`),
        apiJson(`/api/compile/status?manuscript=${encodeURIComponent(name)}`),
      ]);
      this.baselines = baselines;
      this.applyTodoDoc(todoDoc);
      this.ensureSelectedBaselineOption();
      this._lastValidBaselineKey = this.selectedBaselineKey;
      this.compileStatus = status;
      this.showErrorDrawer = status.state === "error";
      // Automatically display cached Clean PDF when Diff PDF does not exist yet
      if (this.activeDocTarget.type === "main") {
        if (this.viewMode === "diff" && !status.has_diff && status.has_clean) {
          this.viewMode = "clean";
        }
        await this.renderCurrentView(false);
      }
    },

    ensureSelectedBaselineOption() {
      if (!this.selectedBaselineKey.startsWith("git:")) return;
      const ref = this.selectedBaselineKey.slice(4);
      const exists = (this.baselines.git || []).some((b) => b.ref === ref);
      if (!exists && ref) {
        this.baselines.git = [{ type: "git", ref, kind: "custom", label: `指定基准: ${ref}` }, ...(this.baselines.git || [])];
      }
    },

    baselineGitGroupLabel() {
      if (this.includeAllGitCommits) return "Git 全仓库历史 (Tag / Commit)";
      if (this.baselines.meta?.relocated) return "Git 历史 (含目录迁移前记录)";
      return "Git 历史 (Tag / Commit)";
    },

    applyTodoDoc(doc) {
      this.todos = doc.items || [];
      this.manuscriptEngine = doc.engine || "auto";
      const bl = doc.active_baseline;
      this.selectedBaselineKey = bl && bl.type && bl.ref ? `${bl.type}:${bl.ref}` : "";
    },

    openImportModal() {
      this.workspaceInputPath = "";
      this.workspaceCustomName = "";
      this.showWorkspaceModal = true;
    },

    syncSuggestedName() {
      const raw = this.workspaceInputPath.trim().replace(/[/\\]+$/, "");
      if (!raw) return;
      if (!this.workspaceCustomName.trim()) {
        this.workspaceCustomName = raw.split(/[/\\]/).pop() || raw;
      }
    },

    async pickImportFolder() {
      try {
        const res = await apiJson("/api/workspace/pick-path", { method: "POST" });
        if (!res.cancelled && res.path) {
          this.workspaceInputPath = res.path;
          this.workspaceCustomName = res.suggested_name || "";
        }
      } catch (err) {
        this.showToast(err.message);
      }
    },

    async confirmImportWorkspace() {
      await this.openWorkspacePath(this.workspaceInputPath, this.workspaceCustomName);
    },

    async openWorkspacePath(path, customName = "") {
      if (!path || !path.trim()) return;
      try {
        const res = await apiJson("/api/workspace/open", {
          method: "POST",
          body: JSON.stringify({ path: path.trim(), name: customName.trim() }),
        });
        this.showWorkspaceModal = false;
        this.applyWorkspace(res);
        if (this.activeManuscript) await this.loadManuscriptState(this.activeManuscript);
      } catch (err) {
        this.showToast(`打开失败: ${err.message}`);
      }
    },

    async openDemoWorkspace() {
      try {
        const res = await apiJson("/api/workspace/demo", { method: "POST" });
        this.showWorkspaceModal = false;
        this.applyWorkspace(res);
        if (this.activeManuscript) await this.loadManuscriptState(this.activeManuscript);
        this.showToast("已加载示例返修工程");
      } catch (err) {
        this.showToast(`加载示例失败: ${err.message}`);
      }
    },

    openCompanionModal() {
      this.companionForm = { template: "title_page", filename: "title_page.tex" };
      this.showCompanionModal = true;
    },

    onCompanionTemplateChange() {
      this.companionForm.filename = COMPANION_DEFAULTS[this.companionForm.template] || "companion.tex";
    },

    async createCompanion() {
      if (!this.activeManuscript) return;
      try {
        const res = await apiJson(`/api/companions?manuscript=${encodeURIComponent(this.activeManuscript)}`, {
          method: "POST",
          body: JSON.stringify(this.companionForm),
        });
        this.workspace = res.workspace;
        this.showCompanionModal = false;
        this.showToast(`已创建 ${res.created.name}`);
        await this.selectCompanion(res.created);
      } catch (err) {
        this.showToast(`创建失败: ${err.message}`);
      }
    },

    async selectCompanion(comp) {
      this.activeDocTarget = { type: "companion", ...comp };
      this.companionViewMode = comp.compilable ? "pdf" : "source";
      await this.loadCompanionContent(comp.name);
      if (comp.compilable) {
        await this.renderCompanionPdf(false);
      }
    },

    async loadCompanionContent(filename) {
      const data = await apiJson(
        `/api/companions/content?manuscript=${encodeURIComponent(this.activeManuscript)}&file=${encodeURIComponent(filename)}`,
      );
      this.companionData = data;
    },

    async switchCompanionViewMode(mode) {
      this.companionViewMode = mode;
      if (mode === "source") {
        await this.loadCompanionContent(this.activeDocTarget.name);
      } else {
        await this.renderCompanionPdf(true);
      }
    },

    hasCompanionPdf() {
      const map = this.compileStatus.companion_pdfs || {};
      return Boolean(map[this.activeDocTarget.name]);
    },

    async renderCompanionPdf(preserveScroll) {
      const container = document.getElementById("companion-pdf-container");
      if (!container || !this.hasCompanionPdf()) {
        if (container) container.innerHTML = "";
        this.totalPages = 0;
        return;
      }
      const token = ++this._renderToken;
      const ms = encodeURIComponent(this.activeManuscript);
      const file = encodeURIComponent(this.activeDocTarget.name);
      await this.renderPdfInto(`/api/pdf/companion?manuscript=${ms}&file=${file}&t=${Date.now()}`, container, token, true);
    },

    viewerTitleText() {
      if (this.activeDocTarget.type === "companion") {
        return `${this.activeDocTarget.role} (${this.activeDocTarget.name})`;
      }
      return { diff: "修订对比视图 (Diff)", clean: "净版预览 (Clean)", split: "双栏同步对照" }[this.viewMode] || "PDF 预览";
    },

    viewerSubText() {
      if (this.activeDocTarget.type === "companion") {
        return `${this.activeManuscript} · 附属文档`;
      }
      return this.activeManuscript + (this.selectedBaselineKey ? ` · 基准: ${this.selectedBaselineKey}` : " · 无对比基准");
    },

    parseBaselineKey() {
      if (!this.selectedBaselineKey) return null;
      const idx = this.selectedBaselineKey.indexOf(":");
      return {
        type: this.selectedBaselineKey.slice(0, idx),
        ref: this.selectedBaselineKey.slice(idx + 1),
      };
    },

    async onBaselineChanged() {
      if (!this.activeManuscript) return;
      if (this.selectedBaselineKey === "__action:toggle_all__") {
        this.selectedBaselineKey = this._lastValidBaselineKey;
        await this.toggleAllGitCommits();
        return;
      }
      if (this.selectedBaselineKey === "__action:custom_ref__") {
        this.selectedBaselineKey = this._lastValidBaselineKey;
        this.customGitRef = "";
        this.showCustomRefModal = true;
        return;
      }
      this._lastValidBaselineKey = this.selectedBaselineKey;
      await apiJson(`/api/baseline?manuscript=${encodeURIComponent(this.activeManuscript)}`, {
        method: "PUT",
        body: JSON.stringify({ active_baseline: this.parseBaselineKey(), engine: this.manuscriptEngine }),
      });
    },

    async toggleAllGitCommits() {
      if (!this.activeManuscript) return;
      this.includeAllGitCommits = !this.includeAllGitCommits;
      const allFlag = this.includeAllGitCommits ? "&all_commits=1" : "";
      const res = await apiJson(`/api/baselines?manuscript=${encodeURIComponent(this.activeManuscript)}${allFlag}`);
      this.baselines = res;
      this.ensureSelectedBaselineOption();
      const count = (res.git || []).length;
      this.showToast(this.includeAllGitCommits ? `已加载全仓库提交 (${count} 项)` : `已切换为本稿件相关提交 (${count} 项)`);
    },

    async applyCustomGitRef() {
      const ref = this.customGitRef.trim();
      if (!this.activeManuscript || !ref || this.isVerifyingRef) return;
      this.isVerifyingRef = true;
      try {
        const res = await apiJson(
          `/api/baselines/verify?manuscript=${encodeURIComponent(this.activeManuscript)}&ref=${encodeURIComponent(ref)}`,
        );
        const item = res.baseline;
        const list = this.baselines.git || [];
        if (!list.some((b) => b.ref === item.ref)) {
          this.baselines.git = [item, ...list];
        }
        const nextKey = `git:${item.ref}`;
        this.showCustomRefModal = false;
        this._lastValidBaselineKey = nextKey;
        this.$nextTick(async () => {
          this.selectedBaselineKey = nextKey;
          await apiJson(`/api/baseline?manuscript=${encodeURIComponent(this.activeManuscript)}`, {
            method: "PUT",
            body: JSON.stringify({ active_baseline: { type: "git", ref: item.ref }, engine: this.manuscriptEngine }),
          });
        });
        this.showToast(`已设置对比基准: ${item.ref}`);
      } catch (err) {
        this.showToast(`验证失败: ${err.message}`);
      } finally {
        this.isVerifyingRef = false;
      }
    },

    async onEngineChanged() {
      if (!this.activeManuscript) return;
      await apiJson(`/api/baseline?manuscript=${encodeURIComponent(this.activeManuscript)}`, {
        method: "PUT",
        body: JSON.stringify({ engine: this.manuscriptEngine }),
      });
    },

    async loadSettings() {
      const data = await apiJson("/api/settings");
      this.settings = data.compiler;
      this.isDesktopShell = Boolean(data.desktop_shell);
    },

    async toggleAutoCompile(enabled) {
      const res = await apiJson("/api/settings", {
        method: "PUT",
        body: JSON.stringify({ auto_compile: enabled }),
      });
      this.settings = res.compiler;
      this.showToast(enabled ? "已开启自动编译" : "已关闭自动编译");
    },

    async openCompilerModal(tab = "config") {
      this.compilerForm = { ...this.settings };
      this.compilerTab = tab;
      this.showCompilerModal = true;
      await Promise.all([this.probeToolchain(), this.scanLocalTex(false)]);
    },

    async openTexInstallTab() {
      this.compilerTab = "install";
      await this.scanLocalTex(false);
      const st = await apiJson("/api/settings/compiler/install/status");
      this.texInstallStatus = st;
    },

    async scanLocalTex(notify = false) {
      const res = await apiJson("/api/settings/compiler/scan");
      this.discoveredTexDirs = res.installations || [];
      this.texMirrors = res.mirrors || {};
      if (notify) {
        if (this.discoveredTexDirs.length > 0 && !this.compilerForm.tex_bin_dir) {
          this.compilerForm.tex_bin_dir = this.discoveredTexDirs[0].bin_dir;
          await this.probeToolchain();
        }
        this.showToast(
          this.discoveredTexDirs.length > 0
            ? `发现 ${this.discoveredTexDirs.length} 个本机 TeX 发行版`
            : "未在本机常见目录发现 MiKTeX / TeX Live，可点击安装标签页一键部署",
        );
      }
    },

    async selectDiscoveredTex(inst) {
      this.compilerForm.tex_bin_dir = inst.bin_dir;
      await this.probeToolchain();
      this.showToast(`已填入 ${inst.distro} 路径`);
    },

    async runTexInstall(action, options = {}) {
      try {
        const res = await apiJson("/api/settings/compiler/install", {
          method: "POST",
          body: JSON.stringify({ action, options }),
        });
        this.texInstallStatus = res;
      } catch (err) {
        this.showToast(err.message);
      }
    },

    async cancelTexInstall() {
      const res = await apiJson("/api/settings/compiler/install/cancel", { method: "POST" });
      this.texInstallStatus = res;
    },

    async probeToolchain() {
      const q = new URLSearchParams({
        tex_bin_dir: this.compilerForm.tex_bin_dir || "",
        perl_path: this.compilerForm.perl_path || "",
      });
      const res = await apiJson(`/api/settings/compiler/check?${q.toString()}`);
      this.toolchain = res.tools || {};
    },

    async browseCompilerDir(field) {
      try {
        const res = await apiJson("/api/settings/select-dir", { method: "POST" });
        if (res.path) {
          this.compilerForm[field] = res.path;
          await this.probeToolchain();
        }
      } catch (err) {
        this.showToast(err.message);
      }
    },

    async saveCompilerSettings() {
      const res = await apiJson("/api/settings", {
        method: "PUT",
        body: JSON.stringify(this.compilerForm),
      });
      this.settings = res.compiler;
      this.showCompilerModal = false;
      this.showToast("编译器配置已保存");
    },

    async triggerCompile() {
      if (!this.activeManuscript || this.isCompiling()) return;
      this.isTriggeringCompile = true;
      const targetTex = this.activeDocTarget.type === "companion" ? this.activeDocTarget.name : "";
      try {
        const st = await apiJson("/api/compile", {
          method: "POST",
          body: JSON.stringify({
            manuscript: this.activeManuscript,
            baseline: this.parseBaselineKey(),
            engine: this.manuscriptEngine,
            target_tex: targetTex,
          }),
        });
        this.compileStatus = st;
      } catch (err) {
        this.isTriggeringCompile = false;
        this.showToast(`触发编译失败: ${err.message}`);
      }
    },

    statusBadgeClass() {
      if (this.isCompiling()) return "compiling";
      if (this.compileStatus.state === "error") return "error";
      if (this.compileStatus.dirty) return "dirty";
      if (this.compileStatus.state === "success") return "success";
      return "idle";
    },

    statusLabel() {
      if (this.isCompiling()) return "编译中...";
      if (this.compileStatus.state === "error") return "编译失败";
      if (this.compileStatus.dirty) return "源码已变更";
      if (this.compileStatus.state === "success") return `已就绪 (${this.compileStatus.engine || "TeX"})`;
      return "待编译";
    },

    reviewers() {
      const set = new Set(this.todos.map((t) => t.reviewer).filter(Boolean));
      return Array.from(set);
    },

    filteredTodos() {
      return this.todos.filter((t) => {
        if (this.reviewerFilter !== "ALL" && t.reviewer !== this.reviewerFilter) return false;
        if (this.statusFilter !== "ALL" && t.status !== this.statusFilter) return false;
        return true;
      });
    },

    todosVerifiedCount() {
      return this.todos.filter((t) => t.status === "verified").length;
    },

    todoProgressPercent() {
      if (!this.todos.length) return 0;
      return Math.round((this.todosVerifiedCount() / this.todos.length) * 100);
    },

    todoProgressText() {
      return `${this.todosVerifiedCount()} / ${this.todos.length} 已核验`;
    },

    statusText(st) {
      return { pending: "待修改", agent_done: "AI已改待核", verified: "已核验" }[st] || st;
    },

    formatTime(iso) {
      if (!iso) return "";
      return iso.replace("T", " ").replace("Z", "");
    },

    async cycleTodoStatus(item) {
      const next = STATUS_CYCLE[(STATUS_CYCLE.indexOf(item.status) + 1) % STATUS_CYCLE.length];
      const doc = await apiJson(
        `/api/todos/${encodeURIComponent(item.id)}?manuscript=${encodeURIComponent(this.activeManuscript)}`,
        { method: "PATCH", body: JSON.stringify({ status: next }) },
      );
      this.applyTodoDoc(doc);
    },

    openTodoModal(item = null) {
      if (item) {
        this.todoForm = { isEdit: true, ...item };
        this.showAdvancedTodoFields = true;
      } else {
        this.todoForm = {
          isEdit: false,
          id: "",
          reviewer: this.reviewerFilter === "ALL" ? "Reviewer #1" : this.reviewerFilter,
          summary: "",
          target_location: "",
          status: "pending",
          response_draft: "",
        };
        this.showAdvancedTodoFields = false;
      }
      this.showTodoModal = true;
    },

    async saveTodo() {
      const ms = encodeURIComponent(this.activeManuscript);
      try {
        let doc;
        if (this.todoForm.isEdit) {
          doc = await apiJson(`/api/todos/${encodeURIComponent(this.todoForm.id)}?manuscript=${ms}`, {
            method: "PATCH",
            body: JSON.stringify(this.todoForm),
          });
        } else {
          doc = await apiJson(`/api/todos?manuscript=${ms}`, {
            method: "POST",
            body: JSON.stringify(this.todoForm),
          });
        }
        this.applyTodoDoc(doc);
        this.showTodoModal = false;
      } catch (err) {
        this.showToast(`保存失败: ${err.message}`);
      }
    },

    requestDeleteTodo(item) {
      this.askConfirm({
        title: "删除审稿意见",
        message: `确认删除审稿意见「${item.id} (${item.reviewer})」？此操作会将其从 .rebuttal_todo.json 中移除。`,
        confirmText: "确认删除",
        onConfirm: async () => {
          const doc = await apiJson(
            `/api/todos/${encodeURIComponent(item.id)}?manuscript=${encodeURIComponent(this.activeManuscript)}`,
            { method: "DELETE" },
          );
          this.applyTodoDoc(doc);
          this.showToast(`已删除 ${item.id}`);
        },
      });
    },

    openHelpModal(tab = "init") {
      this.helpTab = tab;
      this.showHelpModal = true;
    },

    openAiTodoPopover(toggle = false) {
      this.cancelCloseAiTodoPopover();
      this.showAiTodoPopover = toggle ? !this.showAiTodoPopover : true;
    },

    scheduleCloseAiTodoPopover() {
      this.cancelCloseAiTodoPopover();
      this._popoverTimer = setTimeout(() => {
        this.showAiTodoPopover = false;
      }, 220);
    },

    cancelCloseAiTodoPopover() {
      if (this._popoverTimer) {
        clearTimeout(this._popoverTimer);
        this._popoverTimer = null;
      }
    },

    activeManuscriptPath() {
      const found = (this.workspace.manuscripts || []).find((m) => m.name === this.activeManuscript);
      return found ? found.path : "<稿件目录>";
    },

    folderInitPrompt() {
      const root = this.workspace.project_root || "<你的科研项目根目录>";
      return `请帮我将当前项目（${root}）中的 LaTeX 论文手稿整理/初始化为 Rebuttal Sidecar 标准目录结构，方便版本对比、返修与附属文档管理：

1. 目录结构规范：
   - docs/manuscripts/references.bib          （共享参考文献库）
   - docs/manuscripts/_archive/<v1_submitted>/ （保存一版投稿时的原始手稿快照，作为 Diff 对比基准）
   - docs/manuscripts/<manuscript_name>/       （当前活跃返修稿件）
     ├── main.tex                              （主稿入口，含 \\documentclass）
     ├── sections/*.tex                        （按章节拆分子文件并用 \\input{sections/...} 引入）
     ├── figures/                              （图片与矢量 PDF 插图）
     ├── title_page.tex                        （非正文附属文档：作者/单位/致谢/CRediT，含独立 \\documentclass）
     ├── highlights.tex                        （非正文附属文档：投稿亮点，3~5 条且每条 ≤ 85 字符）
     ├── cover_letter.tex                      （非正文附属文档：致编辑信）
     └── .rebuttal_todo.json                   （返修意见跟踪文件）
2. 迁移与安全约束：
   - 对 Git 已跟踪文件优先使用 git mv 保留历史记录；
   - 保证 main.tex 与各个附属 .tex 文件均可独立编译，且不要在 .gitignore 中屏蔽活跃稿件目录；
   - 整理完成后汇报最终目录树。`;
    },

    companionsInitPrompt() {
      const msPath = this.activeManuscriptPath();
      return `请读取当前稿件目录（${msPath}）下的 main.tex 及各 sections/*.tex，帮我生成/完善以下独立可编译的投稿附属文档（与主稿同级放置，Rebuttal Sidecar 左侧树会自动识别并支持单独编译与字数检查）：

1. ${msPath}\\title_page.tex（独立 \\documentclass）：
   - 包含完整论文标题、作者姓名、单位、通讯作者邮箱、Acknowledgments 与 CRediT Authorship Contribution Statement（便于双盲投稿时与匿名主稿分离）。
2. ${msPath}\\highlights.tex（独立 \\documentclass）：
   - 使用 \\begin{itemize} ... \\end{itemize} 列出 3~5 条 \\item，严格控制每条 \\item 文本长度不超过 85 个字符（含空格），突出方法创新与核心实验结论。
3. ${msPath}\\cover_letter.tex（独立 \\documentclass）：
   - 包含致编辑的投稿/返修说明信，简述本文贡献、主要修改点及利益冲突声明（也可单独拆分为 declaration.tex）。

完成后请保留原有 main.tex 编译不受影响，并告知我已生成的附属文件清单。`;
    },

    todoInitPrompt() {
      const msName = this.activeManuscript || "main_manuscript";
      const msPath = this.activeManuscriptPath();
      return `请读取我提供的审稿意见以及当前论文目录（${msPath}）下的 .tex 源码，按以下规范初始化/更新该稿件的返修任务文件：

1. 目标保存路径（必须严格写入此路径）：
   ${msPath}\\.rebuttal_todo.json
2. JSON 数据格式规范：
   {
     "manuscript": "${msName}",
     "active_baseline": null,
     "engine": "auto",
     "items": [
       {
         "id": "R1-C1",
         "reviewer": "Reviewer #1",
         "summary": "简明概括该条审稿意见的核心要求",
         "target_location": "Section 3.2 (sections/method.tex, 第2段), Page 4",
         "status": "pending",
         "response_draft": "",
         "updated_at": "2026-11-26T15:00:00Z"
       }
     ]
   }
3. 段落关联与状态规则：
   - 必须检索论文 .tex 文件，把每条意见尽可能精确落实到具体章节、子文件与段落/公式/图表（写入 target_location，如包含 Page N 界面可直接跳页）；
   - 新录入意见 status 设为 "pending"；当你完成对应 .tex 修改并撰写 response_draft 后，再将 status 更新为 "agent_done"；
   - 仅修改 .rebuttal_todo.json，无需手工编译，Rebuttal Sidecar 会自动实时同步看板。`;
    },

    async copyAiPrompt(text, tag) {
      try {
        await navigator.clipboard.writeText(text);
        this.aiCopyTag = tag;
        this.showToast("提示词已复制到剪贴板");
        setTimeout(() => {
          if (this.aiCopyTag === tag) this.aiCopyTag = "";
        }, 1800);
      } catch {
        this.showToast("复制失败，请手动选中文本框复制");
      }
    },

    async openExportModal() {
      this.showExportModal = true;
      await this.loadExport(this.exportFormat);
    },

    async loadExport(fmt) {
      this.exportFormat = fmt;
      const res = await apiJson(
        `/api/todos/export?manuscript=${encodeURIComponent(this.activeManuscript)}&format=${fmt}`,
      );
      this.exportContent = res.content || "";
    },

    async copyExport() {
      await navigator.clipboard.writeText(this.exportContent);
      this.copyFeedback = "已复制";
      setTimeout(() => (this.copyFeedback = ""), 1500);
    },

    downloadExport() {
      const ext = this.exportFormat === "tex" ? "tex" : "md";
      const blob = new Blob([this.exportContent], { type: "text/plain;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `response_letter_${this.activeManuscript}.${ext}`;
      a.click();
      URL.revokeObjectURL(a.href);
    },

    hasCurrentModePdf() {
      if (this.viewMode === "clean") return this.compileStatus.has_clean;
      if (this.viewMode === "diff") return this.compileStatus.has_diff;
      return this.compileStatus.has_clean || this.compileStatus.has_diff;
    },

    pdfEmptyMessage() {
      if (this.viewMode === "diff" && !this.selectedBaselineKey && this.compileStatus.has_clean) {
        return "当前未选择对比基准（处于仅 Clean 模式），请切换至「Clean 净版」查看或在顶部选择历史基准。";
      }
      if (!this.settings.auto_compile) {
        return "当前默认关闭自动编译，点击「立即编译预览」生成 Clean / Diff PDF。";
      }
      return "尚未编译当前稿件。";
    },

    async switchViewMode(mode) {
      this.viewMode = mode;
      await this.renderCurrentView(true);
    },

    async renderCurrentView(preserveScroll) {
      const stage = document.getElementById("pdf-stage");
      const maxScroll = stage ? Math.max(1, stage.scrollHeight - stage.clientHeight) : 1;
      const savedRatio = preserveScroll && stage ? stage.scrollTop / maxScroll : 0;
      const token = ++this._renderToken;
      const ms = encodeURIComponent(this.activeManuscript);
      const bust = Date.now();

      if (this.viewMode === "split") {
        const cleanEl = document.getElementById("split-clean-container");
        const diffEl = document.getElementById("split-diff-container");
        if (cleanEl && this.compileStatus.has_clean) {
          await this.renderPdfInto(`/api/pdf/clean?manuscript=${ms}&t=${bust}`, cleanEl, token, false);
        } else if (cleanEl) {
          cleanEl.innerHTML = "";
        }
        if (diffEl && this.compileStatus.has_diff) {
          await this.renderPdfInto(`/api/pdf/diff?manuscript=${ms}&t=${bust}`, diffEl, token, false);
        } else if (diffEl) {
          diffEl.innerHTML = "";
        }
      } else {
        const container = document.getElementById("single-pdf-container");
        if (!container || !this.hasCurrentModePdf()) {
          if (container) container.innerHTML = "";
          this.totalPages = 0;
          return;
        }
        const kind = this.viewMode === "clean" ? "clean" : "diff";
        await this.renderPdfInto(`/api/pdf/${kind}?manuscript=${ms}&t=${bust}`, container, token, true);
      }

      if (preserveScroll && stage && token === this._renderToken) {
        requestAnimationFrame(() => {
          const newMax = Math.max(0, stage.scrollHeight - stage.clientHeight);
          stage.scrollTop = savedRatio * newMax;
        });
      }
    },

    async renderPdfInto(url, container, token, updateCounter) {
      try {
        const pdfjsLib = await getPdfJs();
        const doc = await pdfjsLib.getDocument({ url, isEvalSupported: false }).promise;
        if (token !== this._renderToken) return;
        if (updateCounter) {
          this.totalPages = doc.numPages;
          this.currentPage = Math.min(this.currentPage || 1, doc.numPages);
        }

        const frag = document.createDocumentFragment();
        const scale = this.viewMode === "split" && this.activeDocTarget.type === "main"
          ? Math.max(0.65, this.zoomScale * 0.72)
          : this.zoomScale;
        const dpr = Math.min(window.devicePixelRatio || 1, 2);

        for (let num = 1; num <= doc.numPages; num++) {
          if (token !== this._renderToken) return;
          const page = await doc.getPage(num);
          const viewport = page.getViewport({ scale });
          const canvas = document.createElement("canvas");
          canvas.className = "pdf-page-canvas";
          canvas.dataset.page = String(num);
          canvas.width = Math.floor(viewport.width * dpr);
          canvas.height = Math.floor(viewport.height * dpr);
          canvas.style.width = `${Math.floor(viewport.width)}px`;
          canvas.style.height = `${Math.floor(viewport.height)}px`;

          const ctx = canvas.getContext("2d");
          ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
          await page.render({ canvasContext: ctx, viewport }).promise;
          frag.appendChild(canvas);
        }
        if (token === this._renderToken) {
          container.innerHTML = "";
          container.appendChild(frag);
        }
      } catch (err) {
        if (token === this._renderToken) {
          container.innerHTML = `<div class="empty-hint">PDF 渲染失败: ${err.message}</div>`;
        }
      }
    },

    onPdfScroll() {
      const stage = document.getElementById("pdf-stage");
      const selector = this.activeDocTarget.type === "companion"
        ? "#companion-pdf-container .pdf-page-canvas"
        : "#single-pdf-container .pdf-page-canvas";
      const canvases = document.querySelectorAll(selector);
      if (!stage || !canvases.length) return;
      const mid = stage.scrollTop + stage.clientHeight * 0.3;
      for (const c of canvases) {
        if (c.offsetTop + c.offsetHeight >= mid) {
          this.currentPage = Number(c.dataset.page) || 1;
          break;
        }
      }
    },

    goToPage(num) {
      const target = Math.max(1, Math.min(this.totalPages || 1, num));
      const selector = this.activeDocTarget.type === "companion"
        ? `#companion-pdf-container .pdf-page-canvas[data-page="${target}"]`
        : `#single-pdf-container .pdf-page-canvas[data-page="${target}"]`;
      const canvas = document.querySelector(selector);
      if (canvas) {
        canvas.scrollIntoView({ behavior: "smooth", block: "start" });
        this.currentPage = target;
      }
    },

    stepPage(delta) {
      this.goToPage((this.currentPage || 1) + delta);
    },

    jumpToLocation(locText) {
      const match = /(?:page|p\.?|页)\s*(\d+)/i.exec(locText || "");
      if (match) {
        this.goToPage(Number(match[1]));
      }
    },

    async adjustZoom(delta) {
      this.zoomScale = Math.max(0.5, Math.min(2.4, Number((this.zoomScale + delta).toFixed(2))));
      if (this.activeDocTarget.type === "companion" && this.companionViewMode === "pdf") {
        await this.renderCompanionPdf(true);
      } else {
        await this.renderCurrentView(true);
      }
    },

    async resetZoom() {
      this.zoomScale = 1.15;
      if (this.activeDocTarget.type === "companion" && this.companionViewMode === "pdf") {
        await this.renderCompanionPdf(true);
      } else {
        await this.renderCurrentView(true);
      }
    },
  };
}

Alpine.data("sidecarApp", sidecarApp);
window.Alpine = Alpine;
Alpine.start();
