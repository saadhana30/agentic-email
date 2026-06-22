from typing import TypedDict, Optional, Any


class AgentState(TypedDict):
    # Fetched emails (list of raw email dicts from EmailMonitoringAgent)
    raw_emails: list

    # The single email being processed in this graph run
    current_email: dict
    # current_email keys (set by EmailMonitoringAgent):
    #   message_id, thread_id, in_reply_to, references,
    #   sender, sender_domain, subject, received_at,
    #   raw_content, processed_content, attachment_type,
    #   attachments (list of {filename, mime_type, attachment_type, extracted_text})

    # ── Thread awareness ──────────────────────────────────────────────────────
    is_thread_reply: bool          # True when this email is a reply in an existing thread
    thread_history: list           # List of previous messages in the thread (for LLM context)

    # thread_context holds resolved artefacts found in previous thread turns.
    # Populated by spam_classifier_node when is_thread_reply=True.
    # Consumed by JiraAgent, CalendarAgent, ReplyAgent.
    # Keys (all optional):
    #   existing_jira_key  : str   — issue key from a previous turn (e.g. "PROJ-42")
    #   existing_event_id  : str   — Google Calendar event id from a previous turn
    #   existing_event_slot: str   — human-readable slot string from a previous turn
    #   conversation_summary: str  — plain-text summary injected into ReplyAgent prompt
    thread_context: dict

    # ── Spam / client classification ──────────────────────────────────────────
    is_spam: bool
    email_source: str              # "client" | "other" | "spam"
    client_name: Optional[str]
    jira_project_key: Optional[str]

    # ── LLM analysis result ───────────────────────────────────────────────────
    analysis: dict

    # ── Supervisor decision ───────────────────────────────────────────────────
    route: str                     # "execute" | "review"
    next_agents: list              # e.g. ["jira_agent", "calendar_agent"]

    # ── Actions taken by worker agents ───────────────────────────────────────
    actions_taken: list

    # ── Calendar-specific ─────────────────────────────────────────────────────
    calendar_rescheduled: bool
    proposed_slot: Optional[str]

    # ── Jira-specific ─────────────────────────────────────────────────────────
    jira_issue_key: Optional[str]
    jira_status_info: Optional[dict]   # populated by JiraStatusAgent

    # ── Human-in-loop ─────────────────────────────────────────────────────────
    awaiting_review: bool
    review_id: Optional[int]

    # ── Error tracking ────────────────────────────────────────────────────────
    error: Optional[str]
