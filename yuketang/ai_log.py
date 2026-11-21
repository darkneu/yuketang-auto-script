"""Append AI replies locally without file I/O on the task thread."""
from __future__ import annotations

import json
import queue
import threading
from pathlib import Path


class AIReplyLog:
    def __init__(self, path: Path):
        self.path = path
        self.failed = False
        self.events = queue.SimpleQueue()
        self.thread = threading.Thread(target=self._write, name="yuketang-ai-log", daemon=True)
        self.thread.start()

    def record(self, entry):
        self.events.put(entry)

    def _write(self):
        stream = None
        try:
            while (entry := self.events.get()) is not None:
                if not self.failed:
                    try:
                        if stream is None:
                            self.path.parent.mkdir(parents=True, exist_ok=True)
                            stream = self.path.open("a", encoding="utf-8")
                        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
                        stream.flush()
                    except (OSError, ValueError, TypeError):
                        # Logging failure cannot trigger another AI call or submission.
                        self.failed = True
        finally:
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    self.failed = True

    def close(self):
        self.events.put(None)
        self.thread.join()
