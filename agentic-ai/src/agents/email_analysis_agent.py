"""
EmailAnalysisAgent
------------------
Sends processed email content to Ollama (llama3.2:latest).
Returns structured JSON: category, intent, urgency, confidence,
required_agents, execution_plan.

Feature additions:
  1. Thread Awareness — when the email is a reply in an existing thread,
     fetches all previous messages in the Gmail thread and injects them
     as conversation history so the LLM understands context.
  2. Ollama Retry — OllamaRetryExhausted is caught; email is sent to
     review_queue with error recorded in state rather than crashing.
"""

import json
import logging
from src.tools.ollama_client import call_ollama, OllamaRetryExhausted
from src.graph.state import AgentState
from src.database.db import (
    SessionLocal, EmailRecord, AnalysisRecord, ActionRecord,
    get_thread_emails,
)

logger = logging.getLogger(__name__)

ANALYSIS_PROMPT = """You are an AI assistant managing client emails for a company.

{history_section}

{thread_section}

Analyze the following NEW email and return a JSON response ONLY. No explanation, no extra text.

Email Details:
Sender: {sender}
Subject: {subject}
Body:
{body}

Return this exact JSON structure:
{{
  "category": "technical_issue" or "meeting_request" or "task_request" or "complaint" or "status_enquiry" or "general_inquiry" or "other",
  "intent": "one clear sentence describing what the sender wants",
  "urgency": "high" or "medium" or "low",
  "confidence": <float between 0.0 and 1.0>,
  "required_agents": ["jira_agent", "calendar_agent", "reply_agent", "jira_status_agent"],
  "execution_plan": ["step 1", "step 2"]
}}

Rules:
- category must be EXACTLY ONE value from: technical_issue, meeting_request, task_request, complaint, status_enquiry, general_inquiry, other
- Do NOT combine categories with | or / — pick the single best one
- required_agents must only contain values from: jira_agent, calendar_agent, reply_agent, jira_status_agent
- Use jira_status_agent when the client is asking for a status update on an existing request or ticket
- Include only the agents actually needed for this email
- confidence above 0.75 means you are certain about what action is needed
- If the email is vague, ambiguous, or unclear, set confidence below 0.75
- urgency "high" = needs action today, "medium" = within a few days, "low" = no rush
- If history or thread context shows a previous ticket for the same issue, reference it in the execution plan
- Return ONLY valid JSON, nothing else
"""


class EmailAnalysisAgent:

    def __init__(self):
        # Lazily imported to avoid circular deps at module load
        self._monitor = None

    def _get_monitor(self):
        if self._monitor is None:
            from src.agents.email_monitoring_agent import EmailMonitoringAgent
            self._monitor = EmailMonitoringAgent()
        return self._monitor

    def run(self, state: AgentState) -> AgentState:
        email = state.get("current_email", {})
        if not email:
            state["analysis"] = {}
            return state

        # ── Build history section from client domain memory ────────────────
        history_section = self._get_client_history(email.get("sender_domain", ""))

        # ── Build thread section from Gmail thread (Feature 1) ────────────
        thread_section  = ""
        if state.get("is_thread_reply"):
            thread_msgs = self._get_thread_history(email)
            if thread_msgs:
                state["thread_history"] = thread_msgs
                thread_section = self._format_thread_section(thread_msgs)

        prompt = ANALYSIS_PROMPT.format(
            history_section=history_section,
            thread_section=thread_section,
            sender=email.get("sender", "unknown"),
            subject=email.get("subject", ""),
            body=email.get("processed_content", email.get("raw_content", ""))[:3000],
        )

        logger.info(
            f"EmailAnalysisAgent: analyzing email from {email.get('sender')} "
            f"(thread_reply={state.get('is_thread_reply', False)})"
        )

        try:
            raw_text = call_ollama(prompt, temperature=0.1)

            # Strip markdown code fences if present
            if raw_text.startswith("```"):
                raw_text = raw_text.split("```")[1]
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:]
                raw_text = raw_text.strip()

            analysis = json.loads(raw_text)

            required_fields = [
                "category", "intent", "urgency",
                "confidence", "required_agents", "execution_plan",
            ]
            for field in required_fields:
                if field not in analysis:
                    raise ValueError(f"Missing field in LLM response: {field}")

            state["analysis"] = analysis
            state["error"]    = None
            logger.info(
                f"EmailAnalysisAgent: confidence={analysis['confidence']}, "
                f"agents={analysis['required_agents']}"
            )

        except OllamaRetryExhausted as e:
            # All retries exhausted — route to review queue so no email is lost
            logger.error(f"EmailAnalysisAgent: Ollama retries exhausted — {e}")
            state["analysis"] = {
                "category":       "other",
                "intent":         "LLM unavailable — manual review required",
                "urgency":        "medium",
                "confidence":     0.0,    # forces supervisor to send to review_queue
                "required_agents": [],
                "execution_plan": [],
            }
            state["error"] = f"Ollama retry exhausted: {e}"

        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"EmailAnalysisAgent: failed to parse LLM response — {e}")
            state["analysis"] = {
                "category":       "other",
                "intent":         "Could not determine intent",
                "urgency":        "low",
                "confidence":     0.0,
                "required_agents": [],
                "execution_plan": [],
            }

        return state

    # ── Thread history (Feature 1) ────────────────────────────────────────────

    def _get_thread_history(self, email: dict) -> list[dict]:
        """
        Fetch previous messages in the same Gmail thread.
        Excludes the current message itself.
        """
        thread_id  = email.get("thread_id", "")
        message_id = email.get("message_id", "")
        if not thread_id:
            return []
        try:
            monitor = self._get_monitor()
            msgs = monitor.fetch_thread_messages(thread_id)
            # Exclude the current message
            return [m for m in msgs if m.get("message_id") != message_id]
        except Exception as e:
            logger.warning(f"EmailAnalysisAgent: thread history fetch failed — {e}")
            return []

    def _format_thread_section(self, thread_msgs: list[dict]) -> str:
        if not thread_msgs:
            return ""
        lines = [
            "This email is part of an ongoing conversation thread. "
            "Previous messages in the thread (oldest first):"
        ]
        for msg in thread_msgs[-5:]:   # last 5 messages for context
            ts = msg.get("received_at")
            date_str = ts.strftime("%Y-%m-%d %H:%M") if ts else "unknown date"
            lines.append(
                f'- [{date_str}] From: {msg["sender"]} | '
                f'Subject: "{msg["subject"]}" | '
                f'Preview: {msg["body"][:200]}'
            )
        return "\n".join(lines) + "\n"

    # ── Client domain memory ──────────────────────────────────────────────────

    def _get_client_history(self, sender_domain: str) -> str:
        """
        Fetch last 3 emails + their analysis + actions for this client domain.
        Returns formatted history string to inject into prompt.
        """
        if not sender_domain:
            return ""
        try:
            db = SessionLocal()
            try:
                past_emails = (
                    db.query(EmailRecord)
                    .filter(
                        EmailRecord.sender_domain == sender_domain,
                        EmailRecord.is_spam == False,
                    )
                    .order_by(EmailRecord.created_at.desc())
                    .limit(3)
                    .all()
                )

                if not past_emails:
                    return ""

                lines = ["Previous interactions with this client:"]
                for e in past_emails:
                    date_str = (
                        e.created_at.strftime("%Y-%m-%d")
                        if e.created_at else "unknown date"
                    )
                    analysis = (
                        db.query(AnalysisRecord)
                        .filter(AnalysisRecord.email_id == e.id)
                        .first()
                    )
                    category = analysis.category if analysis else "unknown"

                    actions = (
                        db.query(ActionRecord)
                        .filter(
                            ActionRecord.email_id == e.id,
                            ActionRecord.status == "success",
                        )
                        .all()
                    )
                    action_summaries = []
                    for a in actions:
                        import json as _json
                        result = _json.loads(a.action_taken or "{}")
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

                    action_str = (
                        ", ".join(action_summaries)
                        if action_summaries else "no actions taken"
                    )
                    lines.append(
                        f'- [{date_str}] Subject: "{e.subject}" '
                        f'→ Category: {category} → Action: {action_str}'
                    )

                return "\n".join(lines) + "\n"
            finally:
                db.close()
        except Exception as ex:
            logger.warning(
                f"EmailAnalysisAgent: could not fetch client history — {ex}"
            )
            return ""
