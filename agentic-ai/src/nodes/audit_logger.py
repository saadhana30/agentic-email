"""
AuditLoggerNode
---------------
Persists all processing results to SQLite:
- Email record
- LLM analysis
- Actions taken by each agent
Called at the end of every graph run (success or failure).
"""

import json
import logging
from src.graph.state import AgentState
from src.database.db import (
    SessionLocal, save_email, save_analysis, save_action, EmailRecord
)

logger = logging.getLogger(__name__)


def audit_logger_node(state: AgentState) -> AgentState:
    """
    Log everything to database.
    Called as a LangGraph node.
    """
    email = state.get("current_email", {})
    analysis = state.get("analysis", {})
    actions_taken = state.get("actions_taken", [])

    if not email:
        return state

    db = SessionLocal()
    try:
        # Save email record (skip if already exists — dedup)
        existing = db.query(EmailRecord).filter(EmailRecord.id == email.get("message_id")).first()
        if not existing:
            save_email(db, {
                "id": email.get("message_id"),
                "thread_id": email.get("thread_id", ""),
                "sender": email.get("sender", ""),
                "sender_domain": email.get("sender_domain", ""),
                "subject": email.get("subject", ""),
                "received_at": email.get("received_at"),
                "attachment_type": email.get("attachment_type", "text"),
                "raw_content": email.get("processed_content", "")[:5000],
                "is_spam": state.get("is_spam", False),
                "client_name": state.get("client_name"),
                "processed": True,
            })

        # Save LLM analysis
        if analysis:
            save_analysis(db, {
                "email_id": email.get("message_id"),
                "category": analysis.get("category", ""),
                "intent": analysis.get("intent", ""),
                "urgency": analysis.get("urgency", ""),
                "confidence": analysis.get("confidence", 0.0),
                "required_agents": json.dumps(analysis.get("required_agents", [])),
                "execution_plan": json.dumps(analysis.get("execution_plan", [])),
            })

        # Save each action
        for action in actions_taken:
            result = action.get("result", {})
            save_action(db, {
                "email_id": email.get("message_id"),
                "agent_name": action.get("agent", ""),
                "action_taken": json.dumps(result),
                "status": result.get("status", "unknown"),
                "error_message": result.get("error"),
            })

        logger.info(f"AuditLoggerNode: logged email {email.get('message_id')}")
    except Exception as e:
        logger.error(f"AuditLoggerNode: DB write failed — {e}")
    finally:
        db.close()

    return state
