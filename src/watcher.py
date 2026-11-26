"""Debounced Watchdog file monitor with optional auto-compile and live Todo sync."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

SOURCE_SUFFIXES = {".tex", ".bib", ".cls", ".sty", ".bst"}
TODO_FILENAME = ".rebuttal_todo.json"


class _Handler(FileSystemEventHandler):
    def __init__(self, owner: "ManuscriptWatcher") -> None:
        self._owner = owner

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._owner.handle_path(Path(event.src_path))

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._owner.handle_path(Path(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory and getattr(event, "dest_path", None):
            self._owner.handle_path(Path(event.dest_path))


class ManuscriptWatcher:
    """Watches active manuscript sources and .rebuttal_todo.json with debouncing."""

    def __init__(
        self,
        on_auto_compile: Callable[[str], None],
        on_source_dirty: Callable[[str, str], None],
        on_todo_changed: Callable[[str], None],
        was_self_todo_write: Callable[[Path], bool],
    ) -> None:
        self._on_auto_compile = on_auto_compile
        self._on_source_dirty = on_source_dirty
        self._on_todo_changed = on_todo_changed
        self._was_self_todo_write = was_self_todo_write

        self._lock = threading.Lock()
        self._observer: Observer | None = None
        self._manuscripts_dir: Path | None = None
        self._active_manuscript: str = ""
        self.auto_compile: bool = False
        self.debounce_ms: int = 800

        self._compile_timer: threading.Timer | None = None
        self._dirty_timer: threading.Timer | None = None
        self._todo_timer: threading.Timer | None = None

    def _cancel_timer(self, timer: threading.Timer | None) -> None:
        if timer is not None:
            timer.cancel()

    def configure(
        self,
        manuscripts_dir: Path | None,
        active_manuscript: str,
        auto_compile: bool,
        debounce_ms: int,
    ) -> None:
        need_restart = False
        with self._lock:
            resolved = manuscripts_dir.resolve() if manuscripts_dir else None
            if resolved != self._manuscripts_dir:
                need_restart = True
                self._manuscripts_dir = resolved
            self._active_manuscript = active_manuscript
            self.auto_compile = bool(auto_compile)
            self.debounce_ms = max(200, int(debounce_ms))
            if not self.auto_compile:
                self._cancel_timer(self._compile_timer)
                self._compile_timer = None

        if need_restart:
            self._restart_observer()

    def update_options(self, auto_compile: bool, debounce_ms: int) -> None:
        with self._lock:
            self.auto_compile = bool(auto_compile)
            self.debounce_ms = max(200, int(debounce_ms))
            if not self.auto_compile:
                self._cancel_timer(self._compile_timer)
                self._compile_timer = None

    def set_active_manuscript(self, manuscript_name: str) -> None:
        with self._lock:
            if manuscript_name != self._active_manuscript:
                self._active_manuscript = manuscript_name
                self._cancel_timer(self._compile_timer)
                self._compile_timer = None

    def _restart_observer(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None

        if self._manuscripts_dir and self._manuscripts_dir.is_dir():
            observer = Observer(timeout=0.1)
            observer.schedule(_Handler(self), str(self._manuscripts_dir), recursive=True)
            observer.daemon = True
            observer.start()
            self._observer = observer

    def stop(self) -> None:
        with self._lock:
            self._cancel_timer(self._compile_timer)
            self._cancel_timer(self._dirty_timer)
            self._cancel_timer(self._todo_timer)
            self._compile_timer = None
            self._dirty_timer = None
            self._todo_timer = None
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None

    def handle_path(self, raw_path: Path) -> None:
        with self._lock:
            manuscripts_dir = self._manuscripts_dir
            active = self._active_manuscript
            auto_compile = self.auto_compile
            debounce_sec = self.debounce_ms / 1000.0

        if not manuscripts_dir or not active:
            return

        path = raw_path.resolve()
        if path.name.endswith(".tmp"):
            return

        active_dir = (manuscripts_dir / active).resolve()
        todo_path = (active_dir / TODO_FILENAME).resolve()

        if path == todo_path:
            if self._was_self_todo_write(path):
                return
            with self._lock:
                self._cancel_timer(self._todo_timer)
                self._todo_timer = threading.Timer(0.2, self._on_todo_changed, args=(active,))
                self._todo_timer.daemon = True
                self._todo_timer.start()
            return

        in_active_dir = active_dir in path.parents
        is_shared_top = path.parent == manuscripts_dir
        if not (in_active_dir or is_shared_top):
            return

        if path.suffix.lower() not in SOURCE_SUFFIXES:
            return

        with self._lock:
            self._cancel_timer(self._dirty_timer)
            self._dirty_timer = threading.Timer(0.25, self._on_source_dirty, args=(active, path.name))
            self._dirty_timer.daemon = True
            self._dirty_timer.start()

            if auto_compile:
                self._cancel_timer(self._compile_timer)
                self._compile_timer = threading.Timer(debounce_sec, self._on_auto_compile, args=(active,))
                self._compile_timer.daemon = True
                self._compile_timer.start()
