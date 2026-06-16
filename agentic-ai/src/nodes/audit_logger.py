"""
AuditLoggerNode
---------------
Persists all processing results to SQLite:
  - Email record (with thread metadata)
  - Thread record (upsert)
  - Attachment records (one per attachment)
  - LLM analysis
  - Actions taken by each agent

Called at the end of every graph run (success or failure).
"""

import json
import logging
from src.graph.state import AgentState
from src.database.db import (
    SessionLocal,
    save_email, save_analysis, save_action,
    upsert_thread, save_attachment,
    EmailRecord,
)

logger = logging.getLogger(__name__)


def audit_logger_node(state: AgentState) -> AgentState:
    """
    Log everything to database.
    Called as a LangGraph node.
    """
    email        = state.get("current_email", {})
    analysis     = state.get("analysis", {})
    actions_taken = state.get("actions_taken", [])

    if not email:
        return state

    db = SessionLocal()
    try:
        message_id = email.get("message_id")
        thread_id  = email.get("thread_id", "")

        # ── Save email record (skip if already exists — dedup) ────────────────
        existing = db.query(EmailRecord).filter(EmailRecord.id == message_id).first()
        if not existing:
            save_email(db, {
                "id":              message_id,
                "thread_id":       thread_id,
                "in_reply_to":     email.get("in_reply_to"),
                "references":      email.get("references"),
                "sender":          email.get("sender", ""),
                "sender_domain":   email.get("sender_domain", ""),
                "subject":         email.get("subject", ""),
                "received_at":     email.get("received_at"),
                "attachment_type": email.get("attachment_type", "text"),
                "raw_content":     email.get("processed_content", "")[:5000],
                "is_spam":         state.get("is_spam", False),
                "client_name":     state.get("client_name"),
                "processed":       True,
            })

        # ── Upsert thread record (Feature 1) ──────────────────────────────────
        if thread_id and message_id:
            upsert_thread(
                db,
                thread_id=thread_id,
                message_id=message_id,
                sender=email.get("sender", ""),
                subject=email.get("subject", ""),
                received_at=email.get("received_at"),
            )

        # ── Save attachment records (Feature 3) ───────────────────────────────
        for att in email.get("attachments", []):
            save_attachment(db, {
                "email_id":        message_id,
                "filename":        att.get("filename", ""),
                "mime_type":       att.get("mime_type", ""),
                "attachment_type": att.get("attachment_type", ""),
                "extracted_text":  att.get("extracted_text", ""),
                "char_count":      len(att.get("extracted_text", "")),
            })

        # ── Save LLM analysis ─────────────────────────────────────────────────
        if analysis:
            save_analysis(db, {
                "email_id":       message_id,
                "category":       analysis.get("category", ""),
                "intent":         analysis.get("intent", ""),
                "urgency":        analysis.get("urgency", ""),
                "confidence":     analysis.get("confidence", 0.0),
                "required_agents": json.dumps(analysis.get("required_agents", [])),
                "execution_plan":  json.dumps(analysis.get("execution_plan", [])),
            })

        # ── Save each action ──────────────────────────────────────────────────
        for action in actions_taken:
            result = action.get("result", {})
            save_action(db, {
                "email_id":      message_id,
                "agent_name":    action.get("agent", ""),
                "action_taken":  json.dumps(result),
                "status":        result.get("status", "unknown"),
                "error_message": result.get("error"),
            })

        logger.info(f"AuditLoggerNode: logged email {message_id}")

    except Exception as e:
        logger.error(f"AuditLoggerNode: DB write failed — {e}")
    finally:
        db.close()

    return state
