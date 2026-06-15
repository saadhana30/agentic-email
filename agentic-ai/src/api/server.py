"""
FastAPI server
--------------
Serves the frontend and exposes REST endpoints for:
- Dashboard data (emails, analysis, actions, review queue)
- Human-in-loop: approve / reject from review queue
- Client management (add/list clients)
- SSE endpoint for real-time in-app notifications
"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.database.db import (
    get_db, init_db, update_review_status, get_pending_review,
    SessionLocal, NotificationRecord, ReviewQueueRecord,
    EmailRecord, AnalysisRecord, ActionRecord, ClientRecord,
    add_notification
)
from src.graph.workflow import run_graph_for_email
from src.config import CONFIDENCE_THRESHOLD

logger = logging.getLogger(__name__)

# Always resolve paths relative to this file's location (src/api/server.py)
# So it works no matter which directory you run from
BASE_DIR = Path(__file__).resolve().parent.parent.parent  # → agentic-ai/
FRONTEND_DIR = BASE_DIR / "frontend"

app = FastAPI(title="ClientMail AI", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend static files using absolute path
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ── Pydantic models ──────────────────────────────────────────────────────────

class ReviewAction(BaseModel):
    action: str       # "approve" or "reject"
    reviewed_by: str = "admin"

class NewClient(BaseModel):
    name: str
    email_domain: str
    jira_project_key: str


# ── Startup ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
def on_startup():
    init_db()
    logger.info("Database initialised")


# ── Frontend ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


# ── SSE: Real-time notifications ─────────────────────────────────────────────

@app.get("/events")
async def sse_notifications():
    """
    Server-Sent Events endpoint.
    Frontend JS listens here for live popup notifications.
    """
    async def event_generator():
        last_id = 0
        while True:
            db = SessionLocal()
            try:
                new_notifs = (
                    db.query(NotificationRecord)
                    .filter(NotificationRecord.id > last_id, NotificationRecord.seen == False)
                    .order_by(NotificationRecord.id)
                    .all()
                )
                for notif in new_notifs:
                    last_id = notif.id
                    yield {
                        "data": json.dumps({
                            "id": notif.id,
                            "message": notif.message,
                            "type": notif.type,
                            "created_at": notif.created_at.isoformat(),
                        })
                    }
                    # Mark as seen
                    notif.seen = True
                    db.commit()
            finally:
                db.close()
            await asyncio.sleep(3)

    return EventSourceResponse(event_generator())


# ── Dashboard data endpoints ──────────────────────────────────────────────────

@app.get("/api/emails")
def get_emails(limit: int = 100, filter: str = "all", db: Session = Depends(get_db)):
    query = db.query(EmailRecord)

    if filter == "pending":
        processed_ids = db.query(ActionRecord.email_id).filter(ActionRecord.status == "success").distinct()
        query = query.filter(EmailRecord.is_spam == False, EmailRecord.id.notin_(processed_ids))
    elif filter == "executed":
        processed_ids = db.query(ActionRecord.email_id).filter(ActionRecord.status == "success").distinct()
        query = query.filter(EmailRecord.id.in_(processed_ids))
    # filter == "all" → return everything, frontend handles client/spam/other split
        # Executed: emails that have at least one successful action
        processed_ids = db.query(ActionRecord.email_id).filter(ActionRecord.status == "success").distinct()
        query = query.filter(EmailRecord.id.in_(processed_ids))

    emails = query.order_by(EmailRecord.created_at.desc()).limit(limit).all()
    return [
        {
            "id": e.id,
            "sender": e.sender,
            "subject": e.subject,
            "client_name": e.client_name,
            "is_spam": e.is_spam,
            "attachment_type": e.attachment_type,
            "received_at": e.received_at.isoformat() if e.received_at else None,
        }
        for e in emails
    ]


@app.get("/api/analysis/{email_id}")
def get_analysis(email_id: str, db: Session = Depends(get_db)):
    record = db.query(AnalysisRecord).filter(AnalysisRecord.email_id == email_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return {
        "category": record.category,
        "intent": record.intent,
        "urgency": record.urgency,
        "confidence": record.confidence,
        "required_agents": json.loads(record.required_agents or "[]"),
        "execution_plan": json.loads(record.execution_plan or "[]"),
    }


@app.get("/api/actions/{email_id}")
def get_actions(email_id: str, db: Session = Depends(get_db)):
    records = db.query(ActionRecord).filter(ActionRecord.email_id == email_id).all()
    return [
        {
            "agent_name": r.agent_name,
            "action_taken": json.loads(r.action_taken or "{}"),
            "status": r.status,
            "error_message": r.error_message,
            "timestamp": r.timestamp.isoformat(),
        }
        for r in records
    ]


@app.get("/api/review-queue")
def get_review_queue(db: Session = Depends(get_db)):
    records = (
        db.query(ReviewQueueRecord)
        .filter(ReviewQueueRecord.status == "pending")
        .order_by(ReviewQueueRecord.created_at.desc())
        .all()
    )
    result = []
    for r in records:
        email = db.query(EmailRecord).filter(EmailRecord.id == r.email_id).first()
        result.append({
            "review_id": r.id,
            "email_id": r.email_id,
            "sender": email.sender if email else "",
            "subject": email.subject if email else "",
            "analysis": json.loads(r.analysis_snapshot or "{}"),
            "created_at": r.created_at.isoformat(),
        })
    return result


# ── Human-in-loop: approve / reject ──────────────────────────────────────────

@app.post("/api/review/{review_id}")
def handle_review(review_id: int, body: ReviewAction, db: Session = Depends(get_db)):
    record = get_pending_review(db, review_id)
    if not record:
        raise HTTPException(status_code=404, detail="Review not found")
    if record.status != "pending":
        raise HTTPException(status_code=400, detail="Already reviewed")

    update_review_status(db, review_id, body.action, body.reviewed_by)

    if body.action == "approve":
        # Re-fetch the email and resume graph with full confidence
        email_record = db.query(EmailRecord).filter(EmailRecord.id == record.email_id).first()
        if email_record:
            analysis = json.loads(record.analysis_snapshot or "{}")
            # Force confidence to pass threshold so supervisor auto-executes
            analysis["confidence"] = 1.0

            email_dict = {
                "message_id": email_record.id,
                "thread_id": email_record.thread_id,
                "sender": email_record.sender,
                "sender_domain": email_record.sender_domain,
                "subject": email_record.subject,
                "received_at": email_record.received_at,
                "raw_content": email_record.raw_content,
                "processed_content": email_record.raw_content,
                "attachment_type": email_record.attachment_type,
            }
            # Run graph — this time it will auto-execute
            run_graph_for_email(email_dict)
            add_notification(db, f"✅ Review approved — processing email from {email_record.sender}", "system")

    return {"status": "ok", "action": body.action}


# ── Client management ─────────────────────────────────────────────────────────

@app.get("/api/clients")
def get_clients(db: Session = Depends(get_db)):
    clients = db.query(ClientRecord).all()
    return [
        {
            "id": c.id,
            "name": c.name,
            "email_domain": c.email_domain,
            "jira_project_key": c.jira_project_key,
        }
        for c in clients
    ]


@app.post("/api/clients")
def add_client(body: NewClient, db: Session = Depends(get_db)):
    existing = db.query(ClientRecord).filter(ClientRecord.email_domain == body.email_domain).first()
    if existing:
        raise HTTPException(status_code=400, detail="Client with this domain already exists")

    client = ClientRecord(
        name=body.name,
        email_domain=body.email_domain,
        jira_project_key=body.jira_project_key,
    )
    db.add(client)
    db.commit()

    # Auto-reclassify existing emails so this client's past emails get tagged
    from src.nodes.spam_classifier import _is_spam
    emails = db.query(EmailRecord).filter(
        EmailRecord.sender_domain == body.email_domain,
        EmailRecord.is_spam == False,
        EmailRecord.client_name == None
    ).all()
    for e in emails:
        e.client_name = body.name
    db.commit()

    return {"status": "created", "id": client.id}


@app.delete("/api/clients/{client_id}")
def delete_client(client_id: int, db: Session = Depends(get_db)):
    client = db.query(ClientRecord).filter(ClientRecord.id == client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    db.delete(client)
    db.commit()
    return {"status": "deleted"}


# ── Notifications ─────────────────────────────────────────────────────────────

@app.get("/api/notifications")
def get_notifications(limit: int = 20, db: Session = Depends(get_db)):
    notifs = (
        db.query(NotificationRecord)
        .order_by(NotificationRecord.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": n.id,
            "message": n.message,
            "type": n.type,
            "seen": n.seen,
            "created_at": n.created_at.isoformat(),
        }
        for n in notifs
    ]


@app.get("/api/stats")
def get_stats(db: Session = Depends(get_db)):
    total = db.query(EmailRecord).count()
    spam = db.query(EmailRecord).filter(EmailRecord.is_spam == True).count()
    clients = db.query(EmailRecord).filter(EmailRecord.is_spam == False, EmailRecord.client_name != None).count()
    pending_review = db.query(ReviewQueueRecord).filter(ReviewQueueRecord.status == "pending").count()
    actions = db.query(ActionRecord).filter(ActionRecord.status == "success").count()
    return {
        "total_emails": total,
        "spam_detected": spam,
        "client_emails": clients,
        "pending_review": pending_review,
        "actions_taken": actions,
    }


# ── Reclassify existing emails ────────────────────────────────────────────────

@app.post("/api/reclassify")
def reclassify_emails(db: Session = Depends(get_db)):
    """
    Re-run spam/client classification on all existing emails.
    Call this after adding new clients so old emails get tagged correctly.
    """
    from src.nodes.spam_classifier import _is_spam

    emails = db.query(EmailRecord).all()
    updated = 0
    for e in emails:
        sender = (e.sender or "").lower()
        sender_domain = (e.sender_domain or "").lower()
        subject = (e.subject or "").lower()

        if _is_spam(sender, subject):
            e.is_spam = True
            e.client_name = None
            updated += 1
        else:
            from src.database.db import get_client_by_domain
            client = get_client_by_domain(db, sender_domain)
            if not client:
                client = get_client_by_domain(db, sender)
            if client:
                e.is_spam = False
                e.client_name = client.name
                updated += 1
            else:
                e.is_spam = False
                e.client_name = None

    db.commit()
    return {"status": "ok", "updated": updated, "total": len(emails)}
