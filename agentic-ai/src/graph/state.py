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

    # ── Thread awareness (Feature 1) ──────────────────────────────────────────
    is_thread_reply: bool          # True when this email is a reply in an existing thread
    thread_history: list           # List of previous messages in the thread (for LLM context)

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
