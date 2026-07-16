"""Low-cardinality Prometheus text rendering for monitor snapshots."""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from models import MonitorSnapshot


def _escape(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _sample(name: str, value: int | float, **labels: object) -> str:
    suffix = ""
    if labels:
        rendered = ",".join(
            f'{key}="{_escape(labels[key])}"' for key in sorted(labels)
        )
        suffix = "{" + rendered + "}"
    return f"{name}{suffix} {value}"


def _family(name: str, help_text: str, metric_type: str, samples: Iterable[str]) -> list[str]:
    return [f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}", *samples]


def render_prometheus(snapshot: MonitorSnapshot) -> str:
    """Render one snapshot without session, process, error, or endpoint labels."""

    lines: list[str] = []
    lines.extend(
        _family(
            "codexnet_collection_duration_seconds",
            "Duration of the latest monitor collection.",
            "gauge",
            [_sample("codexnet_collection_duration_seconds", snapshot.collection_duration_seconds)],
        )
    )
    lines.extend(
        _family(
            "codexnet_instances",
            "Discovered Codex instances.",
            "gauge",
            [_sample("codexnet_instances", len(snapshot.instances))],
        )
    )

    session_samples: list[str] = []
    network_samples: list[str] = []
    alert_samples: list[str] = []
    event_samples: list[str] = []
    token_samples: list[str] = []
    collector_samples: list[str] = []
    for instance in snapshot.instances:
        lifecycle = Counter(session.lifecycle.value for session in instance.sessions)
        network = Counter(session.network.state.value for session in instance.sessions)
        alerts = Counter(
            (session.alert_level or "active")
            for session in instance.sessions
            if session.alert
        )
        events = Counter(
            event.kind.upper()
            for session in instance.sessions
            for event in session.events
        )
        for state, count in sorted(lifecycle.items()):
            session_samples.append(
                _sample("codexnet_sessions", count, instance=instance.instance_id, state=state)
            )
        for state, count in sorted(network.items()):
            network_samples.append(
                _sample(
                    "codexnet_network_sessions",
                    count,
                    instance=instance.instance_id,
                    state=state,
                )
            )
        for category, count in sorted(alerts.items()):
            alert_samples.append(
                _sample(
                    "codexnet_alerts",
                    count,
                    instance=instance.instance_id,
                    category=category,
                )
            )
        for event_type, count in sorted(events.items()):
            event_samples.append(
                _sample(
                    "codexnet_snapshot_events",
                    count,
                    instance=instance.instance_id,
                    event_type=event_type,
                )
            )

        token_totals: Counter[str] = Counter()
        for session in instance.sessions:
            usage = session.cumulative_token_usage or session.token_usage
            if usage is None:
                continue
            for category, value in (
                ("input", usage.input_tokens),
                ("cached_input", usage.cached_input_tokens),
                ("output", usage.output_tokens),
                ("reasoning_output", usage.reasoning_output_tokens),
                ("total", usage.total_tokens),
            ):
                if value is not None:
                    token_totals[category] += value
        for category, value in sorted(token_totals.items()):
            token_samples.append(
                _sample(
                    "codexnet_tokens",
                    value,
                    instance=instance.instance_id,
                    category=category,
                )
            )

        for collector in instance.collector_health:
            category = (collector.name or "unknown").split(":", 1)[0]
            collector_samples.append(
                _sample(
                    "codexnet_collector_healthy",
                    int(not collector.error and not collector.budget_exceeded),
                    instance=instance.instance_id,
                    category=category,
                )
            )

    lines.extend(_family("codexnet_sessions", "Sessions by lifecycle state.", "gauge", session_samples))
    lines.extend(
        _family(
            "codexnet_network_sessions",
            "Sessions by network state.",
            "gauge",
            network_samples,
        )
    )
    lines.extend(_family("codexnet_alerts", "Active alerts by category.", "gauge", alert_samples))
    lines.extend(
        _family(
            "codexnet_snapshot_events",
            "Events retained in the current snapshot by type.",
            "gauge",
            event_samples,
        )
    )
    lines.extend(
        _family("codexnet_tokens", "Latest aggregate token usage.", "gauge", token_samples)
    )
    lines.extend(
        _family(
            "codexnet_collector_healthy",
            "Whether the latest instance collector sample is healthy.",
            "gauge",
            collector_samples,
        )
    )
    return "\n".join(lines) + "\n"


class PrometheusMetrics:
    """Small adapter suitable for textfile or one-shot CLI integration."""

    def render(self, snapshot: MonitorSnapshot) -> str:
        return render_prometheus(snapshot)
