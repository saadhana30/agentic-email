"""
ReviewQueueNode
---------------
When LLM confidence is below threshold, the email is placed in
the review queue (DB) and waits for human approval via the dashboard.
Graph execution stops here for this email.
Human approves/rejects via API → approved emails re-enter the graph
at supervisor_node.
"""

import json
import logging
from src.graph.state import AgentState
from src.database.db import add_to_review_queue, SessionLocal

logger = logging.getLogger(__name__)


def review_queue_node(state: AgentState) -> AgentState:
    """
    Save email + analysis to review queue.
    Sets state["awaiting_review"] = True.
    Called as a LangGraph node.
    """
    email = state.get("current_email", {})
    analysis = state.get("analysis", {})

    db = SessionLocal()
    try:
        review_id = add_to_review_queue(
            db,
            email_id=email.get("message_id", ""),
            analysis=analysis,
        )
        state["awaiting_review"] = True
        state["review_id"] = review_id
        logger.info(f"ReviewQueueNode: email queued for review — review_id={review_id}")
    finally:
        db.close()

    return state
