from sqlalchemy import create_engine, Column, String, Float, Boolean, DateTime, Text, Integer
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, timezone
import json
from src.config import DATABASE_URL

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class EmailRecord(Base):
    __tablename__ = "emails"

    id = Column(String, primary_key=True)          # Gmail message_id
    thread_id = Column(String, nullable=False)
    sender = Column(String)
    sender_domain = Column(String)
    subject = Column(String)
    received_at = Column(DateTime)
    attachment_type = Column(String, default="text")  # text | pdf | image
    raw_content = Column(Text)
    is_spam = Column(Boolean, default=False)
    client_name = Column(String, nullable=True)
    processed = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class AnalysisRecord(Base):
    __tablename__ = "analysis"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email_id = Column(String)
    category = Column(String)
    intent = Column(Text)
    urgency = Column(String)
    confidence = Column(Float)
    required_agents = Column(Text)   # stored as JSON string
    execution_plan = Column(Text)    # stored as JSON string
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ActionRecord(Base):
    __tablename__ = "actions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email_id = Column(String)
    agent_name = Column(String)
    action_taken = Column(Text)
    status = Column(String)          # success | failed | skipped
    error_message = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ReviewQueueRecord(Base):
    __tablename__ = "review_queue"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email_id = Column(String)
    analysis_snapshot = Column(Text)  # JSON snapshot of analysis
    status = Column(String, default="pending")  # pending | approved | rejected
    reviewed_by = Column(String, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class NotificationRecord(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message = Column(Text)
    type = Column(String)   # jira | calendar | system
    seen = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ClientRecord(Base):
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, unique=True)
    email_domain = Column(String, unique=True)
    jira_project_key = Column(String)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- Helper functions ---

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


def add_to_review_queue(db, email_id: str, analysis: dict):
    record = ReviewQueueRecord(
        email_id=email_id,
        analysis_snapshot=json.dumps(analysis)
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
