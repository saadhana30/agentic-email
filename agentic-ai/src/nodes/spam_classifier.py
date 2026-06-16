"""
SpamClassifierNode
------------------
Classifies email as:
  - spam     → stop processing
  - client   → known client, set jira_project_key
  - other    → unknown sender, process but no Jira

Also detects thread replies (Feature 1):
  Sets state["is_thread_reply"] = True when in_reply_to is present
  AND the thread already exists in our DB.
"""

import logging
from src.graph.state import AgentState
from src.database.db import get_client_by_domain, get_thread, SessionLocal

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
        state["is_spam"]        = True
        state["is_thread_reply"] = False
        state["thread_history"]  = []
        return state

    sender        = email.get("sender",        "").lower()
    sender_domain = email.get("sender_domain", "").lower()
    subject       = email.get("subject",       "").lower()
    thread_id     = email.get("thread_id",     "")
    in_reply_to   = email.get("in_reply_to")

    # ── Spam check ────────────────────────────────────────────────────────────
    if _is_spam(sender, subject):
        state["is_spam"]         = True
        state["is_thread_reply"] = False
        state["thread_history"]  = []
        state["client_name"]     = None
        state["jira_project_key"] = None
        logger.info(f"SpamClassifierNode: spam detected from {sender}")
        return state

    # ── Thread reply detection (Feature 1) ────────────────────────────────────
    is_thread_reply = False
    if in_reply_to and thread_id:
        db = SessionLocal()
        try:
            existing_thread = get_thread(db, thread_id)
            if existing_thread:
                is_thread_reply = True
                logger.info(
                    f"SpamClassifierNode: thread reply detected — "
                    f"thread_id={thread_id}"
                )
        finally:
            db.close()

    state["is_thread_reply"] = is_thread_reply
    state["thread_history"]  = []   # populated by EmailAnalysisAgent

    # ── Client domain lookup ──────────────────────────────────────────────────
    db = SessionLocal()
    try:
        client = get_client_by_domain(db, sender_domain)
        if not client:
            client = get_client_by_domain(db, sender)

        if client:
            state["is_spam"]          = False
            state["client_name"]      = client.name
            state["jira_project_key"] = client.jira_project_key
            state["email_source"]     = "client"
            logger.info(
                f"SpamClassifierNode: client email — {client.name} ({sender_domain})"
            )
        else:
            state["is_spam"]          = False
            state["client_name"]      = None
            state["jira_project_key"] = None
            state["email_source"]     = "other"
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
