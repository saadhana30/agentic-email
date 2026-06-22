"""
monitor.py
----------
Central emit helpers for the Live Execution Monitor.

All calls are fire-and-forget — they never raise, never block the pipeline.
Import and call emit_*() from tracer.py, workflow.py, and agent nodes.

Sensitive data rules (enforced here):
  - No token values, no password hashes, no raw stack traces
  - Email subjects and senders are shown (operational data, not secrets)
  - Error messages are sanitized through log_sanitizer before storage
"""

from __future__ import annotations
from src.database.db import emit_event
from src.log_sanitizer import sanitize


def _safe(text: str | None) -> str:
    """Sanitize a string before persisting it as an event message."""
    if not text:
        return ""
    return sanitize(str(text))


# ─────────────────────────────────────────────────────────────────────────────
# Domain-level emit helpers
# ─────────────────────────────────────────────────────────────────────────────

def emit_email_detected(
    email_id: str,
    sender: str,
    subject: str,
    attachment_type: str = "text",
    is_reply: bool = False,
) -> None:
    reply_note = " [thread reply]" if is_reply else ""
    emit_event(
        email_id   = email_id,
        event_type = "email_detected",
        agent_name = "poller",
        status     = "info",
        message    = (
            f"New email detected from {_safe(sender)}"
            f" — \"{_safe(subject)}\"{reply_note}"
        ),
        meta = {
            "sender":          _safe(sender),
            "subject":         _safe(subject),
            "attachment_type": attachment_type,
            "is_reply":        is_reply,
        },
    )


def emit_attachment_extracted(
    email_id: str,
    filename: str,
    attachment_type: str,
    char_count: int,
) -> None:
    emit_event(
        email_id   = email_id,
        event_type = "attachment_extracted",
        agent_name = "email_processor",
        status     = "success",
        message    = (
            f"Attachment extracted: {_safe(filename)} "
            f"({attachment_type}, {char_count} chars)"
        ),
        meta = {
            "filename":        _safe(filename),
            "attachment_type": attachment_type,
            "char_count":      char_count,
        },
    )


def emit_node_started(email_id: str | None, node_name: str) -> None:
    emit_event(
        email_id   = email_id,
        event_type = "node_started",
        agent_name = node_name,
        status     = "running",
        message    = f"{node_name} started",
    )


def emit_node_completed(
    email_id: str | None,
    node_name: str,
    duration_ms: float,
    extra: str = "",
) -> None:
    msg = f"{node_name} completed in {duration_ms:.0f}ms"
    if extra:
        msg += f" — {_safe(extra)}"
    emit_event(
        email_id    = email_id,
        event_type  = "node_completed",
        agent_name  = node_name,
        status      = "success",
        message     = msg,
        duration_ms = duration_ms,
    )


def emit_node_failed(
    email_id: str | None,
    node_name: str,
    duration_ms: float,
    error: str,
) -> None:
    emit_event(
        email_id    = email_id,
        event_type  = "node_failed",
        agent_name  = node_name,
        status      = "failure",
        message     = f"{node_name} failed after {duration_ms:.0f}ms",
        duration_ms = duration_ms,
        # Sanitize error — no stack traces, no token values
        meta        = {"error": _safe(error[:300])},
    )


def emit_spam_detected(email_id: str, sender: str) -> None:
    emit_event(
        email_id   = email_id,
        event_type = "spam_detected",
        agent_name = "spam_classifier",
        status     = "info",
        message    = f"Spam detected from {_safe(sender)} — discarded",
        meta       = {"sender": _safe(sender)},
    )


def emit_routing_decision(
    email_id: str,
    route: str,
    confidence: float,
    agents: list[str],
) -> None:
    if route == "review":
        msg = (
            f"Supervisor routed to review queue "
            f"(confidence {confidence:.0%} below threshold)"
        )
    elif agents:
        msg = (
            f"Supervisor routing to agents: {', '.join(agents)} "
            f"(confidence {confidence:.0%})"
        )
    else:
        msg = f"Supervisor: no agents required (confidence {confidence:.0%})"

    emit_event(
        email_id   = email_id,
        event_type = "routing_decision",
        agent_name = "supervisor",
        status     = "info",
        message    = msg,
        meta       = {
            "route":      route,
            "confidence": round(confidence, 3),
            "agents":     agents,
        },
    )


def emit_agent_invoked(email_id: str, agent_name: str) -> None:
    emit_event(
        email_id   = email_id,
        event_type = "agent_invoked",
        agent_name = agent_name,
        status     = "running",
        message    = f"{agent_name} invoked",
    )


def emit_jira_ticket_created(
    email_id: str,
    issue_key: str,
    assignee: str,
    issue_type: str,
    priority: str,
) -> None:
    emit_event(
        email_id   = email_id,
        event_type = "jira_ticket_created",
        agent_name = "jira_agent",
        status     = "success",
        message    = (
            f"Jira ticket {_safe(issue_key)} created "
            f"({issue_type}, {priority}) — assigned to {_safe(assignee)}"
        ),
        meta = {
            "issue_key":  _safe(issue_key),
            "assignee":   _safe(assignee),
            "issue_type": issue_type,
            "priority":   priority,
        },
    )


def emit_calendar_event_created(
    email_id: str,
    slot: str,
    rescheduled: bool,
) -> None:
    action = "rescheduled to" if rescheduled else "scheduled at"
    emit_event(
        email_id   = email_id,
        event_type = "calendar_event_created",
        agent_name = "calendar_agent",
        status     = "success",
        message    = f"Meeting {action} {_safe(slot)}",
        meta       = {"slot": _safe(slot), "rescheduled": rescheduled},
    )


def emit_reply_sent(email_id: str, recipient: str) -> None:
    emit_event(
        email_id   = email_id,
        event_type = "reply_sent",
        agent_name = "reply_agent",
        status     = "success",
        message    = f"Reply sent to {_safe(recipient)}",
        meta       = {"recipient": _safe(recipient)},
    )


def emit_review_queued(email_id: str, confidence: float, reason: str = "") -> None:
    emit_event(
        email_id   = email_id,
        event_type = "review_queued",
        agent_name = "review_queue",
        status     = "info",
        message    = (
            f"Email queued for human review "
            f"(confidence {confidence:.0%})"
            + (f" — {_safe(reason)}" if reason else "")
        ),
        meta = {"confidence": round(confidence, 3)},
    )


def emit_processing_completed(
    email_id: str,
    route: str,
    actions_count: int,
) -> None:
    emit_event(
        email_id   = email_id,
        event_type = "processing_completed",
        agent_name = "pipeline",
        status     = "success",
        message    = (
            f"Processing completed — route: {route}, "
            f"{actions_count} action(s) taken"
        ),
        meta = {"route": route, "actions_count": actions_count},
    )


def emit_processing_failed(email_id: str, error: str) -> None:
    emit_event(
        email_id   = email_id,
        event_type = "processing_failed",
        agent_name = "pipeline",
        status     = "failure",
        message    = "Processing failed — email moved to review queue",
        meta       = {"error": _safe(error[:200])},
    )
