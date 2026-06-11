#!/usr/bin/env python3
"""Inotify-based filesystem watcher for the sandbox /workspace/ directory.

Watches /workspace/ recursively using watchdog and appends JSONL change events
to /tmp/fs_events.jsonl. Writes its PID to /tmp/fs_watcher.pid for lifecycle
management. Ignores .git directories, hidden files starting with '.', and
__pycache__ directories.
"""

import os
import sys
import time
import json

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


class WorkspaceChangeHandler(FileSystemEventHandler):
    """Handles filesystem events and appends JSONL lines to the event log."""

    IGNORED_DIRS = {".git", "__pycache__"}

    def _should_ignore(self, path: str) -> bool:
        parts = path.split(os.sep)
        for part in parts:
            if part in self.IGNORED_DIRS or (part.startswith(".") and part != "."):
                return True
        return False

    def _relative_path(self, path: str) -> str:
        workspace = "/workspace/"
        if path.startswith(workspace):
            return path[len(workspace):]
        return path

    def _append_event(self, path: str, event_type: str) -> None:
        if self._should_ignore(path):
            return
        rel = self._relative_path(path)
        if not rel:
            return
        entry = {"path": rel, "event": event_type, "ts": time.time()}
        with open("/tmp/fs_events.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")

    def on_created(self, event):
        if not event.is_directory:
            self._append_event(event.src_path, "create")

    def on_modified(self, event):
        if not event.is_directory:
            self._append_event(event.src_path, "modify")

    def on_deleted(self, event):
        if not event.is_directory:
            self._append_event(event.src_path, "delete")


def main():
    # Write PID file for lifecycle management
    with open("/tmp/fs_watcher.pid", "w") as f:
        f.write(str(os.getpid()))

    # Clear any stale event log
    open("/tmp/fs_events.jsonl", "w").close()

    path = "/workspace"
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)

    event_handler = WorkspaceChangeHandler()
    observer = Observer()
    observer.schedule(event_handler, path, recursive=True)
    observer.start()

    try:
        while observer.is_alive():
            observer.join(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
