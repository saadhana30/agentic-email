"""
NotificationNode
----------------
After agents complete their actions, push in-app notifications
to the dashboard via SSE (Server-Sent Events).
Saves notifications to DB so they persist across page refreshes.
"""

import logging
from src.graph.state import AgentState
from src.database.db import add_notification, SessionLocal

logger = logging.getLogger(__name__)


def notification_node(state: AgentState) -> AgentState:
    """
    Create notifications for each completed action.
    Called as a LangGraph node.
    """
    actions_taken = state.get("actions_taken", [])
    if not actions_taken:
        return state

    db = SessionLocal()
    try:
        for action in actions_taken:
            agent = action.get("agent")
            result = action.get("result", {})

            if result.get("status") != "success":
                continue

            if agent == "jira_agent":
                msg = f"✅ Jira ticket {result.get('issue_key')} created — {result.get('issue_url')}"
                notif_type = "jira"
            elif agent == "calendar_agent":
                slot = result.get("slot", "")
                rescheduled = result.get("rescheduled", False)
                if rescheduled:
                    msg = f"📅 Meeting rescheduled — new slot proposed: {slot}"
                else:
                    msg = f"📅 Meeting scheduled at {slot}"
                notif_type = "calendar"
            elif agent == "reply_agent":
                msg = f"📧 Reply sent to {state.get('current_email', {}).get('sender', 'client')}"
                notif_type = "email"
            else:
                continue

            add_notification(db, msg, notif_type)
            logger.info(f"NotificationNode: {msg}")
    finally:
        db.close()

    return state
