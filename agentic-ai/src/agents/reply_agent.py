"""
ReplyAgent
----------
Drafts and sends a reply email within the same Gmail thread.
Uses LLM to generate a professional reply based on actions taken.
Handles rescheduled meeting notifications in the reply.
"""

import base64
import logging
import time
from email.mime.text import MIMEText

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from src.tools.ollama_client import call_ollama

from src.config import (
    GOOGLE_TOKEN_FILE, GMAIL_SCOPES, COMPANY_EMAIL,
    MAX_RETRIES, RETRY_DELAY_SECONDS
)
from src.graph.state import AgentState

logger = logging.getLogger(__name__)

REPLY_PROMPT = """You are a professional assistant writing a reply email on behalf of the company.

Original Email:
From: {sender}
Subject: {subject}
Body: {body}

{conversation_section}

Actions taken:
{actions_summary}

Write a concise, professional reply email.
- Thank the client for reaching out
- Confirm what actions were taken
- If a meeting was rescheduled, mention the new proposed time
- If this is a follow-up in an ongoing conversation, acknowledge the previous discussion naturally
- If the client said thanks or just acknowledged, keep the reply brief and warm
- Keep it under 150 words
- Do NOT include subject line, just the body text
"""


class ReplyAgent:

    def __init__(self):
        # Lazy init — token.json loaded only when run() is called
        self._gmail_service = None

    def _get_service(self):
        if self._gmail_service is None:
            creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, GMAIL_SCOPES)
            self._gmail_service = build("gmail", "v1", credentials=creds)
        return self._gmail_service

    def run(self, state: AgentState) -> AgentState:
        """
        Generate and send a reply in the same Gmail thread.
        """
        if "reply_agent" not in state.get("next_agents", []):
            return state

        email = state.get("current_email", {})
        actions_taken = state.get("actions_taken", [])

        actions_summary = self._build_actions_summary(state, actions_taken)
        reply_body = self._generate_reply(email, actions_summary, state)
        result = self._send_reply(email, reply_body)

        actions = state.get("actions_taken", [])
        actions.append({"agent": "reply_agent", "result": result})
        state["actions_taken"] = actions

        return state

    def _build_actions_summary(self, state: AgentState, actions_taken: list) -> str:
        parts = []

        # If jira_status_info is present, include live ticket data in reply
        jira_status = state.get("jira_status_info")
        if jira_status and not jira_status.get("error"):
            parts.append(
                f"- Status update for ticket {jira_status.get('ticket_key', '')}:\n"
                f"  Status: {jira_status.get('status', 'Unknown')}\n"
                f"  Assigned to: {jira_status.get('assignee', 'Unassigned')}\n"
                f"  Priority: {jira_status.get('priority', 'None')}\n"
                f"  Latest update: {jira_status.get('latest_comment', 'No comments yet')}\n"
                f"  View ticket: {jira_status.get('ticket_url', '')}"
            )

        for action in actions_taken:
            agent = action.get("agent")
            res = action.get("result", {})
            if agent == "jira_agent" and res.get("status") == "success":
                parts.append(f"- Jira ticket {res.get('issue_key')} created: {res.get('issue_url')}")
            elif agent == "calendar_agent" and res.get("status") == "success":
                slot = res.get("slot", "")
                rescheduled = res.get("rescheduled", False)
                if rescheduled:
                    parts.append(f"- Meeting could not be scheduled at requested time. Proposed new slot: {slot}")
                else:
                    parts.append(f"- Meeting scheduled at: {slot}")

        if not parts:
            parts.append("- Your request has been received and noted")

        return "\n".join(parts)

    def _generate_reply(self, email: dict, actions_summary: str, state: AgentState) -> str:
        # Build conversation section from thread_context summary if available
        thread_context = state.get("thread_context", {})
        conversation_summary = thread_context.get("conversation_summary", "")
        if conversation_summary:
            conversation_section = (
                "Conversation history (for context only — do not repeat verbatim):\n"
                + conversation_summary
            )
        else:
            conversation_section = ""

        prompt = REPLY_PROMPT.format(
            sender               = email.get("sender", ""),
            subject              = email.get("subject", ""),
            body                 = email.get("processed_content", "")[:500],
            conversation_section = conversation_section,
            actions_summary      = actions_summary,
        )
        try:
            return call_ollama(prompt, temperature=0.4)
        except Exception as e:
            logger.error(f"ReplyAgent: LLM reply generation failed — {e}")
            return "Thank you for your email. We have received your request and will get back to you shortly."

    def _send_reply(self, email: dict, reply_body: str) -> dict:
        """Send reply within the same Gmail thread."""
        msg = MIMEText(reply_body)
        msg["To"] = email.get("sender", "")
        msg["From"] = COMPANY_EMAIL
        msg["Subject"] = f"Re: {email.get('subject', '')}"
        msg["In-Reply-To"] = email.get("message_id", "")
        msg["References"] = email.get("message_id", "")

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

        for attempt in range(MAX_RETRIES + 1):
            try:
                sent = self._get_service().users().messages().send(
                    userId="me",
                    body={
                        "raw": raw,
                        "threadId": email.get("thread_id", ""),   # same thread
                    }
                ).execute()
                logger.info(f"ReplyAgent: reply sent — message_id={sent.get('id')}")
                return {"status": "success", "message_id": sent.get("id")}
            except Exception as e:
                logger.warning(f"ReplyAgent: attempt {attempt + 1} failed — {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SECONDS)

        return {"status": "failed", "error": "Reply send failed after retries"}
