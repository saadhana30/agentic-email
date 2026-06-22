"""
tracer.py
---------
Execution tracer for LangGraph nodes.

Records per-node timing + success/failure in execution_traces table.
Also emits structured events to execution_events table for the
Live Execution Monitor page.

Usage:
    with trace_node("my_node", state):
        ... actual logic ...
"""

import logging
from contextlib import contextmanager
from datetime import datetime, timezone

from src.database.db import SessionLocal, save_execution_trace

logger = logging.getLogger(__name__)

# Nodes that are purely infrastructure — emit at DEBUG level, still tracked
_INFRA_NODES = {"audit_log", "notification"}


def _get_email_id(state: dict) -> str | None:
    email = state.get("current_email") or {}
    return email.get("message_id")


@contextmanager
def trace_node(node_name: str, state: dict):
    """
    Context manager that:
      1. Records start/end/duration/success in execution_traces table
      2. Emits node_started / node_completed / node_failed to execution_events
    """
    email_id   = _get_email_id(state)
    start_time = datetime.now(timezone.utc)
    exc_msg    = None
    success    = True

    # ── Emit node_started (skip infra nodes to keep monitor clean) ────────────
    if node_name not in _INFRA_NODES:
        try:
            from src.monitor import emit_node_started
            emit_node_started(email_id, node_name)
        except Exception:
            pass

    try:
        yield
    except Exception as exc:
        success = False
        exc_msg = str(exc)
        logger.error(f"Tracer [{node_name}]: exception — {exc_msg}")
        raise
    finally:
        end_time    = datetime.now(timezone.utc)
        duration_ms = (end_time - start_time).total_seconds() * 1000

        status_sym = "✓" if success else "✗"
        logger.info(
            f"Tracer {status_sym} [{node_name}] "
            f"email={email_id} "
            f"duration={duration_ms:.0f}ms"
        )

        # ── Persist to execution_traces ───────────────────────────────────────
        try:
            db = SessionLocal()
            try:
                save_execution_trace(db, {
                    "email_id":          email_id,
                    "node_name":         node_name,
                    "start_time":        start_time,
                    "end_time":          end_time,
                    "duration_ms":       round(duration_ms, 2),
                    "success":           success,
                    "exception_message": exc_msg,
                })
            finally:
                db.close()
        except Exception as db_exc:
            logger.warning(f"Tracer: failed to persist trace for {node_name} — {db_exc}")

        # ── Emit to execution_events (monitor) ────────────────────────────────
        if node_name not in _INFRA_NODES:
            try:
                if success:
                    from src.monitor import emit_node_completed
                    emit_node_completed(email_id, node_name, round(duration_ms, 2))
                else:
                    from src.monitor import emit_node_failed
                    emit_node_failed(email_id, node_name, round(duration_ms, 2),
                                     exc_msg or "unknown error")
            except Exception:
                pass
