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

Every node is wrapped with trace_node() for timing/tracing.
Domain events are emitted at key pipeline points for the Live Monitor.
"""

import logging
from langgraph.graph import StateGraph, END

from src.graph.state   import AgentState
from src.graph.tracer  import trace_node

# Nodes
from src.nodes.email_processor import email_processor_node
from src.nodes.spam_classifier  import spam_classifier_node
from src.nodes.review_queue     import review_queue_node
from src.nodes.notification     import notification_node
from src.nodes.audit_logger     import audit_logger_node

# Agents
from src.agents.email_analysis_agent import EmailAnalysisAgent
from src.agents.supervisor_agent     import SupervisorAgent
from src.agents.calendar_agent       import CalendarAgent
from src.agents.jira_agent           import JiraAgent
from src.agents.jira_status_agent    import JiraStatusAgent
from src.agents.reply_agent          import ReplyAgent

logger = logging.getLogger(__name__)

# ── Agent instances ───────────────────────────────────────────────────────────
email_analysis_agent = EmailAnalysisAgent()
supervisor_agent     = SupervisorAgent()
calendar_agent       = CalendarAgent()
jira_agent           = JiraAgent()
jira_status_agent    = JiraStatusAgent()
reply_agent          = ReplyAgent()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _email_id(state: AgentState) -> str | None:
    return (state.get("current_email") or {}).get("message_id")


def _safe_emit(fn, *args, **kwargs):
    """Call a monitor emit function without ever raising."""
    try:
        fn(*args, **kwargs)
    except Exception:
        pass


# ── Traced + monitored node wrappers ─────────────────────────────────────────

def _email_processor(state: AgentState) -> AgentState:
    with trace_node("email_processor", state):
        result = email_processor_node(state)
    # Emit per-attachment events after node completes
    for att in (result.get("current_email") or {}).get("attachments", []):
        _safe_emit(
            lambda a: __import__("src.monitor", fromlist=["emit_attachment_extracted"])
            .emit_attachment_extracted(
                _email_id(result),
                a.get("filename", "unknown"),
                a.get("attachment_type", "unknown"),
                len(a.get("extracted_text", "")),
            ),
            att,
        )
    return result


def _spam_classifier(state: AgentState) -> AgentState:
    with trace_node("spam_classifier", state):
        result = spam_classifier_node(state)

    email  = result.get("current_email") or {}
    eid    = _email_id(result)

    # Existing: spam detected event
    if result.get("is_spam"):
        _safe_emit(
            lambda: __import__("src.monitor", fromlist=["emit_spam_detected"])
            .emit_spam_detected(eid, email.get("sender", "unknown"))
        )
        return result

    # New: thread reply detected — fires only if spam_classifier found an existing thread
    if result.get("is_thread_reply"):
        thread_id    = email.get("thread_id", "")
        history      = result.get("thread_history", [])
        ctx          = result.get("thread_context", {})

        _safe_emit(
            lambda t=thread_id, c=len(history): (
                __import__("src.monitor", fromlist=["emit_thread_reply_detected"])
                .emit_thread_reply_detected(eid, t, c)
            )
        )

        # New: thread history loaded — fires only when history was actually populated
        if history:
            _safe_emit(
                lambda c=len(history), jk=ctx.get("existing_jira_key"), ev=ctx.get("existing_event_id"): (
                    __import__("src.monitor", fromlist=["emit_thread_history_loaded"])
                    .emit_thread_history_loaded(eid, c, jk, ev)
                )
            )

    return result


def _llm_analysis(state: AgentState) -> AgentState:
    with trace_node("llm_analysis", state):
        result = email_analysis_agent.run(state)

    # New: conversation_context_applied — fires when thread history was used
    if result.get("is_thread_reply") and result.get("thread_history"):
        analysis  = result.get("analysis") or {}
        category  = analysis.get("category", "unknown")
        ctx       = result.get("thread_context", {})
        jira_key  = ctx.get("existing_jira_key")
        event_id  = ctx.get("existing_event_id")

        parts = []
        if jira_key:
            parts.append(f"referenced ticket {jira_key}")
        if event_id:
            parts.append("referenced calendar event")
        if not parts:
            parts.append("history injected into prompt")

        _safe_emit(
            lambda cat=category, note=", ".join(parts): (
                __import__("src.monitor", fromlist=["emit_conversation_context_applied"])
                .emit_conversation_context_applied(_email_id(result), cat, note)
            )
        )

    return result


def _supervisor(state: AgentState) -> AgentState:
    with trace_node("supervisor", state):
        result = supervisor_agent.run(state)
    analysis = result.get("analysis") or {}
    _safe_emit(
        lambda: __import__("src.monitor", fromlist=["emit_routing_decision"])
        .emit_routing_decision(
            _email_id(result),
            result.get("route", ""),
            float(analysis.get("confidence", 0)),
            result.get("next_agents", []),
        )
    )
    return result


def _review_queue(state: AgentState) -> AgentState:
    with trace_node("review_queue", state):
        result = review_queue_node(state)
    analysis = result.get("analysis") or {}
    _safe_emit(
        lambda: __import__("src.monitor", fromlist=["emit_review_queued"])
        .emit_review_queued(
            _email_id(result),
            float(analysis.get("confidence", 0)),
            analysis.get("intent", ""),
        )
    )
    return result


def _jira_status_agent(state: AgentState) -> AgentState:
    if "jira_status_agent" not in state.get("next_agents", []):
        return state
    if "jira_status_agent" in state.get("executed_agents", []):
        return state

    _safe_emit(
        lambda: __import__("src.monitor", fromlist=["emit_agent_invoked"])
        .emit_agent_invoked(_email_id(state), "jira_status_agent")
    )

    with trace_node("jira_status_agent", state):
        state = jira_status_agent.run(state)

    executed = state.get("executed_agents", [])
    if "jira_status_agent" not in executed:
        executed.append("jira_status_agent")
    state["executed_agents"] = executed
    state["next_agents"] = [a for a in state.get("next_agents", []) if a != "jira_status_agent"]
    return state


def _jira_agent(state: AgentState) -> AgentState:
    if "jira_agent" not in state.get("next_agents", []):
        return state
    if "jira_agent" in state.get("executed_agents", []):
        return state

    _safe_emit(
        lambda: __import__("src.monitor", fromlist=["emit_agent_invoked"])
        .emit_agent_invoked(_email_id(state), "jira_agent")
    )

    with trace_node("jira_agent", state):
        state = jira_agent.run(state)

    # Emit domain event if ticket was created
    for action in state.get("actions_taken", []):
        if action.get("agent") == "jira_agent":
            res = action.get("result", {})
            if res.get("status") == "success" and res.get("issue_key"):
                _safe_emit(
                    lambda r=res: __import__("src.monitor", fromlist=["emit_jira_ticket_created"])
                    .emit_jira_ticket_created(
                        _email_id(state),
                        r.get("issue_key", ""),
                        r.get("assignee", "Unassigned"),
                        r.get("issue_type", "Task"),
                        r.get("priority", "Medium"),
                    )
                )

    executed = state.get("executed_agents", [])
    if "jira_agent" not in executed:
        executed.append("jira_agent")
    state["executed_agents"] = executed
    state["next_agents"] = [a for a in state.get("next_agents", []) if a != "jira_agent"]
    return state


def _calendar_agent(state: AgentState) -> AgentState:
    if "calendar_agent" not in state.get("next_agents", []):
        return state
    if "calendar_agent" in state.get("executed_agents", []):
        return state

    _safe_emit(
        lambda: __import__("src.monitor", fromlist=["emit_agent_invoked"])
        .emit_agent_invoked(_email_id(state), "calendar_agent")
    )

    with trace_node("calendar_agent", state):
        state = calendar_agent.run(state)

    for action in state.get("actions_taken", []):
        if action.get("agent") == "calendar_agent":
            res = action.get("result", {})
            if res.get("status") == "success" and res.get("slot"):
                _safe_emit(
                    lambda r=res: __import__("src.monitor", fromlist=["emit_calendar_event_created"])
                    .emit_calendar_event_created(
                        _email_id(state),
                        r.get("slot", ""),
                        r.get("rescheduled", False),
                    )
                )

    executed = state.get("executed_agents", [])
    if "calendar_agent" not in executed:
        executed.append("calendar_agent")
    state["executed_agents"] = executed
    state["next_agents"] = [a for a in state.get("next_agents", []) if a != "calendar_agent"]
    return state


def _reply_agent(state: AgentState) -> AgentState:
    if "reply_agent" not in state.get("next_agents", []):
        return state
    if "reply_agent" in state.get("executed_agents", []):
        return state

    _safe_emit(
        lambda: __import__("src.monitor", fromlist=["emit_agent_invoked"])
        .emit_agent_invoked(_email_id(state), "reply_agent")
    )

    with trace_node("reply_agent", state):
        state = reply_agent.run(state)

    for action in state.get("actions_taken", []):
        if action.get("agent") == "reply_agent":
            res = action.get("result", {})
            if res.get("status") == "success":
                _safe_emit(
                    lambda: __import__("src.monitor", fromlist=["emit_reply_sent"])
                    .emit_reply_sent(
                        _email_id(state),
                        (state.get("current_email") or {}).get("sender", "client"),
                    )
                )

    executed = state.get("executed_agents", [])
    if "reply_agent" not in executed:
        executed.append("reply_agent")
    state["executed_agents"] = executed
    state["next_agents"] = [a for a in state.get("next_agents", []) if a != "reply_agent"]
    return state


def _notification(state: AgentState) -> AgentState:
    with trace_node("notification", state):
        return notification_node(state)


def _audit_log(state: AgentState) -> AgentState:
    with trace_node("audit_log", state):
        result = audit_logger_node(state)
    # Final processing_completed / processing_failed event
    if result.get("error") or result.get("route") == "failed":
        _safe_emit(
            lambda: __import__("src.monitor", fromlist=["emit_processing_failed"])
            .emit_processing_failed(_email_id(result), result.get("error", "unknown"))
        )
    else:
        _safe_emit(
            lambda: __import__("src.monitor", fromlist=["emit_processing_completed"])
            .emit_processing_completed(
                _email_id(result),
                result.get("route", "execute"),
                len(result.get("actions_taken", [])),
            )
        )
    return result


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
        "spam_classifier", route_after_spam_check,
        {"spam_end": "audit_log", "llm_analysis": "llm_analysis"},
    )
    graph.add_edge("llm_analysis", "supervisor")
    graph.add_conditional_edges(
        "supervisor", route_supervisor,
        {
            "review":            "review_queue",
            "jira_status_agent": "jira_status_agent",
            "jira_agent":        "jira_agent",
            "calendar_agent":    "calendar_agent",
            "reply_agent":       "reply_agent",
            "notification":      "notification",
        },
    )
    graph.add_conditional_edges(
        "jira_status_agent", route_after_jira_status,
        {"reply_agent": "reply_agent", "notification": "notification"},
    )
    graph.add_conditional_edges(
        "jira_agent", route_after_jira,
        {
            "calendar_agent": "calendar_agent",
            "reply_agent":    "reply_agent",
            "notification":   "notification",
        },
    )
    graph.add_conditional_edges(
        "calendar_agent", route_after_calendar,
        {"reply_agent": "reply_agent", "notification": "notification"},
    )
    graph.add_edge("reply_agent",  "notification")
    graph.add_edge("notification", "audit_log")
    graph.add_edge("review_queue", "audit_log")
    graph.add_edge("audit_log",    END)

    return graph


compiled_graph = build_graph().compile()


# ── Public invoke ─────────────────────────────────────────────────────────────

def run_graph_for_email(email: dict) -> AgentState:
    """Entry point — emit email_detected, then run the full graph."""
    # Emit email_detected before the graph starts
    _safe_emit(
        lambda: __import__("src.monitor", fromlist=["emit_email_detected"])
        .emit_email_detected(
            email.get("message_id"),
            email.get("sender", "unknown"),
            email.get("subject", "(no subject)"),
            email.get("attachment_type", "text"),
            bool(email.get("in_reply_to")),
        )
    )

    initial_state: AgentState = {
        "raw_emails":           [],
        "current_email":        email,
        "is_thread_reply":      False,
        "thread_history":       [],
        "thread_context":       {},
        "is_spam":              False,
        "email_source":         "",
        "client_name":          None,
        "jira_project_key":     None,
        "analysis":             {},
        "route":                "",
        "next_agents":          [],
        "executed_agents":      [],
        "actions_taken":        [],
        "calendar_rescheduled": False,
        "proposed_slot":        None,
        "jira_issue_key":       None,
        "jira_status_info":     None,
        "awaiting_review":      False,
        "review_id":            None,
        "error":                None,
    }

    logger.info(f"LangGraph: invoking graph for email from {email.get('sender')}")
    result = compiled_graph.invoke(initial_state)
    logger.info(f"LangGraph: completed for email {email.get('message_id')}")
    return result
