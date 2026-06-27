"""
EmailAnalysisAgent
------------------
Sends processed email content to Ollama (llama3.2:latest).
Returns structured JSON: category, intent, urgency, confidence,
required_agents, execution_plan.

Thread-awareness additions:
  - When state["is_thread_reply"] is True, thread_history (loaded by
    SpamClassifierNode) is injected into the LLM prompt as a full
    conversation recap.
  - The LLM can now detect short follow-up messages like "not available",
    "can we reschedule", "thanks", "any progress?" in context.
  - New categories supported: meeting_reschedule, jira_update,
    acknowledgement, follow_up, status_enquiry.
  - thread_context artefacts (existing Jira key, Calendar event) are
    mentioned in the prompt so the LLM can reference them in execution_plan.
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

# ── Analysis prompt ───────────────────────────────────────────────────────────
ANALYSIS_PROMPT = """You are an AI assistant managing client emails for a company.

{history_section}

{thread_section}

{artefacts_section}

Analyze the following NEW email and return a JSON response ONLY. No explanation, no extra text.

Email Details:
Sender: {sender}
Subject: {subject}
Body:
{body}

Return this exact JSON structure:
{{
  "category": "<one value from the list below>",
  "intent": "one clear sentence describing what the sender wants",
  "urgency": "high" or "medium" or "low",
  "confidence": <float 0.0–1.0>,
  "required_agents": ["jira_agent", "calendar_agent", "reply_agent", "jira_status_agent"],
  "execution_plan": ["step 1", "step 2"]
}}

Allowed category values:
  technical_issue    — client reports a bug or technical problem
  meeting_request    — client wants to schedule a new meeting
  meeting_reschedule — client wants to change or cancel an existing meeting
  task_request       — client requests work to be done
  jira_update        — client wants a comment or update added to an existing Jira ticket
  complaint          — client is dissatisfied
  status_enquiry     — client asks for status on an existing ticket or task
  follow_up          — client is following up with no specific new request
  acknowledgement    — client is simply saying thanks, noted, OK, etc.
  general_inquiry    — general question not covered above
  other              — none of the above

Rules:
- category must be EXACTLY ONE value from the list above
- Do NOT combine categories with | or / — pick the single best one
- required_agents must only contain: jira_agent, calendar_agent, reply_agent, jira_status_agent
- For meeting_reschedule: use calendar_agent to update the existing event, reply_agent to confirm
- For jira_update: use jira_agent to add a comment/update, reply_agent to confirm
- For status_enquiry: use jira_status_agent to fetch live ticket status
- For follow_up, acknowledgement: usually only reply_agent is needed
- For acknowledgement with no action needed: empty required_agents is acceptable
- confidence above 0.75 means you are certain about the intent
- If the email is vague, ambiguous, or a very short follow-up, set confidence below 0.75
  UNLESS thread context clarifies the intent, in which case confidence may be higher
- urgency "high" = needs action today, "medium" = within a few days, "low" = no rush
- If thread context shows an existing Jira ticket, reference it by key in execution_plan
- If thread context shows an existing calendar event, reference it in execution_plan
- Return ONLY valid JSON, nothing else

IMPORTANT THREAD RULES:

- Never classify an email as acknowledgement if it contains ANY new request.

- A reply email can still request a meeting, create a Jira task, ask for status, or request work.

- If the email contains words such as:
  schedule,
  meeting,
  tomorrow,
  today,
  Monday,
  Tuesday,
  Wednesday,
  Thursday,
  Friday,
  Saturday,
  Sunday,
  AM,
  PM,
  calendar,
  review meeting

  classify it as meeting_request unless the sender is explicitly changing an existing meeting.

- If the email both requests a meeting and requests work, include BOTH:
  calendar_agent
  jira_agent

- Thread replies MUST be classified from the CONTENT of the latest email, not merely because they are replies.

- acknowledgement means ONLY:
  thanks,
  thank you,
  okay,
  ok,
  noted,
  received,
  understood,
  sounds good,
  great,
  perfect,
  appreciate it

  AND absolutely NO new request exists.
"""


class EmailAnalysisAgent:

    def __init__(self):
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

        is_thread_reply = state.get("is_thread_reply", False)
        thread_history  = state.get("thread_history", [])
        thread_context  = state.get("thread_context", {})

        # ── History section: client domain memory ─────────────────────────────
        history_section = self._get_client_history(email.get("sender_domain", ""))

        # ── Thread section: full conversation history from SQLite ─────────────
        thread_section = ""
        if is_thread_reply and thread_history:
            thread_section = self._format_thread_section(thread_history)

        # ── Artefacts section: existing Jira / Calendar from this thread ──────
        artefacts_section = self._format_artefacts_section(thread_context)

        prompt = ANALYSIS_PROMPT.format(
            history_section  = history_section,
            thread_section   = thread_section,
            artefacts_section= artefacts_section,
            sender  = email.get("sender", "unknown"),
            subject = email.get("subject", ""),
            body    = email.get("processed_content", email.get("raw_content", ""))[:3000],
        )

        logger.info(
            f"EmailAnalysisAgent: analyzing email from {email.get('sender')} "
            f"(thread_reply={is_thread_reply}, "
            f"history_turns={len(thread_history)})"
        )

        try:
            raw_text = call_ollama(prompt, temperature=0.1)

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
                f"EmailAnalysisAgent: category={analysis['category']} "
                f"confidence={analysis['confidence']}, "
                f"agents={analysis['required_agents']}"
            )

        except OllamaRetryExhausted as e:
            logger.error(f"EmailAnalysisAgent: Ollama retries exhausted — {e}")
            state["analysis"] = {
                "category":        "other",
                "intent":          "LLM unavailable — manual review required",
                "urgency":         "medium",
                "confidence":      0.0,
                "required_agents": [],
                "execution_plan":  [],
            }
            state["error"] = f"Ollama retry exhausted: {e}"

        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"EmailAnalysisAgent: failed to parse LLM response — {e}")
            state["analysis"] = {
                "category":        "other",
                "intent":          "Could not determine intent",
                "urgency":         "low",
                "confidence":      0.0,
                "required_agents": [],
                "execution_plan":  [],
            }

        return state

    # ── Thread history section ────────────────────────────────────────────────

    def _format_thread_section(self, thread_history: list) -> str:
        """
        Format the thread history loaded by SpamClassifierNode into a
        structured prompt section. Includes sender, date, intent, and actions.
        """
        lines = [
            "=== CONVERSATION THREAD CONTEXT ===",
            "This email is a REPLY in an ongoing thread. "
            "Read the previous messages below before analysing the new email.",
            "Previous messages (oldest first):",
        ]
        for msg in thread_history[-6:]:   # keep last 6 turns to stay within token budget
            actions_str = (
                ", ".join(msg.get("actions", [])) if msg.get("actions") else "no actions taken"
            )
            intent_str = f" | Intent: {msg['intent']}" if msg.get("intent") else ""
            lines.append(
                f"  [{msg['ts_str']}] From: {msg['sender']}"
                f" | Subject: \"{msg['subject']}\""
                f"{intent_str}"
                f" | Actions: {actions_str}"
                f"\n    Body preview: {msg['body'][:250]}"
            )
        lines.append("=== END OF THREAD CONTEXT ===")
        return "\n".join(lines) + "\n"

    def _format_artefacts_section(self, thread_context: dict) -> str:
        """
        Tell the LLM about existing Jira / Calendar artefacts so it can
        reference them in the execution_plan and choose correct agents.
        """
        if not thread_context:
            return ""
        parts = []
        jira_key = thread_context.get("existing_jira_key")
        event_id = thread_context.get("existing_event_id")
        slot     = thread_context.get("existing_event_slot")

        if jira_key:
            parts.append(
                f"An existing Jira ticket ({jira_key}) was created earlier in this thread. "
                "If the client is asking for an update or comment, reference this ticket key."
            )
        if event_id:
            slot_note = f" (slot: {slot})" if slot else ""
            parts.append(
                f"An existing calendar event{slot_note} was created earlier in this thread. "
                "If the client wants to reschedule, update this event rather than creating a new one."
            )
        if not parts:
            return ""
        return "=== EXISTING ARTEFACTS FROM THIS THREAD ===\n" + "\n".join(parts) + "\n"

    # ── Client domain memory ──────────────────────────────────────────────────

    def _get_client_history(self, sender_domain: str) -> str:
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
                        result = json.loads(a.action_taken or "{}")
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
            logger.warning(f"EmailAnalysisAgent: could not fetch client history — {ex}")
            return ""
