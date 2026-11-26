import * as pdfjsLib from "/pdfjs/pdf.min.mjs";

pdfjsLib.GlobalWorkerOptions.workerSrc = "/pdfjs/pdf.worker.min.mjs";

const STATUS_CYCLE = ["pending", "agent_done", "verified"];

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

window.sidecarApp = function sidecarApp() {
  return {
    workspace: {
      project_root: "",
      manuscripts_dir: "",
      manuscripts: [],
      archive_baselines: [],
      recent_workspaces: [],
    },
    activeManuscript: "",
    baselines: { archive: [], git: [] },
    selectedBaselineKey: "",
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

    compileStatus: {
      state: "idle",
      dirty: false,
      has_clean: false,
      has_diff: false,
      errors: [],
      log_tail: "",
      message: "",
    },

    viewMode: "diff",
    currentPage: 1,
    totalPages: 0,
    zoomScale: 1.15,
    _renderToken: 0,
    _sse: null,

    showCompilerModal: false,
    showWorkspaceModal: false,
    workspaceInputPath: "",
    showTodoModal: false,
    todoForm: { isEdit: false, id: "", reviewer: "Reviewer #1", summary: "", target_location: "", status: "pending", response_draft: "" },
    showExportModal: false,
    exportFormat: "md",
    exportContent: "",
    copyFeedback: "",
    showErrorDrawer: false,
    toastMessage: "",

    async init() {
      await Promise.all([this.loadSettings(), this.loadWorkspace()]);
      this.connectSSE();
    },

    showToast(msg) {
      this.toastMessage = msg;
      setTimeout(() => {
        if (this.toastMessage === msg) this.toastMessage = "";
      }, 2800);
    },

    connectSSE() {
      if (this._sse) this._sse.close();
      const es = new EventSource("/api/events");
      es.addEventListener("WORKSPACE_UPDATED", async (e) => {
        this.applyWorkspace(JSON.parse(e.data));
        if (this.activeManuscript) await this.loadManuscriptState(this.activeManuscript);
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
          this.compileStatus = st;
          this.showErrorDrawer = false;
          if (!st.has_diff && this.viewMode === "diff") {
            this.viewMode = "clean";
          }
          await this.renderCurrentView(true);
          this.showToast("编译完成");
        }
      });
      es.addEventListener("COMPILE_ERROR", (e) => {
        const st = JSON.parse(e.data);
        if (st.manuscript === this.activeManuscript) {
          this.compileStatus = st;
          this.showErrorDrawer = true;
        }
      });
      this._sse = es;
    },

    workspaceName() {
      if (!this.workspace.project_root) return "打开工程";
      const parts = this.workspace.project_root.replace(/[/\\]+$/, "").split(/[/\\]/);
      return parts[parts.length - 1] || this.workspace.project_root;
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
    },

    async selectManuscript(name) {
      this.activeManuscript = name;
      await this.loadManuscriptState(name);
    },

    async loadManuscriptState(name) {
      const [baselines, todoDoc, status] = await Promise.all([
        apiJson(`/api/baselines?manuscript=${encodeURIComponent(name)}`),
        apiJson(`/api/todos?manuscript=${encodeURIComponent(name)}`),
        apiJson(`/api/compile/status?manuscript=${encodeURIComponent(name)}`),
      ]);
      this.baselines = baselines;
      this.applyTodoDoc(todoDoc);
      this.compileStatus = status;
      this.showErrorDrawer = status.state === "error";
      await this.renderCurrentView(false);
    },

    applyTodoDoc(doc) {
      this.todos = doc.items || [];
      this.manuscriptEngine = doc.engine || "auto";
      const bl = doc.active_baseline;
      this.selectedBaselineKey = bl && bl.type && bl.ref ? `${bl.type}:${bl.ref}` : "";
    },

    async openWorkspacePicker() {
      if (this.workspace.project_root) {
        this.workspaceInputPath = this.workspace.project_root;
      }
      this.showWorkspaceModal = true;
    },

    async selectFolderNative() {
      try {
        const res = await apiJson("/api/workspace/select", { method: "POST" });
        if (!res.cancelled) {
          this.showWorkspaceModal = false;
          this.applyWorkspace(res);
          if (this.activeManuscript) await this.loadManuscriptState(this.activeManuscript);
        }
      } catch {
        this.showWorkspaceModal = true;
      }
    },

    async openWorkspacePath(path) {
      if (!path) return;
      try {
        const res = await apiJson("/api/workspace/open", {
          method: "POST",
          body: JSON.stringify({ path }),
        });
        this.showWorkspaceModal = false;
        this.applyWorkspace(res);
        if (this.activeManuscript) await this.loadManuscriptState(this.activeManuscript);
      } catch (err) {
        this.showToast(`打开失败: ${err.message}`);
      }
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
      await apiJson(`/api/baseline?manuscript=${encodeURIComponent(this.activeManuscript)}`, {
        method: "PUT",
        body: JSON.stringify({ active_baseline: this.parseBaselineKey(), engine: this.manuscriptEngine }),
      });
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
    },

    async toggleAutoCompile(enabled) {
      const res = await apiJson("/api/settings", {
        method: "PUT",
        body: JSON.stringify({ auto_compile: enabled }),
      });
      this.settings = res.compiler;
      this.showToast(enabled ? "已开启自动编译" : "已关闭自动编译");
    },

    async openCompilerModal() {
      this.compilerForm = { ...this.settings };
      this.showCompilerModal = true;
      await this.probeToolchain();
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
      if (!this.activeManuscript) return;
      try {
        const st = await apiJson("/api/compile", {
          method: "POST",
          body: JSON.stringify({
            manuscript: this.activeManuscript,
            baseline: this.parseBaselineKey(),
            engine: this.manuscriptEngine,
          }),
        });
        this.compileStatus = st;
      } catch (err) {
        this.showToast(`触发编译失败: ${err.message}`);
      }
    },

    statusBadgeClass() {
      if (this.compileStatus.state === "compiling") return "compiling";
      if (this.compileStatus.state === "error") return "error";
      if (this.compileStatus.dirty) return "dirty";
      if (this.compileStatus.state === "success") return "success";
      return "idle";
    },

    statusLabel() {
      if (this.compileStatus.state === "compiling") return "编译中...";
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

    todoProgressPercent() {
      if (!this.todos.length) return 0;
      const done = this.todos.filter((t) => t.status === "verified").length;
      return Math.round((done / this.todos.length) * 100);
    },

    todoProgressText() {
      const done = this.todos.filter((t) => t.status === "verified").length;
      return `${done} / ${this.todos.length} 已核验`;
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

    async deleteTodo(itemId) {
      const doc = await apiJson(
        `/api/todos/${encodeURIComponent(itemId)}?manuscript=${encodeURIComponent(this.activeManuscript)}`,
        { method: "DELETE" },
      );
      this.applyTodoDoc(doc);
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
        const doc = await pdfjsLib.getDocument({ url, isEvalSupported: false }).promise;
        if (token !== this._renderToken) return;
        if (updateCounter) {
          this.totalPages = doc.numPages;
          this.currentPage = Math.min(this.currentPage || 1, doc.numPages);
        }

        const frag = document.createDocumentFragment();
        const scale = this.viewMode === "split" ? Math.max(0.65, this.zoomScale * 0.72) : this.zoomScale;
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
      const canvases = document.querySelectorAll("#single-pdf-container .pdf-page-canvas");
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
      const canvas = document.querySelector(`#single-pdf-container .pdf-page-canvas[data-page="${target}"]`);
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
      await this.renderCurrentView(true);
    },

    async resetZoom() {
      this.zoomScale = 1.15;
      await this.renderCurrentView(true);
    },
  };
};
