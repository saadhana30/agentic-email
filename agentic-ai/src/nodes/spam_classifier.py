"""
SpamClassifierNode
------------------
Classifies email as:
  - spam     → stop processing
  - client   → known client, set jira_project_key
  - other    → unknown sender, process but no Jira

Thread-awareness additions:
  - Sets state["is_thread_reply"] = True when in_reply_to is present
    AND the thread already exists in our DB.
  - When is_thread_reply=True, fetches conversation history from SQLite
    and resolves prior Jira / Calendar artefacts via get_thread_prior_actions.
  - Populates state["thread_history"] and state["thread_context"] so every
    downstream agent has the full picture without re-querying the DB.
"""

import json
import logging

from src.graph.state import AgentState
from src.database.db import (
    get_client_by_domain,
    get_thread,
    get_thread_emails,
    get_thread_prior_actions,
    SessionLocal,
    AnalysisRecord,
    ActionRecord,
)

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
        state["thread_context"]  = {}
        return state

    sender        = email.get("sender",        "").lower()
    sender_domain = email.get("sender_domain", "").lower()
    subject       = email.get("subject",       "").lower()
    thread_id     = email.get("thread_id",     "")
    in_reply_to   = email.get("in_reply_to")
    message_id    = email.get("message_id",    "")

    # ── Spam check ────────────────────────────────────────────────────────────
    if _is_spam(sender, subject):
        state["is_spam"]          = True
        state["is_thread_reply"]  = False
        state["thread_history"]   = []
        state["thread_context"]   = {}
        state["client_name"]      = None
        state["jira_project_key"] = None
        logger.info(f"SpamClassifierNode: spam detected from {sender}")
        return state

    # ── Thread reply detection + history loading ──────────────────────────────
    is_thread_reply = False
    thread_history  = []
    thread_context  = {}

    if in_reply_to and thread_id:
        db = SessionLocal()
        try:
            existing_thread = get_thread(db, thread_id)
            if existing_thread:
                is_thread_reply = True
                logger.info(
                    f"SpamClassifierNode: thread reply detected — thread_id={thread_id}"
                )

                # ── Load conversation history from SQLite ──────────────────
                prior_emails = get_thread_emails(
                    db, thread_id, exclude_message_id=message_id
                )
                for e in prior_emails:
                    # Fetch the analysis for this prior email
                    analysis_rec = (
                        db.query(AnalysisRecord)
                        .filter(AnalysisRecord.email_id == e.id)
                        .first()
                    )
                    # Fetch successful actions for this prior email
                    action_recs = (
                        db.query(ActionRecord)
                        .filter(
                            ActionRecord.email_id == e.id,
                            ActionRecord.status == "success",
                        )
                        .all()
                    )
                    action_summaries = []
                    for a in action_recs:
                        try:
                            result = json.loads(a.action_taken or "{}")
                        except Exception:
                            result = {}
                        if a.agent_name == "jira_agent" and result.get("issue_key"):
                            action_summaries.append(
                                f"Jira ticket {result['issue_key']} created"
                            )
                        elif a.agent_name == "calendar_agent" and result.get("slot"):
                            action_summaries.append(
                                f"Meeting scheduled at {result['slot']}"
                            )
                        elif a.agent_name == "reply_agent":
                            action_summaries.append("Reply sent")
                        elif a.agent_name == "jira_status_agent":
                            action_summaries.append("Status update provided")

                    ts = e.received_at.strftime("%Y-%m-%d %H:%M") if e.received_at else "unknown"
                    thread_history.append({
                        "message_id":  e.id,
                        "sender":      e.sender or "",
                        "subject":     e.subject or "",
                        "body":        (e.raw_content or "")[:400],
                        "received_at": e.received_at,
                        "ts_str":      ts,
                        "category":    analysis_rec.category if analysis_rec else "unknown",
                        "intent":      analysis_rec.intent if analysis_rec else "",
                        "actions":     action_summaries,
                    })

                # ── Resolve prior Jira / Calendar artefacts ────────────────
                prior_actions = get_thread_prior_actions(db, thread_id, message_id)
                thread_context = prior_actions   # dict with existing_jira_key etc.

                # Build a plain-text conversation summary for ReplyAgent
                thread_context["conversation_summary"] = _build_conversation_summary(
                    thread_history
                )

                logger.info(
                    f"SpamClassifierNode: thread history loaded — "
                    f"{len(thread_history)} prior message(s), "
                    f"jira={prior_actions.get('existing_jira_key')}, "
                    f"calendar={prior_actions.get('existing_event_id')}"
                )
        except Exception as exc:
            logger.warning(f"SpamClassifierNode: thread history load failed — {exc}")
            is_thread_reply = False
            thread_history  = []
            thread_context  = {}
        finally:
            db.close()

    state["is_thread_reply"] = is_thread_reply
    state["thread_history"]  = thread_history
    state["thread_context"]  = thread_context

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


def _build_conversation_summary(thread_history: list) -> str:
    """
    Build a compact plain-text summary of the thread for ReplyAgent.
    """
    if not thread_history:
        return ""
    lines = ["Previous conversation in this thread:"]
    for msg in thread_history[-6:]:   # last 6 turns is enough for context
        actions_str = (
            ", ".join(msg["actions"]) if msg["actions"] else "no actions"
        )
        lines.append(
            f'  [{msg["ts_str"]}] {msg["sender"]}: '
            f'"{msg["subject"]}" — {actions_str}'
        )
    return "\n".join(lines)


def _is_spam(sender: str, subject: str) -> bool:
    for signal in SPAM_SIGNALS:
        if signal in sender:
            return True
    for keyword in SPAM_SUBJECT_KEYWORDS:
        if keyword in subject:
            return True
    return False
