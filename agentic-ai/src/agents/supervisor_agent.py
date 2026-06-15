"""
SupervisorAgent
---------------
Reads required_agents from LLM analysis.
Sets execution order for downstream agents.
Also checks confidence threshold to decide auto-execute vs human review.
"""

import logging
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
        required_agents = analysis.get("required_agents", [])

        logger.info(f"SupervisorAgent: confidence={confidence}, required={required_agents}")

        if confidence >= CONFIDENCE_THRESHOLD:
            state["route"] = "execute"
            state["next_agents"] = required_agents
            logger.info("SupervisorAgent: confidence sufficient — auto executing")
        else:
            state["route"] = "review"
            state["next_agents"] = []
            logger.info("SupervisorAgent: confidence too low — sending to review queue")

        return state
