"""Thread-safe ``decisions.jsonl`` writer.

Every page and every image candidate gets one line: title, url, decision, rule_id,
detail.  A human can audit *exactly* why an image was used or dropped.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from models import Decision, ImageDecision

LOG = logging.getLogger("pipeline.decisions")


class DecisionLog:
    """Append-only JSONL log, safe for the worker threads."""

    def __init__(self, path: Path, lock: threading.Lock | None = None) -> None:
        self._path = path
        self._lock = lock or threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        title: str,
        url: str,
        decision: str,
        rule_id: str,
        detail: str = "",
    ) -> None:
        """Write one decision line (``decision`` is ``keep`` or ``reject``)."""
        payload = ImageDecision(
            title=title, url=url, decision=decision, rule_id=rule_id, detail=detail
        )
        line = json.dumps(
            {
                "title": payload.title,
                "url": payload.url,
                "decision": payload.decision,
                "rule_id": payload.rule_id,
                "detail": payload.detail,
            },
            ensure_ascii=False,
        )
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()

    def keep(self, *, title: str, url: str, rule_id: str, detail: str = "") -> None:
        self.record(
            title=title, url=url, decision=Decision.KEEP.value, rule_id=rule_id, detail=detail
        )

    def reject(self, *, title: str, url: str, rule_id: str, detail: str = "") -> None:
        self.record(
            title=title, url=url, decision=Decision.REJECT.value, rule_id=rule_id, detail=detail
        )
        LOG.info("[reject:%s] %s | %s", rule_id, title, detail or url)
