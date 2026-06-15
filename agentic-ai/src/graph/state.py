from typing import TypedDict, Optional, Any


class AgentState(TypedDict):
    # Fetched emails (list of raw email dicts)
    raw_emails: list

    # The single email being processed in this graph run
    current_email: dict

    # After email_processor_node runs
    # (processed_content is set inside current_email dict)

    # Spam / client classification
    is_spam: bool
    email_source: str        # "client" | "other" | "spam"
    client_name: Optional[str]
    jira_project_key: Optional[str]

    # LLM analysis result
    analysis: dict

    # Supervisor decision
    route: str               # "execute" | "review"
    next_agents: list        # e.g. ["jira_agent", "calendar_agent"]

    # Actions taken by worker agents
    actions_taken: list

    # Calendar-specific
    calendar_rescheduled: bool
    proposed_slot: Optional[str]

    # Jira-specific
    jira_issue_key: Optional[str]
    jira_status_info: Optional[dict]  # populated by JiraStatusAgent for status enquiries

    # Human-in-loop
    awaiting_review: bool
    review_id: Optional[int]

    # Error tracking
    error: Optional[str]
