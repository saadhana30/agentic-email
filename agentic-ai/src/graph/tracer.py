"""
tracer.py
---------
Execution tracer for LangGraph nodes.

Usage — wrap any node function or agent .run() method:

    from src.graph.tracer import trace_node

    def my_node(state: AgentState) -> AgentState:
        with trace_node("my_node", state):
            ... actual logic ...

Or use the decorator form:

    @traced("my_node")
    def my_node(state: AgentState) -> AgentState:
        ...

Records: node_name, start_time, end_time, duration_ms, success,
         exception_message — persisted to execution_traces table.
"""

import logging
from contextlib import contextmanager
from datetime import datetime, timezone

from src.database.db import SessionLocal, save_execution_trace

logger = logging.getLogger(__name__)


def _get_email_id(state: dict) -> str | None:
    email = state.get("current_email") or {}
    return email.get("message_id")


@contextmanager
def trace_node(node_name: str, state: dict):
    """
    Context manager that records start/end time and success/failure
    for a LangGraph node into the execution_traces table.
    """
    email_id   = _get_email_id(state)
    start_time = datetime.now(timezone.utc)
    exc_msg    = None
    success    = True

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
            # Never let tracing failures crash the graph
            logger.warning(f"Tracer: failed to persist trace for {node_name} — {db_exc}")


def make_traced(node_name: str):
    """
    Decorator factory that wraps a node function with trace_node.

    Example:
        @make_traced("email_processor")
        def email_processor_node(state):
            ...
    """
    def decorator(fn):
        def wrapper(state):
            with trace_node(node_name, state):
                return fn(state)
        wrapper.__name__ = fn.__name__
        return wrapper
    return decorator
