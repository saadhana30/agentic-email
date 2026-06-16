"""
workflow.py
-----------
LangGraph definition, compilation and invocation.

Graph flow:
  START
    → email_processor        (extract clean text from email)
    → spam_classifier        (is it spam? which client?)
    → [spam] → audit_log → END
    → llm_analysis           (LLM categorises email)
    → supervisor             (confidence check → route)
    → [review] → review_queue → audit_log → END
    → [execute] → jira_status_agent (if needed)
              → jira_agent          (if needed)
              → calendar_agent      (if needed)
              → reply_agent         (if needed)
    → notification           (push in-app alerts)
    → audit_log → END

Every node is wrapped with trace_node() for execution observability (Feature 4).
OllamaRetryExhausted in EmailAnalysisAgent sets confidence=0.0 so supervisor
automatically routes to review_queue — no graph crash.
"""

import logging
from langgraph.graph import StateGraph, END

from src.graph.state import AgentState
from src.graph.tracer import trace_node

# Nodes
from src.nodes.email_processor import email_processor_node
from src.nodes.spam_classifier  import spam_classifier_node
from src.nodes.review_queue     import review_queue_node
from src.nodes.notification     import notification_node
from src.nodes.audit_logger     import audit_logger_node

# Agents
from src.agents.email_analysis_agent   import EmailAnalysisAgent
from src.agents.supervisor_agent       import SupervisorAgent
from src.agents.calendar_agent         import CalendarAgent
from src.agents.jira_agent             import JiraAgent
from src.agents.jira_status_agent      import JiraStatusAgent
from src.agents.reply_agent            import ReplyAgent

logger = logging.getLogger(__name__)

# ── Agent instances (created once, reused for every email) ───────────────────
email_analysis_agent = EmailAnalysisAgent()
supervisor_agent     = SupervisorAgent()
calendar_agent       = CalendarAgent()
jira_agent           = JiraAgent()
jira_status_agent    = JiraStatusAgent()
reply_agent          = ReplyAgent()


# ── Traced node wrappers (Feature 4) ─────────────────────────────────────────
# Each wrapper calls trace_node so timing + success/failure is persisted.

def _email_processor(state: AgentState) -> AgentState:
    with trace_node("email_processor", state):
        return email_processor_node(state)

def _spam_classifier(state: AgentState) -> AgentState:
    with trace_node("spam_classifier", state):
        return spam_classifier_node(state)

def _llm_analysis(state: AgentState) -> AgentState:
    with trace_node("llm_analysis", state):
        return email_analysis_agent.run(state)

def _supervisor(state: AgentState) -> AgentState:
    with trace_node("supervisor", state):
        return supervisor_agent.run(state)

def _review_queue(state: AgentState) -> AgentState:
    with trace_node("review_queue", state):
        return review_queue_node(state)

def _jira_status_agent(state: AgentState) -> AgentState:
    with trace_node("jira_status_agent", state):
        return jira_status_agent.run(state)

def _jira_agent(state: AgentState) -> AgentState:
    with trace_node("jira_agent", state):
        return jira_agent.run(state)

def _calendar_agent(state: AgentState) -> AgentState:
    with trace_node("calendar_agent", state):
        return calendar_agent.run(state)

def _reply_agent(state: AgentState) -> AgentState:
    with trace_node("reply_agent", state):
        return reply_agent.run(state)

def _notification(state: AgentState) -> AgentState:
    with trace_node("notification", state):
        return notification_node(state)

def _audit_log(state: AgentState) -> AgentState:
    with trace_node("audit_log", state):
        return audit_logger_node(state)


# ── Routing functions ─────────────────────────────────────────────────────────

def route_after_spam_check(state: AgentState) -> str:
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
    if "reply_agent" in state.get("next_agents", []):
        return "reply_agent"
    return "notification"


def route_after_jira(state: AgentState) -> str:
    if "calendar_agent" in state.get("next_agents", []):
        return "calendar_agent"
    if "reply_agent" in state.get("next_agents", []):
        return "reply_agent"
    return "notification"


def route_after_calendar(state: AgentState) -> str:
    if "reply_agent" in state.get("next_agents", []):
        return "reply_agent"
    return "notification"


# ── Build the LangGraph ───────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    graph = StateGraph(AgentState)

    graph.add_node("email_processor",   _email_processor)
    graph.add_node("spam_classifier",   _spam_classifier)
    graph.add_node("llm_analysis",      _llm_analysis)
    graph.add_node("supervisor",        _supervisor)
    graph.add_node("review_queue",      _review_queue)
    graph.add_node("jira_status_agent", _jira_status_agent)
    graph.add_node("jira_agent",        _jira_agent)
    graph.add_node("calendar_agent",    _calendar_agent)
    graph.add_node("reply_agent",       _reply_agent)
    graph.add_node("notification",      _notification)
    graph.add_node("audit_log",         _audit_log)

    graph.set_entry_point("email_processor")

    graph.add_edge("email_processor", "spam_classifier")

    graph.add_conditional_edges(
        "spam_classifier",
        route_after_spam_check,
        {
            "spam_end":     "audit_log",
            "llm_analysis": "llm_analysis",
        }
    )

    graph.add_edge("llm_analysis", "supervisor")

    graph.add_conditional_edges(
        "supervisor",
        route_supervisor,
        {
            "review":            "review_queue",
            "jira_status_agent": "jira_status_agent",
            "jira_agent":        "jira_agent",
            "calendar_agent":    "calendar_agent",
            "reply_agent":       "reply_agent",
            "notification":      "notification",
        }
    )

    graph.add_conditional_edges(
        "jira_status_agent",
        route_after_jira_status,
        {
            "reply_agent":  "reply_agent",
            "notification": "notification",
        }
    )

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

    graph.add_edge("reply_agent",  "notification")
    graph.add_edge("notification", "audit_log")
    graph.add_edge("review_queue", "audit_log")
    graph.add_edge("audit_log",    END)

    return graph


# Compile once — reuse for all invocations
compiled_graph = build_graph().compile()


# ── Public invoke ─────────────────────────────────────────────────────────────

def run_graph_for_email(email: dict) -> AgentState:
    """
    Entry point to invoke the compiled LangGraph for a single email.
    Called by the polling loop or by the API (human approval / reprocess).
    """
    initial_state: AgentState = {
        "raw_emails":          [],
        "current_email":       email,
        "is_thread_reply":     False,
        "thread_history":      [],
        "is_spam":             False,
        "email_source":        "",
        "client_name":         None,
        "jira_project_key":    None,
        "analysis":            {},
        "route":               "",
        "next_agents":         [],
        "actions_taken":       [],
        "calendar_rescheduled": False,
        "proposed_slot":       None,
        "jira_issue_key":      None,
        "jira_status_info":    None,
        "awaiting_review":     False,
        "review_id":           None,
        "error":               None,
    }

    logger.info(f"LangGraph: invoking graph for email from {email.get('sender')}")
    result = compiled_graph.invoke(initial_state)
    logger.info(f"LangGraph: completed for email {email.get('message_id')}")
    return result
