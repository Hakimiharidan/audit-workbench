"""Run log: an ordered list of events, written to runlog.json and the Run log sheet."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field


def json_safe(v):
    from decimal import Decimal
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [json_safe(x) for x in v]
    return v


@dataclass
class RunLog:
    events: list[dict] = field(default_factory=list)

    def event(self, kind: str, message: str, **detail) -> None:
        self.events.append({"kind": kind, "message": message, **{k: json_safe(v) for k, v in detail.items()}})

    def to_json(self, header: dict) -> str:
        return json.dumps({"run": json_safe(header), "events": self.events}, indent=1, ensure_ascii=False, sort_keys=True)
