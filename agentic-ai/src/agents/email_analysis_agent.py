"""
EmailAnalysisAgent
------------------
Sends processed email content to Ollama (llama3.2:latest).
Returns structured JSON: category, intent, urgency, confidence,
required_agents, execution_plan.

Also fetches the last 3 emails from the same client domain (conversation memory)
and injects them as context so the LLM understands history.
"""

import json
import logging
from src.tools.ollama_client import call_ollama
from src.graph.state import AgentState
from src.database.db import SessionLocal, EmailRecord, AnalysisRecord, ActionRecord

logger = logging.getLogger(__name__)

ANALYSIS_PROMPT = """You are an AI assistant managing client emails for a company.

{history_section}

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
- If history shows a previous ticket for the same issue, reference it in the execution plan
- Return ONLY valid JSON, nothing else
"""


class EmailAnalysisAgent:

    def __init__(self):
        pass

    def run(self, state: AgentState) -> AgentState:
        email = state.get("current_email", {})
        if not email:
            state["analysis"] = {}
            return state

        # Fetch conversation memory for this client
        history_section = self._get_client_history(email.get("sender_domain", ""))

        prompt = ANALYSIS_PROMPT.format(
            history_section=history_section,
            sender=email.get("sender", "unknown"),
            subject=email.get("subject", ""),
            body=email.get("processed_content", email.get("raw_content", ""))[:3000],
        )

        logger.info(f"EmailAnalysisAgent: analyzing email from {email.get('sender')}")

        try:
            raw_text = call_ollama(prompt, temperature=0.1)

            if raw_text.startswith("```"):
                raw_text = raw_text.split("```")[1]
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:]
                raw_text = raw_text.strip()

            analysis = json.loads(raw_text)

            required_fields = ["category", "intent", "urgency", "confidence", "required_agents", "execution_plan"]
            for field in required_fields:
                if field not in analysis:
                    raise ValueError(f"Missing field in LLM response: {field}")

            state["analysis"] = analysis
            state["error"] = None
            logger.info(f"EmailAnalysisAgent: confidence={analysis['confidence']}, agents={analysis['required_agents']}")

        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"EmailAnalysisAgent: failed to parse LLM response — {e}")
            state["analysis"] = {
                "category": "other",
                "intent": "Could not determine intent",
                "urgency": "low",
                "confidence": 0.0,
                "required_agents": [],
                "execution_plan": [],
            }

        return state

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
                # Get last 3 emails from this domain
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
                    date_str = e.created_at.strftime("%Y-%m-%d") if e.created_at else "unknown date"

                    # Get analysis for this email
                    analysis = db.query(AnalysisRecord).filter(AnalysisRecord.email_id == e.id).first()
                    category = analysis.category if analysis else "unknown"

                    # Get actions taken
                    actions = db.query(ActionRecord).filter(
                        ActionRecord.email_id == e.id,
                        ActionRecord.status == "success"
                    ).all()

                    action_summaries = []
                    for a in actions:
                        result = json.loads(a.action_taken or "{}")
                        if a.agent_name == "jira_agent" and result.get("issue_key"):
                            action_summaries.append(f"Jira ticket {result['issue_key']} created")
                        elif a.agent_name == "calendar_agent" and result.get("slot"):
                            action_summaries.append(f"Meeting scheduled at {result['slot']}")
                        elif a.agent_name == "reply_agent":
                            action_summaries.append("Reply sent")
                        elif a.agent_name == "jira_status_agent":
                            action_summaries.append("Status update provided")

                    action_str = ", ".join(action_summaries) if action_summaries else "no actions taken"
                    lines.append(f'- [{date_str}] Subject: "{e.subject}" → Category: {category} → Action: {action_str}')

                return "\n".join(lines) + "\n"

            finally:
                db.close()

        except Exception as ex:
            logger.warning(f"EmailAnalysisAgent: could not fetch client history — {ex}")
            return ""
