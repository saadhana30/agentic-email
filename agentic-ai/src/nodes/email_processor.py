"""
EmailProcessorNode
------------------
Processes raw email content into clean text for LLM analysis.

What it does:
  - Plain text  → use as-is
  - HTML body   → strip tags and decode entities
  - PDF/image   → attachment text was ALREADY extracted by EmailMonitoringAgent
                  and appended to raw_content; we just clean up any residual HTML.

The dead base64-decode branches for pdf/image have been removed — by the time
this node runs, EmailMonitoringAgent has already extracted all attachment text
and combined it into raw_content.  This node only needs to handle HTML cleanup.
"""

import re
import logging

from src.graph.state import AgentState

logger = logging.getLogger(__name__)


def strip_html(html: str) -> str:
    """Remove HTML tags and decode common entities."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = (
        text
        .replace("&nbsp;", " ")
        .replace("&amp;",  "&")
        .replace("&lt;",   "<")
        .replace("&gt;",   ">")
        .replace("&quot;", '"')
        .replace("&#39;",  "'")
    )
    return re.sub(r"\s+", " ", text).strip()


def email_processor_node(state: AgentState) -> AgentState:
    """
    Process raw email content into clean text.
    Called as a LangGraph node — wrapped with trace_node in workflow.py.
    """
    email = state.get("current_email", {})
    if not email:
        return state

    attachment_type = email.get("attachment_type", "text")
    raw_content     = email.get("raw_content", "")

    if attachment_type == "html":
        processed = strip_html(raw_content)
    else:
        # For text, pdf, image, mixed — raw_content already contains clean
        # body text plus any extracted attachment text from EmailMonitoringAgent.
        # Just strip any residual HTML tags that may have leaked through.
        if "<" in raw_content and ">" in raw_content:
            processed = strip_html(raw_content)
        else:
            processed = raw_content

    email["processed_content"] = processed or raw_content
    state["current_email"] = email

    logger.info(
        f"EmailProcessorNode: processed '{attachment_type}' email "
        f"({len(email['processed_content'])} chars)"
    )
    return state
