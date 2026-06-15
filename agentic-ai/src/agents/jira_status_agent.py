"""
JiraStatusAgent
---------------
Handles status_enquiry emails — when a client asks:
  "What is the status of our bug fix?"
  "Any update on PROJ-42?"
  "Can you tell me where things stand?"

Steps:
1. LLM extracts the ticket key from the email (if mentioned)
2. If no key mentioned, searches Jira for the most recent open ticket
   from that client's project
3. Fetches ticket status, assignee, priority, latest comment via Jira REST API
4. Puts the fetched info into state so ReplyAgent uses it in the reply
"""

import re
import json
import logging
import requests
from requests.auth import HTTPBasicAuth
from src.tools.ollama_client import call_ollama
from src.config import JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN
from src.graph.state import AgentState

logger = logging.getLogger(__name__)

EXTRACT_KEY_PROMPT = """You are a Jira assistant. Read this email and extract the Jira ticket key if mentioned.

Email Subject: {subject}
Email Body: {body}

A Jira ticket key looks like: PROJ-123, ABC-45, SCRUM-7 etc.

Return ONLY a JSON object:
{{
  "ticket_key": "PROJ-123 or null if not mentioned"
}}

Return ONLY valid JSON.
"""


class JiraStatusAgent:

    def __init__(self):
        self.auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)
        self.headers = {"Accept": "application/json"}
        self.base_url = JIRA_URL.rstrip("/")

    def run(self, state: AgentState) -> AgentState:
        """
        AGENT INVOCATION:
        Step 1 — LLM extracts ticket key from email
        Step 2 — Fetch live ticket data from Jira
        Step 3 — Put status info into state for ReplyAgent to use
        """
        if "jira_status_agent" not in state.get("next_agents", []):
            return state

        email = state.get("current_email", {})
        jira_project_key = state.get("jira_project_key")

        logger.info(f"JiraStatusAgent INVOKED for email from {email.get('sender')}")

        # STEP 1: LLM extracts ticket key
        ticket_key = self._extract_ticket_key(email)

        # STEP 2: If no key found, search for most recent open ticket in client project
        if not ticket_key and jira_project_key:
            ticket_key = self._find_latest_ticket(jira_project_key)
            logger.info(f"JiraStatusAgent: no key in email, found latest ticket: {ticket_key}")

        if not ticket_key:
            logger.warning("JiraStatusAgent: could not find any ticket to report on")
            state["jira_status_info"] = {"error": "No ticket found to report status on"}
            actions = state.get("actions_taken", [])
            actions.append({"agent": "jira_status_agent", "result": {"status": "skipped", "reason": "No ticket found"}})
            state["actions_taken"] = actions
            return state

        # STEP 3: Fetch live ticket data from Jira
        ticket_info = self._fetch_ticket(ticket_key)
        logger.info(f"JiraStatusAgent: fetched status for {ticket_key} — {ticket_info.get('status')}")

        # Put into state so ReplyAgent can use it
        state["jira_status_info"] = ticket_info

        actions = state.get("actions_taken", [])
        actions.append({"agent": "jira_status_agent", "result": {
            "status": "success",
            "ticket_key": ticket_key,
            "ticket_status": ticket_info.get("status"),
        }})
        state["actions_taken"] = actions

        return state

    def _extract_ticket_key(self, email: dict) -> str | None:
        """Use LLM to extract ticket key, then fallback to regex."""
        body = email.get("processed_content", "") + " " + email.get("subject", "")

        # Fast regex check first
        match = re.search(r'\b([A-Z][A-Z0-9]+-\d+)\b', body)
        if match:
            return match.group(1)

        # LLM fallback for natural language references
        try:
            prompt = EXTRACT_KEY_PROMPT.format(
                subject=email.get("subject", ""),
                body=email.get("processed_content", "")[:500],
            )
            raw = call_ollama(prompt, temperature=0.0)
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            data = json.loads(raw)
            key = data.get("ticket_key")
            return key if key and key != "null" else None
        except Exception:
            return None

    def _find_latest_ticket(self, project_key: str) -> str | None:
        """Search Jira for most recent open ticket in this project."""
        try:
            url = f"{self.base_url}/rest/api/3/search"
            params = {
                "jql": f"project={project_key} AND statusCategory != Done ORDER BY created DESC",
                "maxResults": 1,
                "fields": "summary",
            }
            r = requests.get(url, params=params, auth=self.auth, headers=self.headers, timeout=10)
            r.raise_for_status()
            issues = r.json().get("issues", [])
            return issues[0]["key"] if issues else None
        except Exception as e:
            logger.error(f"JiraStatusAgent: search failed — {e}")
            return None

    def _fetch_ticket(self, ticket_key: str) -> dict:
        """Fetch ticket details from Jira REST API."""
        try:
            url = f"{self.base_url}/rest/api/3/issue/{ticket_key}"
            r = requests.get(url, auth=self.auth, headers=self.headers, timeout=10)
            r.raise_for_status()
            data = r.json()
            fields = data.get("fields", {})

            # Get latest comment
            comments = fields.get("comment", {}).get("comments", [])
            latest_comment = ""
            if comments:
                last = comments[-1]
                # Extract plain text from Atlassian Document Format
                try:
                    content = last.get("body", {}).get("content", [])
                    texts = []
                    for block in content:
                        for item in block.get("content", []):
                            if item.get("type") == "text":
                                texts.append(item.get("text", ""))
                    latest_comment = " ".join(texts)
                except Exception:
                    latest_comment = ""

            assignee = fields.get("assignee")
            assignee_name = assignee.get("displayName", "Unassigned") if assignee else "Unassigned"

            return {
                "ticket_key": ticket_key,
                "summary": fields.get("summary", ""),
                "status": fields.get("status", {}).get("name", "Unknown"),
                "priority": fields.get("priority", {}).get("name", "None"),
                "assignee": assignee_name,
                "latest_comment": latest_comment or "No comments yet",
                "ticket_url": f"{self.base_url}/browse/{ticket_key}",
            }
        except Exception as e:
            logger.error(f"JiraStatusAgent: fetch failed for {ticket_key} — {e}")
            return {"ticket_key": ticket_key, "error": str(e)}
