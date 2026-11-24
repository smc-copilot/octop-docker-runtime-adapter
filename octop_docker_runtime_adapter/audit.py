from __future__ import annotations

import json
import logging
import logging.handlers
import os
from datetime import datetime, timezone


class Audit:
    def __init__(self, path=None):
        self.logger = logging.getLogger("octop_dra." + str(os.getpid()))
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        if not self.logger.handlers:
            stream = logging.StreamHandler()
            stream.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(stream)
            if path:
                # Parent provisioned by the deployment command, not startup.
                try:
                    handler = logging.handlers.RotatingFileHandler(
                        path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
                    handler.setFormatter(logging.Formatter("%(message)s"))
                    self.logger.addHandler(handler)
                except OSError:
                    self.emit("file_log_unavailable", level="warning")

    def emit(self, event, level="info", **fields):
        # Never pass kwargs, command, environment, raw inspect or exception messages.
        record = {"component": "octop_dra", "event": event,
                  "time": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(), **fields}
        getattr(self.logger, level)(json.dumps(record, ensure_ascii=False, sort_keys=True))
