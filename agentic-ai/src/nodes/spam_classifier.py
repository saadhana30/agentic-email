"""
SpamClassifierNode
------------------
Classifies email as:
  - spam     → stop processing
  - client   → known client, set jira_project_key
  - other    → unknown sender, process but no Jira

Uses DB client table to match sender domain.
Also checks common spam signals (no-reply, marketing keywords, etc.).
"""

import logging
import re
from src.graph.state import AgentState
from src.database.db import get_client_by_domain, SessionLocal

logger = logging.getLogger(__name__)

SPAM_SIGNALS = [
    "no-reply", "noreply", "donotreply", "do-not-reply",
    "unsubscribe", "newsletter", "promotions", "marketing",
    "notification@", "updates@", "alerts@",
]

SPAM_SUBJECT_KEYWORDS = [
    "unsubscribe", "you won", "congratulations", "free offer",
    "limited time", "click here", "verify your account",
    "act now", "urgent action required",
]


def spam_classifier_node(state: AgentState) -> AgentState:
    """
    Classify email source and set routing flags.
    Called as a LangGraph node.
    """
    email = state.get("current_email", {})
    if not email:
        state["is_spam"] = True
        return state

    sender = email.get("sender", "").lower()
    sender_domain = email.get("sender_domain", "").lower()
    subject = email.get("subject", "").lower()

    # Check spam signals
    if _is_spam(sender, subject):
        state["is_spam"] = True
        state["client_name"] = None
        state["jira_project_key"] = None
        logger.info(f"SpamClassifierNode: spam detected from {sender}")
        return state

    # Check if sender is a known client
    # Try domain match first, then full email match (in case full email was entered as domain)
    db = SessionLocal()
    try:
        client = get_client_by_domain(db, sender_domain)
        if not client:
            client = get_client_by_domain(db, sender)  # fallback: match full email
        if client:
            state["is_spam"] = False
            state["client_name"] = client.name
            state["jira_project_key"] = client.jira_project_key
            state["email_source"] = "client"
            logger.info(f"SpamClassifierNode: client email — {client.name} ({sender_domain})")
        else:
            state["is_spam"] = False
            state["client_name"] = None
            state["jira_project_key"] = None
            state["email_source"] = "other"
            logger.info(f"SpamClassifierNode: unknown sender — {sender_domain}")
    finally:
        db.close()

    return state


def _is_spam(sender: str, subject: str) -> bool:
    for signal in SPAM_SIGNALS:
        if signal in sender:
            return True
    for keyword in SPAM_SUBJECT_KEYWORDS:
        if keyword in subject:
            return True
    return False
