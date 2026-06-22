"""
db.py
-----
Database models and helper functions.

Tables:
  emails             — email records with thread metadata
  analysis           — LLM analysis results
  actions            — agent actions taken
  review_queue       — low-confidence emails awaiting human review
  notifications      — in-app notifications
  clients            — registered client domains → Jira project keys
  email_threads      — thread metadata (thread_id, message chain)
  attachments        — per-attachment extracted text (multi-attachment support)
  execution_traces   — per-node LangGraph execution timing and status
  users              — authentication users
  execution_events   — structured execution monitor events (Live Monitor page)
"""

import json
from datetime import datetime, timezone

from sqlalchemy import (
    create_engine, Column, String, Float, Boolean,
    DateTime, Text, Integer, ForeignKey,
)
from sqlalchemy.orm import declarative_base, sessionmaker

from src.config import DATABASE_URL

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


# ─────────────────────────────────────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────────────────────────────────────

class EmailRecord(Base):
    __tablename__ = "emails"

    id              = Column(String,  primary_key=True)   # Gmail message_id
    thread_id       = Column(String,  nullable=False, index=True)
    in_reply_to     = Column(String,  nullable=True)      # Message-ID header of parent
    references      = Column(Text,    nullable=True)      # Space-separated reference chain
    sender          = Column(String)
    sender_domain   = Column(String)
    subject         = Column(String)
    received_at     = Column(DateTime)
    attachment_type = Column(String,  default="text")     # text | pdf | image | mixed
    raw_content     = Column(Text)
    is_spam         = Column(Boolean, default=False)
    client_name     = Column(String,  nullable=True)
    processed       = Column(Boolean, default=False)
    created_at      = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class EmailThread(Base):
    """Tracks the full message chain within a Gmail thread."""
    __tablename__ = "email_threads"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    thread_id       = Column(String,  nullable=False, index=True, unique=True)
    participant_emails = Column(Text, nullable=True)       # JSON list of all senders in thread
    message_ids     = Column(Text,    nullable=True)       # JSON list of message_ids in order
    subject         = Column(String,  nullable=True)       # Original subject of the thread
    last_message_at = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at      = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                             onupdate=lambda: datetime.now(timezone.utc))


class AttachmentRecord(Base):
    """Stores extracted text per attachment (multi-attachment support)."""
    __tablename__ = "attachments"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    email_id        = Column(String,  nullable=False, index=True)
    filename        = Column(String,  nullable=True)
    mime_type       = Column(String,  nullable=True)
    attachment_type = Column(String,  nullable=True)       # pdf | image
    extracted_text  = Column(Text,    nullable=True)
    char_count      = Column(Integer, default=0)
    created_at      = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class AnalysisRecord(Base):
    __tablename__ = "analysis"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    email_id        = Column(String)
    category        = Column(String)
    intent          = Column(Text)
    urgency         = Column(String)
    confidence      = Column(Float)
    required_agents = Column(Text)    # stored as JSON string
    execution_plan  = Column(Text)    # stored as JSON string
    created_at      = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ActionRecord(Base):
    __tablename__ = "actions"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    email_id        = Column(String)
    agent_name      = Column(String)
    action_taken    = Column(Text)
    status          = Column(String)   # success | failed | skipped
    error_message   = Column(Text,    nullable=True)
    timestamp       = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ReviewQueueRecord(Base):
    __tablename__ = "review_queue"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    email_id            = Column(String)
    analysis_snapshot   = Column(Text)   # JSON snapshot of analysis at time of queuing
    status              = Column(String, default="pending")   # pending | approved | rejected
    reviewed_by         = Column(String, nullable=True)
    reviewed_at         = Column(DateTime, nullable=True)
    created_at          = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class NotificationRecord(Base):
    __tablename__ = "notifications"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    message     = Column(Text)
    type        = Column(String)    # jira | calendar | email | system
    seen        = Column(Boolean,  default=False)
    created_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ClientRecord(Base):
    __tablename__ = "clients"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    name             = Column(String, unique=True)
    email_domain     = Column(String, unique=True)
    jira_project_key = Column(String)
    created_at       = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ExecutionTrace(Base):
    """Records timing and outcome for every LangGraph node execution."""
    __tablename__ = "execution_traces"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    email_id          = Column(String,  nullable=True, index=True)
    node_name         = Column(String,  nullable=False)
    start_time        = Column(DateTime, nullable=False)
    end_time          = Column(DateTime, nullable=True)
    duration_ms       = Column(Float,   nullable=True)   # milliseconds
    success           = Column(Boolean, default=True)
    exception_message = Column(Text,    nullable=True)
    created_at        = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class UserRecord(Base):
    """Authentication users."""
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    username        = Column(String,  unique=True, nullable=False)
    email           = Column(String,  unique=True, nullable=False)
    hashed_password = Column(String,  nullable=False)
    role            = Column(String,  default="user")   # admin | user
    is_active       = Column(Boolean, default=True)
    created_at      = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# ─────────────────────────────────────────────────────────────────────────────
# INIT
# ─────────────────────────────────────────────────────────────────────────────

def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def is_duplicate_email(db, message_id: str) -> bool:
    return db.query(EmailRecord).filter(EmailRecord.id == message_id).first() is not None


def get_client_by_domain(db, domain: str):
    return db.query(ClientRecord).filter(ClientRecord.email_domain == domain).first()


def save_email(db, data: dict):
    record = EmailRecord(**data)
    db.add(record)
    db.commit()


def save_analysis(db, data: dict):
    record = AnalysisRecord(**data)
    db.add(record)
    db.commit()


def save_action(db, data: dict):
    record = ActionRecord(**data)
    db.add(record)
    db.commit()


def add_to_review_queue(db, email_id: str, analysis: dict) -> int:
    record = ReviewQueueRecord(
        email_id=email_id,
        analysis_snapshot=json.dumps(analysis),
    )
    db.add(record)
    db.commit()
    return record.id


def add_notification(db, message: str, notif_type: str):
    record = NotificationRecord(message=message, type=notif_type)
    db.add(record)
    db.commit()


def get_pending_review(db, review_id: int):
    return db.query(ReviewQueueRecord).filter(ReviewQueueRecord.id == review_id).first()


def update_review_status(db, review_id: int, status: str, reviewed_by: str = "admin"):
    record = db.query(ReviewQueueRecord).filter(ReviewQueueRecord.id == review_id).first()
    if record:
        record.status = status
        record.reviewed_by = reviewed_by
        record.reviewed_at = datetime.now(timezone.utc)
        db.commit()
    return record


# ─────────────────────────────────────────────────────────────────────────────
# THREAD HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_thread(db, thread_id: str):
    return db.query(EmailThread).filter(EmailThread.thread_id == thread_id).first()


def upsert_thread(db, thread_id: str, message_id: str, sender: str,
                  subject: str, received_at: datetime | None):
    thread = db.query(EmailThread).filter(EmailThread.thread_id == thread_id).first()
    if thread:
        # Add message_id if not already present
        msg_ids = json.loads(thread.message_ids or "[]")
        if message_id not in msg_ids:
            msg_ids.append(message_id)
        thread.message_ids = json.dumps(msg_ids)

        participants = json.loads(thread.participant_emails or "[]")
        if sender not in participants:
            participants.append(sender)
        thread.participant_emails = json.dumps(participants)

        thread.last_message_at = received_at
        thread.updated_at = datetime.now(timezone.utc)
    else:
        thread = EmailThread(
            thread_id=thread_id,
            message_ids=json.dumps([message_id]),
            participant_emails=json.dumps([sender]),
            subject=subject,
            last_message_at=received_at,
        )
        db.add(thread)
    db.commit()
    return thread


def get_thread_emails(db, thread_id: str, exclude_message_id: str | None = None) -> list:
    """Return all EmailRecord rows for a thread, ordered oldest first."""
    query = db.query(EmailRecord).filter(EmailRecord.thread_id == thread_id)
    if exclude_message_id:
        query = query.filter(EmailRecord.id != exclude_message_id)
    return query.order_by(EmailRecord.received_at.asc()).all()


# ─────────────────────────────────────────────────────────────────────────────
# ATTACHMENT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def save_attachment(db, data: dict):
    record = AttachmentRecord(**data)
    db.add(record)
    db.commit()
    return record


def get_attachments_for_email(db, email_id: str) -> list:
    return db.query(AttachmentRecord).filter(AttachmentRecord.email_id == email_id).all()


# ─────────────────────────────────────────────────────────────────────────────
# EXECUTION TRACE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def save_execution_trace(db, data: dict):
    record = ExecutionTrace(**data)
    db.add(record)
    db.commit()
    return record


def get_traces_for_email(db, email_id: str) -> list:
    return (
        db.query(ExecutionTrace)
        .filter(ExecutionTrace.email_id == email_id)
        .order_by(ExecutionTrace.start_time.asc())
        .all()
    )


# ─────────────────────────────────────────────────────────────────────────────
# USER HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_user_by_username(db, username: str):
    return db.query(UserRecord).filter(UserRecord.username == username).first()


def get_user_by_id(db, user_id: int):
    return db.query(UserRecord).filter(UserRecord.id == user_id).first()


def create_user(db, username: str, email: str, hashed_password: str, role: str = "user"):
    user = UserRecord(
        username=username,
        email=email,
        hashed_password=hashed_password,
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ─────────────────────────────────────────────────────────────────────────────
# EXECUTION EVENT MODEL  (Live Execution Monitor)
# ─────────────────────────────────────────────────────────────────────────────

class ExecutionEvent(Base):
    """
    Structured execution events for the Live Execution Monitor page.
    One row per meaningful step in email processing.
    NOT raw terminal logs — human-readable operational events only.
    """
    __tablename__ = "execution_events"

    id          = Column(Integer,  primary_key=True, autoincrement=True)
    email_id    = Column(String,   nullable=True,  index=True)
    timestamp   = Column(DateTime, nullable=False,
                         default=lambda: datetime.now(timezone.utc), index=True)
    event_type  = Column(String,   nullable=False)   # see EVENT_TYPES below
    agent_name  = Column(String,   nullable=True)    # node / agent that produced this event
    status      = Column(String,   nullable=False, default="info")
    # info | running | success | failure
    message     = Column(Text,     nullable=False)
    duration_ms = Column(Float,    nullable=True)    # populated for node-complete events
    meta        = Column(Text,     nullable=True)    # JSON — optional extra structured data


# Allowed event_type values — used by frontend for icons and filters
EVENT_TYPES = {
    "email_detected",
    "attachment_extracted",
    "node_started",
    "node_completed",
    "node_failed",
    "spam_detected",
    "routing_decision",
    "agent_invoked",
    "jira_ticket_created",
    "calendar_event_created",
    "reply_sent",
    "review_queued",
    "processing_completed",
    "processing_failed",
}


def emit_event(
    email_id: str | None,
    event_type: str,
    message: str,
    agent_name: str | None = None,
    status: str = "info",
    duration_ms: float | None = None,
    meta: dict | None = None,
) -> None:
    """
    Persist a structured execution event.
    Never raises — silently skips on any DB error so
    event emission never interrupts the processing pipeline.
    """
    try:
        db = SessionLocal()
        try:
            record = ExecutionEvent(
                email_id    = email_id,
                event_type  = event_type,
                agent_name  = agent_name,
                status      = status,
                message     = message,
                duration_ms = duration_ms,
                meta        = json.dumps(meta) if meta else None,
            )
            db.add(record)
            db.commit()
        finally:
            db.close()
    except Exception:
        pass   # event emission must never crash the pipeline


def get_recent_events(db, limit: int = 200) -> list:
    """Return the latest `limit` events ordered oldest-first for display."""
    rows = (
        db.query(ExecutionEvent)
        .order_by(ExecutionEvent.id.desc())
        .limit(limit)
        .all()
    )
    return list(reversed(rows))   # oldest first within the window


def get_events_since(db, last_id: int, limit: int = 100) -> list:
    """Return events with id > last_id for SSE streaming."""
    return (
        db.query(ExecutionEvent)
        .filter(ExecutionEvent.id > last_id)
        .order_by(ExecutionEvent.id.asc())
        .limit(limit)
        .all()
    )

