"""
poller.py
---------
Background polling loop — runs every POLL_INTERVAL_SECONDS.

On FIRST run: records all current unread email IDs in DB as "seen"
              but does NOT process them. This ensures only truly NEW
              incoming emails are processed going forward.

On SUBSEQUENT runs: fetches unread emails, skips any already in DB,
                    processes only new ones.

For each new email:
  1. Dedup check (message_id in DB → skip if seen)
  2. INVOKE LangGraph → which invokes each agent in sequence
  3. Mark email as read in Gmail
"""

import time
import logging
from src.config import POLL_INTERVAL_SECONDS
from src.agents.email_monitoring_agent import EmailMonitoringAgent
from src.database.db import SessionLocal, is_duplicate_email, save_email
from src.graph.workflow import run_graph_for_email   # ← this is where agents are invoked

logger = logging.getLogger(__name__)


def _seed_existing_emails(monitor: EmailMonitoringAgent):
    """
    On first startup: mark all currently unread emails as seen
    WITHOUT processing them. So only future new emails get processed.
    """
    logger.info("Poller: first run — seeding existing unread emails as seen (will not process them)")
    state = {"raw_emails": [], "error": None}
    state = monitor.run(state)
    emails = state.get("raw_emails", [])

    db = SessionLocal()
    try:
        for email in emails:
            if not is_duplicate_email(db, email["message_id"]):
                # Save minimal record — mark as processed=True so they're skipped
                save_email(db, {
                    "id": email["message_id"],
                    "thread_id": email.get("thread_id", ""),
                    "sender": email.get("sender", ""),
                    "sender_domain": email.get("sender_domain", ""),
                    "subject": email.get("subject", ""),
                    "received_at": email.get("received_at"),
                    "attachment_type": email.get("attachment_type", "text"),
                    "raw_content": "",
                    "is_spam": False,
                    "client_name": None,
                    "processed": True,   # mark as processed so poller skips it
                })
        logger.info(f"Poller: seeded {len(emails)} existing emails — they will not be processed")
    finally:
        db.close()


def run_poller():
    logger.info(f"Poller started — will check inbox every {POLL_INTERVAL_SECONDS}s")
    monitor = EmailMonitoringAgent()

    # Seed existing emails on first run — skip them, only process new ones
    _seed_existing_emails(monitor)

    logger.info("Poller: now listening for NEW incoming emails only...")

    while True:
        try:
            # ── Fetch current unread emails ───────────────────────────────
            state = {"raw_emails": [], "error": None}
            state = monitor.run(state)
            emails = state.get("raw_emails", [])

            # ── Dedup: keep only emails NOT already in DB ─────────────────
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

            # ── For each new email: INVOKE agents via LangGraph ───────────
            for email in new_emails:
                logger.info(
                    f"Poller: invoking agents for email "
                    f"[{email.get('message_id')}] from {email.get('sender')}"
                )
                try:
                    # ════════════════════════════════════════════════════
                    # THIS IS WHERE ALL AGENTS ARE INVOKED
                    # run_graph_for_email() calls compiled_graph.invoke()
                    # which runs every agent node in the LangGraph graph:
                    #   EmailAnalysisAgent → SupervisorAgent →
                    #   JiraAgent / CalendarAgent / ReplyAgent
                    # ════════════════════════════════════════════════════
                    final_state = run_graph_for_email(email)

                    # Mark as read in Gmail after successful processing
                    monitor.mark_as_read(email["message_id"])

                    logger.info(
                        f"Poller: agents completed for [{email.get('message_id')}] "
                        f"— route={final_state.get('route')}, "
                        f"agents_run={final_state.get('next_agents')}, "
                        f"actions={[a['agent'] for a in final_state.get('actions_taken', [])]}"
                    )

                except Exception as e:
                    logger.error(
                        f"Poller: agent invocation failed for "
                        f"[{email.get('message_id')}] — {e}"
                    )

        except Exception as e:
            logger.error(f"Poller: unexpected error — {e}")

        time.sleep(POLL_INTERVAL_SECONDS)
