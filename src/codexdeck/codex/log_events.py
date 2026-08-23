"""Normalize supporting Codex SQLite and SSE log families."""

from __future__ import annotations

import json
import re

from codexdeck.codex.state_store import LogRecord
from codexdeck.config import EVENT_LABELS
from codexdeck.models import Confidence, FailureInfo, NormalizedEvent
from codexdeck.utils import one_line, redact_sensitive


def _event(
    record: LogRecord,
    kind: str,
    detail: str = "",
    *,
    source: str = "log",
    metadata: dict[str, object] | None = None,
    failure: FailureInfo | None = None,
) -> NormalizedEvent:
    cleaned = redact_sensitive(detail)
    if kind != "TURN_FAILED" and len(cleaned) > 480:
        cleaned = cleaned[:479] + "…"
    return NormalizedEvent(
        record.timestamp,
        kind,
        EVENT_LABELS.get(kind, kind),
        detail=cleaned,
        source=source,
        confidence=Confidence.MEDIUM,
        failure=failure,
        source_id=f"log:{record.log_id}",
        metadata=metadata or {},
    )


def normalize_log(record: LogRecord) -> list[NormalizedEvent]:
    body = record.body
    lowered = body.lower()
    if record.target == "codex_core::responses_retry":
        if "falling back from websockets to https" in lowered:
            return [_event(record, "TRANSPORT_FALLBACK", one_line(body))]
        if "stream disconnected" in lowered or "idle timeout waiting for sse" in lowered:
            return [_event(record, "RECONNECTING", one_line(body))]
    if record.target == "codex_http_client::transport" and "/responses" in lowered:
        compacting = any(
            marker in lowered
            for marker in ("run_auto_compact{", "run_remote_compact", "run_pre_sampling_compact")
        )
        if not compacting and " post to " not in lowered:
            return []
        trigger = "auto" if "run_auto_compact" in lowered else "unknown"
        return [
            _event(
                record,
                "COMPACTING" if compacting else "REQUEST_SENT",
                metadata={"trigger": trigger} if compacting else {},
            )
        ]
    if record.target == "codex_api::sse::responses" and "sse event: " in lowered:
        encoded = body.split("SSE event: ", 1)[1]
        try:
            payload = json.loads(encoded)
        except json.JSONDecodeError:
            match = re.search(r'"type"\s*:\s*"([^"]+)"', encoded)
            payload = {"type": match.group(1) if match else ""}
        event_type = str(payload.get("type") or "")
        response = payload.get("response")
        response = response if isinstance(response, dict) else {}
        if event_type == "response.completed" and response.get("object") == "response.compaction":
            return [_event(record, "COMPACT_COMPLETED", source="sse")]
        mapped_kind = {
            "keepalive": "KEEPALIVE",
            "response.created": "RESPONSE_STARTED",
            "response.completed": "MODEL_PROGRESS",
            "response.failed": "TURN_FAILED",
            "response.incomplete": "TURN_FAILED",
        }.get(event_type)
        if mapped_kind:
            failure = None
            detail = event_type
            if mapped_kind == "TURN_FAILED":
                error = response.get("error")
                error = error if isinstance(error, dict) else {}
                message = redact_sensitive(
                    str(error.get("message") or response.get("status_details") or event_type)
                )
                category = str(error.get("code") or event_type.replace(".", "_"))
                failure = FailureInfo(category, message, "", "", record.timestamp, "sse")
                detail = message
            return [
                _event(
                    record,
                    mapped_kind,
                    detail,
                    source="sse",
                    failure=failure,
                )
            ]
    return []
