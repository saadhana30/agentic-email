"""
poller.py
---------
Background polling loop — runs every POLL_INTERVAL_SECONDS.

On FIRST run: records all current unread email IDs in DB as "seen"
              but does NOT process them. Only truly NEW emails get processed.

On SUBSEQUENT runs: fetches unread emails, skips any already in DB,
                    processes only new ones.

For each new email:
  1. Dedup check (message_id in DB → skip if seen)
  2. INVOKE LangGraph → which invokes each agent in sequence
  3. Mark email as read in Gmail

Thread reply emails are processed normally — EmailAnalysisAgent injects
the full thread history into the LLM prompt automatically.
"""

import time
import logging
from src.config import POLL_INTERVAL_SECONDS
from src.agents.email_monitoring_agent import EmailMonitoringAgent
from src.database.db import (
    SessionLocal,
    is_duplicate_email,
    is_duplicate_recent_email,
    save_email,
    emit_event,
)
from src.graph.workflow import run_graph_for_email

logger = logging.getLogger(__name__)


def _seed_existing_emails(monitor: EmailMonitoringAgent):
    """
    On first startup: mark all currently unread emails as seen
    WITHOUT processing them. So only future new emails get processed.
    """
    logger.info(
        "Poller: first run — seeding existing unread emails as seen "
        "(will not process them)"
    )
    state = {"raw_emails": [], "error": None}
    state = monitor.run(state)
    emails = state.get("raw_emails", [])

    db = SessionLocal()
    try:
        for email in emails:
            if not is_duplicate_email(db, email["message_id"]):
                save_email(db, {
                    "id":              email["message_id"],
                    "thread_id":       email.get("thread_id", ""),
                    "in_reply_to":     email.get("in_reply_to"),
                    "references":      email.get("references"),
                    "sender":          email.get("sender", ""),
                    "sender_domain":   email.get("sender_domain", ""),
                    "subject":         email.get("subject", ""),
                    "received_at":     email.get("received_at"),
                    "attachment_type": email.get("attachment_type", "text"),
                    "raw_content":     "",
                    "is_spam":         False,
                    "client_name":     None,
                    "processed":       True,   # skip on next poll
                })
        logger.info(
            f"Poller: seeded {len(emails)} existing emails — "
            "they will not be processed"
        )
    finally:
        db.close()


def run_poller():
    logger.info(
        f"Poller started — checking inbox every {POLL_INTERVAL_SECONDS}s"
    )
    monitor = EmailMonitoringAgent()

    # Seed existing unread emails on first run
    _seed_existing_emails(monitor)

    logger.info("Poller: now listening for NEW incoming emails only...")

    while True:
        try:
            state = {"raw_emails": [], "error": None}
            state = monitor.run(state)
            emails = state.get("raw_emails", [])

            db = SessionLocal()
            try:
                new_emails = [
                    e for e in emails
                    if not is_duplicate_email(db, e["message_id"])
                ]
            finally:
                db.close()

            if new_emails:
                logger.info(f"Poller: {len(new_emails)} new email(s) detected")

            for email in new_emails:
                try:
                    # Thread replies must always be processed normally
                    if not email.get("in_reply_to"):
                        db = SessionLocal()
                        try:
                            content = email.get("processed_content") or email.get("raw_content", "")
                            if is_duplicate_recent_email(
                                db,
                                sender=email.get("sender", ""),
                                subject=email.get("subject", ""),
                                content=content,
                                received_at=email.get("received_at"),
                                window_minutes=10,
                            ):
                                logger.info("Duplicate email detected")
                                # Emit a structured execution event for the Live Monitor UI
                                try:
                                    emit_event(
                                        email.get("message_id"),
                                        "duplicate_email_detected",
                                        "Duplicate email detected — processing skipped",
                                        agent_name=None,
                                        status="info",
                                        meta={
                                            "sender": email.get("sender", ""),
                                            "subject": email.get("subject", ""),
                                        },
                                    )
                                except Exception:
                                    # emit_event is resilient, but don't let failures block polling
                                    logger.exception("Failed to emit duplicate_email_detected event")
                                monitor.mark_as_read(email["message_id"])
                                continue
                        finally:
                            db.close()

                    logger.info(
                        f"Poller: invoking graph for email "
                        f"[{email.get('message_id')}] from {email.get('sender')}"
                    )
                    final_state = run_graph_for_email(email)
                    monitor.mark_as_read(email["message_id"])
                    logger.info(
                        f"Poller: completed [{email.get('message_id')}] "
                        f"route={final_state.get('route')} "
                        f"agents={final_state.get('next_agents')} "
                        f"thread_reply={final_state.get('is_thread_reply', False)}"
                    )
                except Exception as e:
                    logger.error(
                        f"Poller: graph failed for "
                        f"[{email.get('message_id')}] — {e}"
                    )

        except Exception as e:
            logger.error(f"Poller: unexpected error — {e}")

        time.sleep(POLL_INTERVAL_SECONDS)
