"""
SupervisorAgent
---------------
Reads required_agents from LLM analysis.
Sets execution order for downstream agents.
Also checks confidence threshold to decide auto-execute vs human review.
"""

import logging
import json
from src.config import CONFIDENCE_THRESHOLD
from src.graph.state import AgentState

logger = logging.getLogger(__name__)


class SupervisorAgent:

    def run(self, state: AgentState) -> AgentState:
        """
        Decide routing based on confidence and required_agents.
        Sets state["next_agents"] and state["route"].
        """
        analysis = state.get("analysis", {})
        confidence = analysis.get("confidence", 0.0)

        # Normalize required_agents to a clean list of allowed agents.
        raw_agents = analysis.get("required_agents", [])
        allowed = {"jira_agent", "calendar_agent", "reply_agent", "jira_status_agent"}

        # Defensive parsing: accept lists, JSON-encoded lists or comma-separated strings
        normalized: list = []
        try:
            if isinstance(raw_agents, str):
                # Try JSON first (e.g. '[]' or '["reply_agent"]')
                try:
                    parsed = json.loads(raw_agents)
                    if isinstance(parsed, list):
                        raw_list = parsed
                    else:
                        # Fall back to comma-separated
                        raw_list = [s.strip() for s in raw_agents.split(",") if s.strip()]
                except Exception:
                    raw_list = [s.strip() for s in raw_agents.split(",") if s.strip()]
            elif isinstance(raw_agents, list):
                raw_list = raw_agents
            else:
                raw_list = []

            # Keep only allowed agent names and preserve order, dedupe
            seen = set()
            for a in raw_list:
                if not isinstance(a, str):
                    continue
                name = a.strip()
                if name in allowed and name not in seen:
                    normalized.append(name)
                    seen.add(name)
        except Exception as e:
            logger.warning(f"SupervisorAgent: failed to normalize required_agents — {e}")
            normalized = []

        logger.info(f"SupervisorAgent: confidence={confidence}, required={normalized}")

        if confidence >= CONFIDENCE_THRESHOLD:
            state["route"] = "execute"
            state["next_agents"] = normalized
            logger.info("SupervisorAgent: confidence sufficient — auto executing")
        else:
            state["route"] = "review"
            state["next_agents"] = []
            logger.info("SupervisorAgent: confidence too low — sending to review queue")

        return state
