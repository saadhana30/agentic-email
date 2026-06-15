"""
workflow.py
-----------
THIS IS WHERE LANGGRAPH IS DEFINED, COMPILED, AND INVOKED.

Graph flow:
  START
    → email_processor        (extract clean text from email)
    → spam_classifier        (is it spam? which client?)
    → [spam] → audit_log → END
    → llm_analysis           (LLM categorises email)
    → supervisor             (confidence check → route)
    → [review] → review_queue → audit_log → END
    → [execute] → jira_agent (if needed)
              → calendar_agent (if needed)
              → reply_agent    (if needed)
    → notification           (push in-app alerts)
    → audit_log → END
"""

import logging
from langgraph.graph import StateGraph, END

from src.graph.state import AgentState

# Nodes (processing functions)
from src.nodes.email_processor import email_processor_node
from src.nodes.spam_classifier import spam_classifier_node
from src.nodes.review_queue import review_queue_node
from src.nodes.notification import notification_node
from src.nodes.audit_logger import audit_logger_node

# Agents (classes with .run() methods)
from src.agents.email_analysis_agent import EmailAnalysisAgent
from src.agents.supervisor_agent import SupervisorAgent
from src.agents.calendar_agent import CalendarAgent
from src.agents.jira_agent import JiraAgent
from src.agents.jira_status_agent import JiraStatusAgent
from src.agents.reply_agent import ReplyAgent

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════════════
# AGENT INSTANTIATION
# Each agent is a class with an LLM + tools inside it.
# They are instantiated once here and reused across every graph run.
# Their .run() methods are registered as LangGraph nodes below.
# LangGraph calls .run() automatically when it reaches that node.
# ════════════════════════════════════════════════════════════════════════════
email_analysis_agent = EmailAnalysisAgent()   # uses Ollama LLM to categorise email
supervisor_agent     = SupervisorAgent()       # uses confidence threshold to route
calendar_agent       = CalendarAgent()         # uses Ollama LLM + Calendar API
jira_agent           = JiraAgent()             # uses Ollama LLM + Jira REST API
jira_status_agent    = JiraStatusAgent()       # fetches live Jira ticket status
reply_agent          = ReplyAgent()            # uses Ollama LLM + Gmail API


# ── Routing functions (return string → LangGraph uses it to pick next node) ─

def route_after_spam_check(state: AgentState) -> str:
    """After spam classification: spam → end, else → analyse"""
    if state.get("is_spam"):
        return "spam_end"
    return "llm_analysis"


def route_supervisor(state: AgentState) -> str:
    if state.get("route") == "review":
        return "review"

    agents = state.get("next_agents", [])
    if "jira_status_agent" in agents:
        return "jira_status_agent"
    if "jira_agent" in agents:
        return "jira_agent"
    if "calendar_agent" in agents:
        return "calendar_agent"
    if "reply_agent" in agents:
        return "reply_agent"
    return "notification"


def route_after_jira_status(state: AgentState) -> str:
    """After jira_status: always go to reply so client gets the status info"""
    if "reply_agent" in state.get("next_agents", []):
        return "reply_agent"
    return "notification"


def route_after_jira(state: AgentState) -> str:
    """After jira: check if calendar is also needed"""
    if "calendar_agent" in state.get("next_agents", []):
        return "calendar_agent"
    if "reply_agent" in state.get("next_agents", []):
        return "reply_agent"
    return "notification"


def route_after_calendar(state: AgentState) -> str:
    """After calendar: check if reply is also needed"""
    if "reply_agent" in state.get("next_agents", []):
        return "reply_agent"
    return "notification"



# ── Build the LangGraph StateGraph ──────────────────────────────────────────

def build_graph() -> StateGraph:
    graph = StateGraph(AgentState)

    # ════════════════════════════════════════════════════════════════════════
    # ADD NODES — each .run() is an agent being registered into the graph.
    # LangGraph will CALL these methods when it reaches the node.
    # This is how agents are invoked — not manually, but by the graph engine.
    # ════════════════════════════════════════════════════════════════════════
    graph.add_node("email_processor",   email_processor_node)
    graph.add_node("spam_classifier",   spam_classifier_node)
    graph.add_node("llm_analysis",      email_analysis_agent.run)
    graph.add_node("supervisor",        supervisor_agent.run)
    graph.add_node("review_queue",      review_queue_node)
    graph.add_node("jira_status_agent", jira_status_agent.run)      # AGENT: fetches live Jira status
    graph.add_node("jira_agent",        jira_agent.run)
    graph.add_node("calendar_agent",    calendar_agent.run)
    graph.add_node("reply_agent",       reply_agent.run)
    graph.add_node("notification",      notification_node)
    graph.add_node("audit_log",         audit_logger_node)

    # ── Entry point ──
    graph.set_entry_point("email_processor")

    # ── Edges ──
    graph.add_edge("email_processor", "spam_classifier")

    # After spam check → branch
    graph.add_conditional_edges(
        "spam_classifier",
        route_after_spam_check,
        {
            "spam_end":     "audit_log",
            "llm_analysis": "llm_analysis",
        }
    )

    graph.add_edge("llm_analysis", "supervisor")

    # After supervisor → branch:
    # "review" → review_queue
    # "execute" → first agent in required_agents list (or notification if none)
    graph.add_conditional_edges(
        "supervisor",
        route_supervisor,
        {
            "review":             "review_queue",
            "jira_status_agent":  "jira_status_agent",
            "jira_agent":         "jira_agent",
            "calendar_agent":     "calendar_agent",
            "reply_agent":        "reply_agent",
            "notification":       "notification",
        }
    )

    # jira_status_agent → always reply so client gets the info
    graph.add_conditional_edges(
        "jira_status_agent",
        route_after_jira_status,
        {
            "reply_agent":  "reply_agent",
            "notification": "notification",
        }
    )

    # Agent chain: jira → calendar → reply → notification
    graph.add_conditional_edges(
        "jira_agent",
        route_after_jira,
        {
            "calendar_agent": "calendar_agent",
            "reply_agent":    "reply_agent",
            "notification":   "notification",
        }
    )

    graph.add_conditional_edges(
        "calendar_agent",
        route_after_calendar,
        {
            "reply_agent":  "reply_agent",
            "notification": "notification",
        }
    )

    graph.add_edge("reply_agent",   "notification")
    graph.add_edge("notification",  "audit_log")
    graph.add_edge("review_queue",  "audit_log")
    graph.add_edge("audit_log",     END)

    return graph


# ── Compile once — reuse across all invocations ──────────────────────────────
compiled_graph = build_graph().compile()


# ── Public invoke function ───────────────────────────────────────────────────

def run_graph_for_email(email: dict) -> AgentState:
    """
    Entry point to invoke the compiled LangGraph for a single email.
    Called by the polling loop or by the API after human approval.
    """
    initial_state: AgentState = {
        "raw_emails": [],
        "current_email": email,
        "is_spam": False,
        "email_source": "",
        "client_name": None,
        "jira_project_key": None,
        "analysis": {},
        "route": "",
        "next_agents": [],
        "actions_taken": [],
        "calendar_rescheduled": False,
        "proposed_slot": None,
        "jira_issue_key": None,
        "jira_status_info": None,
        "awaiting_review": False,
        "review_id": None,
        "error": None,
    }

    logger.info(f"LangGraph: invoking graph for email from {email.get('sender')}")

    # ════════════════════════════════════════════════════════════════════════
    # THIS IS THE SINGLE LINE WHERE ALL AGENTS ARE INVOKED.
    # compiled_graph.invoke() triggers LangGraph to walk the graph,
    # calling each registered agent's .run() method in sequence:
    #
    #   email_processor_node()
    #   spam_classifier_node()
    #   email_analysis_agent.run()   ← EmailAnalysisAgent INVOKED
    #   supervisor_agent.run()       ← SupervisorAgent INVOKED
    #   (if confidence >= threshold):
    #     jira_agent.run()           ← JiraAgent INVOKED (if needed)
    #     calendar_agent.run()       ← CalendarAgent INVOKED (if needed)
    #     reply_agent.run()          ← ReplyAgent INVOKED (if needed)
    #   notification_node()
    #   audit_logger_node()
    # ════════════════════════════════════════════════════════════════════════
    result = compiled_graph.invoke(initial_state)
    logger.info(f"LangGraph: graph completed for email {email.get('message_id')}")

    return result
