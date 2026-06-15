"""
JiraAgent
---------
Professional task assignment agent that:
1. LLM decides: issue type, priority, which team
2. Fetches real team members from Jira project
3. LLM picks the most suitable assignee based on role/name
4. Creates ticket and assigns it to that person

This mirrors how a real project manager assigns work.
"""

import json
import logging
import time
import requests
from requests.auth import HTTPBasicAuth
from src.tools.ollama_client import call_ollama
from src.config import JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN, MAX_RETRIES, RETRY_DELAY_SECONDS
from src.graph.state import AgentState

logger = logging.getLogger(__name__)

JIRA_TICKET_PROMPT = """You are a project manager assigning work to your team.

A client email has been received. Decide how to create and assign the Jira ticket.

Email From: {sender}
Email Subject: {subject}
Email Body: {body}
Category: {category}
Intent: {intent}
Urgency: {urgency}

Available team members in this project:
{team_members}

Return ONLY a JSON object:
{{
  "issue_title": "clear, concise ticket title (max 80 chars)",
  "issue_description": "detailed description including what the client needs and what the team must do",
  "issue_type": "Bug or Task or Story",
  "priority": "Highest or High or Medium or Low",
  "assignee_account_id": "the accountId of the most suitable team member from the list above",
  "assignee_name": "display name of that person",
  "team_label": "development or marketing or support or design or management"
}}

Rules:
- Pick the assignee based on who is best suited for this type of work
- For bugs → pick a developer
- For meetings/scheduling → pick a manager or lead
- For support queries → pick a support person
- If unsure, pick the first person in the list
- Return ONLY valid JSON
"""


class JiraAgent:

    def __init__(self):
        self.auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self.base_url = JIRA_URL.rstrip("/")

    def run(self, state: AgentState) -> AgentState:
        if "jira_agent" not in state.get("next_agents", []):
            return state

        email = state.get("current_email", {})
        analysis = state.get("analysis", {})
        jira_project_key = state.get("jira_project_key")

        if not jira_project_key:
            logger.warning("JiraAgent: no jira_project_key — email is not from a known client")
            actions = state.get("actions_taken", [])
            actions.append({"agent": "jira_agent", "result": {
                "status": "skipped",
                "reason": "No Jira project key — sender is not a registered client"
            }})
            state["actions_taken"] = actions
            return state

        logger.info(f"JiraAgent INVOKED for email from {email.get('sender')} → project {jira_project_key}")

        # STEP 1: Fetch real team members from Jira
        team_members = self._get_project_members(jira_project_key)

        # STEP 2: LLM decides ticket details AND picks the right assignee
        ticket_details = self._reason_about_ticket(email, analysis, team_members)
        logger.info(f"JiraAgent LLM decision: type={ticket_details.get('issue_type')}, assignee={ticket_details.get('assignee_name')}")

        # STEP 3: Create and assign the ticket
        result = self._create_issue(jira_project_key, email, ticket_details)

        actions = state.get("actions_taken", [])
        actions.append({"agent": "jira_agent", "result": result})
        state["actions_taken"] = actions

        if result.get("status") == "success":
            state["jira_issue_key"] = result.get("issue_key")

        return state

    def _get_project_members(self, project_key: str) -> list:
        """Fetch all assignable users in this Jira project."""
        try:
            url = f"{self.base_url}/rest/api/3/user/assignable/search"
            params = {"project": project_key, "maxResults": 20}
            r = requests.get(url, params=params, auth=self.auth, headers=self.headers, timeout=10)
            r.raise_for_status()
            users = r.json()
            members = [
                {
                    "accountId": u.get("accountId"),
                    "displayName": u.get("displayName"),
                    "emailAddress": u.get("emailAddress", ""),
                }
                for u in users
                if u.get("active", True)
            ]
            logger.info(f"JiraAgent: found {len(members)} team members in {project_key}")
            return members
        except Exception as e:
            logger.warning(f"JiraAgent: could not fetch team members — {e}")
            return []

    def _reason_about_ticket(self, email: dict, analysis: dict, team_members: list) -> dict:
        """LLM decides ticket structure and picks the right assignee."""
        if team_members:
            members_str = "\n".join(
                f"- {m['displayName']} (accountId: {m['accountId']}, email: {m.get('emailAddress', 'N/A')})"
                for m in team_members
            )
        else:
            members_str = "No team members found — leave assignee as null"

        prompt = JIRA_TICKET_PROMPT.format(
            sender=email.get("sender", ""),
            subject=email.get("subject", ""),
            body=email.get("processed_content", "")[:1500],
            category=analysis.get("category", ""),
            intent=analysis.get("intent", ""),
            urgency=analysis.get("urgency", "medium"),
            team_members=members_str,
        )
        try:
            raw = call_ollama(prompt, temperature=0.1)
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            return json.loads(raw)
        except Exception as e:
            logger.warning(f"JiraAgent: LLM reasoning failed, using defaults — {e}")
            default_assignee = team_members[0] if team_members else {}
            return {
                "issue_title": email.get("subject", "Client request"),
                "issue_description": email.get("processed_content", "")[:500],
                "issue_type": "Task",
                "priority": "Medium",
                "assignee_account_id": default_assignee.get("accountId"),
                "assignee_name": default_assignee.get("displayName", "Unassigned"),
                "team_label": "support",
            }

    def _create_issue(self, project_key: str, email: dict, details: dict) -> dict:
        """Create Jira issue and assign to chosen team member."""
        payload = {
            "fields": {
                "project": {"key": project_key},
                "summary": details.get("issue_title", email.get("subject", "Email request")),
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": details.get("issue_description", "")}]
                        },
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": f"\n---\nClient: {email.get('sender', '')}"}]
                        }
                    ]
                },
                "issuetype": {"name": details.get("issue_type", "Task")},
                "priority": {"name": details.get("priority", "Medium")},
                "labels": ["agentic-ai", details.get("team_label", "support")],
            }
        }

        # Assign to team member if LLM picked one
        assignee_id = details.get("assignee_account_id")
        if assignee_id:
            payload["fields"]["assignee"] = {"accountId": assignee_id}

        url = f"{self.base_url}/rest/api/3/issue"

        for attempt in range(MAX_RETRIES + 1):
            try:
                response = requests.post(
                    url, json=payload,
                    auth=self.auth, headers=self.headers, timeout=10
                )
                response.raise_for_status()
                data = response.json()
                issue_key = data.get("key")
                logger.info(f"JiraAgent: issue created — {issue_key}, assigned to {details.get('assignee_name')}")
                return {
                    "status": "success",
                    "issue_key": issue_key,
                    "issue_url": f"{self.base_url}/browse/{issue_key}",
                    "assignee": details.get("assignee_name", "Unassigned"),
                    "team": details.get("team_label"),
                    "issue_type": details.get("issue_type"),
                }
            except requests.RequestException as e:
                logger.warning(f"JiraAgent: attempt {attempt + 1} failed — {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SECONDS)

        return {"status": "failed", "error": "Jira issue creation failed after retries"}
