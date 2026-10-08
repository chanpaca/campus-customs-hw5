"""Append-only audit trail for the Campus Customs agent team.

Every step the team takes - agent started, delegation, tool call, voucher
prepared, run finished - is appended to output/audit_trail.json. The file is
never truncated and never rewritten from scratch: each append reads what is
already there, adds the new event, and swaps the file atomically via
os.replace, so a crash mid-write cannot leave a half-file behind.

The file is a JSON array so a reviewer can `json.load` it directly. A `.jsonl`
mirror is written alongside it as a pure append (`open(..., "a")`), which is
the format that survives even a kill during the write.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from backend.models import AuditEvent

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUDIT_PATH = PROJECT_ROOT / "output" / "audit_trail.json"
AUDIT_JSONL_PATH = PROJECT_ROOT / "output" / "audit_trail.jsonl"

# Serialises appends from concurrent agent runs in the same process.
_LOCK = threading.Lock()


class AuditTrail:
    """Append-only writer. One instance per run; `seq` numbers the steps."""

    def __init__(self, run_id: str, path: Path | None = None, mode: str = "agent") -> None:
        self.run_id = run_id
        #: Stamped onto every event. "scripted" marks a run with no LLM in the
        #: loop, so a reader can never mistake a replayed plan for a decision.
        self.mode = mode
        self.path = path or AUDIT_PATH
        self.jsonl_path = (
            AUDIT_JSONL_PATH if path is None else path.with_suffix(".jsonl")
        )
        self._seq = 0
        self.events: list[AuditEvent] = []

    # -- reading -----------------------------------------------------------

    def _load_existing(self) -> list[dict[str, Any]]:
        """Read the events already on disk. Never raises on a missing file.

        A corrupt or non-array file is preserved rather than discarded: we move
        it aside and start a fresh array, because silently overwriting an audit
        trail is worse than leaving a stray file.
        """
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "[]")
        except json.JSONDecodeError:
            salvage = self.path.with_suffix(".corrupt.json")
            self.path.replace(salvage)
            return []
        return data if isinstance(data, list) else [data]

    # -- writing -----------------------------------------------------------

    def append(self, event: str, **fields: Any) -> AuditEvent:
        """Append one event. Returns the event as written."""
        with _LOCK:
            self._seq += 1
            fields.setdefault("mode", self.mode)
            entry = AuditEvent(run_id=self.run_id, seq=self._seq, event=event, **fields)
            self.events.append(entry)

            payload = entry.model_dump(mode="json")

            # JSONL mirror: a true append, so it survives an interrupted write.
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with self.jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

            # JSON array: read, append, atomic swap. Existing events are kept.
            existing = self._load_existing()
            existing.append(payload)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            os.replace(tmp, self.path)  # atomic on Windows and POSIX

            return entry

    # -- convenience wrappers ---------------------------------------------

    def tool_call(
        self,
        agent: Any,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        ok: bool = True,
        error: str | None = None,
        ticket_id: int | None = None,
    ) -> AuditEvent:
        return self.append(
            "tool_call",
            agent=agent,
            ticket_id=ticket_id,
            tool_name=tool_name,
            tool_args=arguments,
            tool_result=result,
            ok=ok,
            message=error if error else f"{agent} called {tool_name}",
        )

    def delegation(
        self,
        from_agent: Any,
        to_agent: Any,
        question: str,
        ticket_id: int | None = None,
        accepted: bool = True,
        reason: str | None = None,
    ) -> AuditEvent:
        return self.append(
            "delegation" if accepted else "delegation_refused",
            agent=from_agent,
            to_agent=to_agent,
            ticket_id=ticket_id,
            message=question if accepted else f"refused: {reason}",
            ok=accepted,
        )


def read_trail(path: Path | None = None) -> list[dict[str, Any]]:
    """Read the whole audit trail back. Used by the backend routes and tests."""
    target = path or AUDIT_PATH
    if not target.exists():
        return []
    data = json.loads(target.read_text(encoding="utf-8") or "[]")
    return data if isinstance(data, list) else [data]


def read_recent(limit: int = 50, path: Path | None = None) -> list[dict[str, Any]]:
    """Most recent events, newest last - what the dashboard feed renders."""
    return read_trail(path)[-limit:]
