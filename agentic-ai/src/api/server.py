"""
FastAPI server
--------------
Serves the frontend and exposes REST endpoints.

New in this version:
  - JWT authentication (Feature 8): /api/auth/login, /api/auth/refresh, /api/auth/me
  - All data endpoints protected by get_current_user dependency
  - Admin-only endpoints protected by require_admin dependency
  - POST /api/reprocess/{email_id} — manual reprocessing (Feature 7)
  - GET  /api/traces/{email_id}    — execution traces (Feature 4)
  - GET  /api/attachments/{email_id} — attachment records (Feature 3)
  - Fixed duplicate filter=executed logic in /api/emails
"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.database.db import (
    get_db, init_db, update_review_status, get_pending_review,
    SessionLocal, NotificationRecord, ReviewQueueRecord,
    EmailRecord, AnalysisRecord, ActionRecord, ClientRecord,
    ExecutionTrace, AttachmentRecord, ExecutionEvent,
    add_notification, get_traces_for_email, get_attachments_for_email,
    get_recent_events, get_events_since,
    create_user, get_user_by_username,
)
from src.graph.workflow import run_graph_for_email
from src.config import CONFIDENCE_THRESHOLD
from src.auth import (
    hash_password, verify_password,
    create_access_token, create_refresh_token, decode_token,
    get_current_user, require_admin,
    TokenResponse,
)
from src.agents.nova_agent import NovaAgent

# Nova agent singleton — one instance, sessions tracked internally
_nova = NovaAgent()

logger = logging.getLogger(__name__)

BASE_DIR     = Path(__file__).resolve().parent.parent.parent  # → agentic-ai/
FRONTEND_DIR = BASE_DIR / "frontend"

app = FastAPI(title="NovaSphere Agent", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ── Pydantic models ───────────────────────────────────────────────────────────

class ReviewAction(BaseModel):
    action: str
    reviewed_by: str = "admin"

class NewClient(BaseModel):
    name: str
    email_domain: str
    jira_project_key: str

class RegisterUser(BaseModel):
    username: str
    email: str
    password: str
    role: str = "user"

class RefreshRequest(BaseModel):
    refresh_token: str

class NovaChatRequest(BaseModel):
    message: str
    session_id: str = "default"
    email_id: str | None = None    # optional — focus context on a specific email


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
def on_startup():
    init_db()
    _ensure_default_admin()
    logger.info("Database initialised")


def _ensure_default_admin():
    """Create a default admin account if no users exist."""
    db = SessionLocal()
    try:
        from src.database.db import UserRecord
        if db.query(UserRecord).count() == 0:
            create_user(
                db,
                username="admin",
                email="admin@localhost",
                hashed_password=hash_password("admin123"),
                role="admin",
            )
            logger.warning(
                "Created default admin account: admin / admin123 — "
                "CHANGE THIS PASSWORD IN PRODUCTION"
            )
    finally:
        db.close()


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    return FileResponse(str(FRONTEND_DIR / "index.html"))

@app.get("/login", response_class=HTMLResponse)
def serve_login():
    return FileResponse(str(FRONTEND_DIR / "login.html"))


# ── Auth endpoints ────────────────────────────────────────────────────────────

@app.post("/api/auth/login", response_model=TokenResponse)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    user = get_user_by_username(db, form_data.username)
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated")

    access_token  = create_access_token(user.id, user.role)
    refresh_token = create_refresh_token(user.id, user.role)
    logger.info(f"Auth: login successful for '{user.username}' (role={user.role})")
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@app.post("/api/auth/refresh", response_model=TokenResponse)
def refresh_token(body: RefreshRequest, db: Session = Depends(get_db)):
    payload = decode_token(body.refresh_token)
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    from src.database.db import get_user_by_id
    user = get_user_by_id(db, int(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found")

    access_token  = create_access_token(user.id, user.role)
    refresh_token_ = create_refresh_token(user.id, user.role)
    return TokenResponse(access_token=access_token, refresh_token=refresh_token_)


@app.get("/api/auth/me")
def get_me(current_user=Depends(get_current_user)):
    return {
        "id":       current_user.id,
        "username": current_user.username,
        "email":    current_user.email,
        "role":     current_user.role,
    }


@app.post("/api/auth/register")
def register_user(
    body: RegisterUser,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    """Admin-only: create a new user account."""
    existing = get_user_by_username(db, body.username)
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")
    user = create_user(
        db,
        username=body.username,
        email=body.email,
        hashed_password=hash_password(body.password),
        role=body.role,
    )
    return {"status": "created", "id": user.id, "username": user.username}


# ── SSE: Real-time notifications (auth-aware via query param token) ───────────

@app.get("/events")
async def sse_notifications(token: str = "", db: Session = Depends(get_db)):
    """
    SSE endpoint. Frontend passes ?token=<jwt> since EventSource can't
    set Authorization headers.
    """
    # Validate token
    try:
        decode_token(token)
    except HTTPException:
        # Return empty stream rather than crashing
        async def empty():
            return
        return EventSourceResponse(empty())

    async def event_generator():
        last_id = 0
        while True:
            db2 = SessionLocal()
            try:
                new_notifs = (
                    db2.query(NotificationRecord)
                    .filter(
                        NotificationRecord.id > last_id,
                        NotificationRecord.seen == False,
                    )
                    .order_by(NotificationRecord.id)
                    .all()
                )
                for notif in new_notifs:
                    last_id = notif.id
                    yield {
                        "data": json.dumps({
                            "id":         notif.id,
                            "message":    notif.message,
                            "type":       notif.type,
                            "created_at": notif.created_at.isoformat(),
                        })
                    }
                    notif.seen = True
                    db2.commit()
            finally:
                db2.close()
            await asyncio.sleep(3)

    return EventSourceResponse(event_generator())


# ── Dashboard data ────────────────────────────────────────────────────────────

@app.get("/api/emails")
def get_emails(
    limit: int = 100,
    filter: str = "all",
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    query = db.query(EmailRecord)

    if filter == "pending":
        processed_ids = (
            db.query(ActionRecord.email_id)
            .filter(ActionRecord.status == "success")
            .distinct()
        )
        query = query.filter(
            EmailRecord.is_spam == False,
            EmailRecord.id.notin_(processed_ids),
        )
    elif filter == "executed":
        # Fixed: removed the accidental duplicate block from the original
        processed_ids = (
            db.query(ActionRecord.email_id)
            .filter(ActionRecord.status == "success")
            .distinct()
        )
        query = query.filter(EmailRecord.id.in_(processed_ids))
    # filter == "all" → return everything; frontend filters by client/spam/other

    emails = query.order_by(EmailRecord.created_at.desc()).limit(limit).all()
    return [
        {
            "id":              e.id,
            "thread_id":       e.thread_id,
            "sender":          e.sender,
            "subject":         e.subject,
            "client_name":     e.client_name,
            "is_spam":         e.is_spam,
            "attachment_type": e.attachment_type,
            "received_at":     e.received_at.isoformat() if e.received_at else None,
        }
        for e in emails
    ]


@app.get("/api/analysis/{email_id}")
def get_analysis(
    email_id: str,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    record = (
        db.query(AnalysisRecord)
        .filter(AnalysisRecord.email_id == email_id)
        .first()
    )
    if not record:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return {
        "category":       record.category,
        "intent":         record.intent,
        "urgency":        record.urgency,
        "confidence":     record.confidence,
        "required_agents": json.loads(record.required_agents or "[]"),
        "execution_plan":  json.loads(record.execution_plan or "[]"),
    }


@app.get("/api/actions/{email_id}")
def get_actions(
    email_id: str,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    records = (
        db.query(ActionRecord)
        .filter(ActionRecord.email_id == email_id)
        .all()
    )
    return [
        {
            "agent_name":    r.agent_name,
            "action_taken":  json.loads(r.action_taken or "{}"),
            "status":        r.status,
            "error_message": r.error_message,
            "timestamp":     r.timestamp.isoformat(),
        }
        for r in records
    ]


@app.get("/api/review-queue")
def get_review_queue(
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
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
            "review_id":  r.id,
            "email_id":   r.email_id,
            "sender":     email.sender  if email else "",
            "subject":    email.subject if email else "",
            "analysis":   json.loads(r.analysis_snapshot or "{}"),
            "created_at": r.created_at.isoformat(),
        })
    return result


# ── Human-in-loop ─────────────────────────────────────────────────────────────

@app.post("/api/review/{review_id}")
def handle_review(
    review_id: int,
    body: ReviewAction,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    record = get_pending_review(db, review_id)
    if not record:
        raise HTTPException(status_code=404, detail="Review not found")
    if record.status != "pending":
        raise HTTPException(status_code=400, detail="Already reviewed")

    update_review_status(db, review_id, body.action, body.reviewed_by)

    if body.action == "approve":
        email_record = (
            db.query(EmailRecord)
            .filter(EmailRecord.id == record.email_id)
            .first()
        )
        if email_record:
            analysis = json.loads(record.analysis_snapshot or "{}")
            analysis["confidence"] = 1.0

            email_dict = {
                "message_id":       email_record.id,
                "thread_id":        email_record.thread_id,
                "in_reply_to":      email_record.in_reply_to,
                "references":       email_record.references,
                "sender":           email_record.sender,
                "sender_domain":    email_record.sender_domain,
                "subject":          email_record.subject,
                "received_at":      email_record.received_at,
                "raw_content":      email_record.raw_content,
                "processed_content": email_record.raw_content,
                "attachment_type":  email_record.attachment_type,
                "attachments":      [],
            }
            run_graph_for_email(email_dict)
            add_notification(
                db,
                f"✅ Review approved — processing email from {email_record.sender}",
                "system",
            )

    return {"status": "ok", "action": body.action}


# ── Manual reprocess (Feature 7) ──────────────────────────────────────────────

@app.post("/api/reprocess/{email_id}")
def reprocess_email(
    email_id: str,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    """
    Reload email from DB and re-run the full LangGraph pipeline.
    Updates analysis, actions, notifications.
    Audit history is preserved (new records appended).
    """
    email_record = db.query(EmailRecord).filter(EmailRecord.id == email_id).first()
    if not email_record:
        raise HTTPException(status_code=404, detail="Email not found")

    # Fetch attachment records to rebuild attachments list
    attachments = get_attachments_for_email(db, email_id)
    att_list = [
        {
            "filename":        a.filename,
            "mime_type":       a.mime_type,
            "attachment_type": a.attachment_type,
            "extracted_text":  a.extracted_text or "",
        }
        for a in attachments
    ]

    email_dict = {
        "message_id":       email_record.id,
        "thread_id":        email_record.thread_id,
        "in_reply_to":      email_record.in_reply_to,
        "references":       email_record.references,
        "sender":           email_record.sender,
        "sender_domain":    email_record.sender_domain,
        "subject":          email_record.subject,
        "received_at":      email_record.received_at,
        "raw_content":      email_record.raw_content,
        "processed_content": email_record.raw_content,
        "attachment_type":  email_record.attachment_type,
        "attachments":      att_list,
    }

    logger.info(f"Manual reprocess triggered for email {email_id}")
    final_state = run_graph_for_email(email_dict)

    add_notification(
        db,
        f"🔄 Reprocessed email from {email_record.sender}",
        "system",
    )

    return {
        "status":      "reprocessed",
        "email_id":    email_id,
        "route":       final_state.get("route"),
        "next_agents": final_state.get("next_agents"),
    }


# ── Execution traces (Feature 4) ──────────────────────────────────────────────

@app.get("/api/traces/{email_id}")
def get_execution_traces(
    email_id: str,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    traces = get_traces_for_email(db, email_id)
    return [
        {
            "node_name":         t.node_name,
            "start_time":        t.start_time.isoformat(),
            "end_time":          t.end_time.isoformat() if t.end_time else None,
            "duration_ms":       t.duration_ms,
            "success":           t.success,
            "exception_message": t.exception_message,
        }
        for t in traces
    ]


# ── Attachments (Feature 3) ───────────────────────────────────────────────────

@app.get("/api/attachments/{email_id}")
def get_email_attachments(
    email_id: str,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    atts = get_attachments_for_email(db, email_id)
    return [
        {
            "id":              a.id,
            "filename":        a.filename,
            "mime_type":       a.mime_type,
            "attachment_type": a.attachment_type,
            "char_count":      a.char_count,
            "extracted_text":  (a.extracted_text or "")[:500] + (
                "..." if len(a.extracted_text or "") > 500 else ""
            ),
        }
        for a in atts
    ]


# ── Client management ─────────────────────────────────────────────────────────

@app.get("/api/clients")
def get_clients(
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    clients = db.query(ClientRecord).all()
    return [
        {
            "id":               c.id,
            "name":             c.name,
            "email_domain":     c.email_domain,
            "jira_project_key": c.jira_project_key,
        }
        for c in clients
    ]


@app.post("/api/clients")
def add_client(
    body: NewClient,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    existing = (
        db.query(ClientRecord)
        .filter(ClientRecord.email_domain == body.email_domain)
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=400,
            detail="Client with this domain already exists",
        )

    client = ClientRecord(
        name=body.name,
        email_domain=body.email_domain,
        jira_project_key=body.jira_project_key,
    )
    db.add(client)
    db.commit()

    # Auto-tag existing emails from this domain
    emails = (
        db.query(EmailRecord)
        .filter(
            EmailRecord.sender_domain == body.email_domain,
            EmailRecord.is_spam == False,
            EmailRecord.client_name == None,
        )
        .all()
    )
    for e in emails:
        e.client_name = body.name
    db.commit()

    return {"status": "created", "id": client.id}


@app.delete("/api/clients/{client_id}")
def delete_client(
    client_id: int,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    client = db.query(ClientRecord).filter(ClientRecord.id == client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    db.delete(client)
    db.commit()
    return {"status": "deleted"}


# ── Notifications ─────────────────────────────────────────────────────────────

@app.get("/api/notifications")
def get_notifications(
    limit: int = 20,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    notifs = (
        db.query(NotificationRecord)
        .order_by(NotificationRecord.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id":         n.id,
            "message":    n.message,
            "type":       n.type,
            "seen":       n.seen,
            "created_at": n.created_at.isoformat(),
        }
        for n in notifs
    ]


@app.get("/api/stats")
def get_stats(
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    total          = db.query(EmailRecord).count()
    spam           = db.query(EmailRecord).filter(EmailRecord.is_spam == True).count()
    clients        = db.query(EmailRecord).filter(
        EmailRecord.is_spam == False,
        EmailRecord.client_name != None,
    ).count()
    pending_review = db.query(ReviewQueueRecord).filter(
        ReviewQueueRecord.status == "pending"
    ).count()
    actions        = db.query(ActionRecord).filter(
        ActionRecord.status == "success"
    ).count()
    return {
        "total_emails":   total,
        "spam_detected":  spam,
        "client_emails":  clients,
        "pending_review": pending_review,
        "actions_taken":  actions,
    }


# ── Nova Assistant (AI chat) ──────────────────────────────────────────────────

@app.post("/api/nova/chat")
def nova_chat(
    body: NovaChatRequest,
    _user=Depends(get_current_user),
):
    """
    Nova Assistant chat endpoint.
    Accepts a natural-language question, returns Nova's answer.
    Maintains per-session conversation history server-side.
    """
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    # Use user-scoped session so each user has their own history
    session_id = f"{_user.id}:{body.session_id}"

    logger.info(
        f"Nova: question from user={_user.username} "
        f"session={body.session_id} "
        f"email_ctx={body.email_id or 'none'}"
    )

    answer = _nova.chat(
        question=body.message.strip(),
        session_id=session_id,
        email_id=body.email_id,
    )
    return {"answer": answer, "session_id": body.session_id}


@app.get("/api/nova/history")
def nova_history(
    session_id: str = "default",
    _user=Depends(get_current_user),
):
    """Return the conversation history for a session."""
    scoped_id = f"{_user.id}:{session_id}"
    history   = _nova.get_history(scoped_id)
    return {"history": history, "session_id": session_id}


@app.delete("/api/nova/history")
def nova_clear_history(
    session_id: str = "default",
    _user=Depends(get_current_user),
):
    """Clear conversation history for a session."""
    scoped_id = f"{_user.id}:{session_id}"
    _nova.clear_history(scoped_id)
    return {"status": "cleared", "session_id": session_id}


# ── Live Execution Monitor ────────────────────────────────────────────────────

from zoneinfo import ZoneInfo as _ZoneInfo
_IST = _ZoneInfo("Asia/Kolkata")


def _to_ist_iso(dt) -> str | None:
    """
    Convert a datetime to Asia/Kolkata and return an ISO-8601 string
    that includes the +05:30 offset.  Returns None if dt is None.
    The offset in the string is what tells the browser the exact wall-clock
    time — no browser-timezone dependency.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Treat naive datetimes (stored as UTC) as UTC before converting
        from datetime import timezone as _tz
        dt = dt.replace(tzinfo=_tz.utc)
    return dt.astimezone(_IST).isoformat()


def _event_to_dict(e: ExecutionEvent) -> dict:
    """Serialize an ExecutionEvent row to a JSON-safe dict.
    Timestamps are always returned in Asia/Kolkata (IST, +05:30)."""
    return {
        "id":          e.id,
        "email_id":    e.email_id,
        "timestamp":   _to_ist_iso(e.timestamp),
        "event_type":  e.event_type,
        "agent_name":  e.agent_name,
        "status":      e.status,
        "message":     e.message,
        "duration_ms": e.duration_ms,
        "meta":        json.loads(e.meta) if e.meta else None,
    }


@app.get("/api/monitor/events")
def get_monitor_events(
    limit: int = 200,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Return the latest events for the Live Execution Monitor (initial load)."""
    events = get_recent_events(db, limit=min(limit, 200))
    return [_event_to_dict(e) for e in events]


@app.get("/api/monitor/events/stream")
async def stream_monitor_events(token: str = ""):
    """
    SSE stream for the Live Execution Monitor.
    Delivers new execution_events rows as they are written.
    Frontend passes ?token=<jwt> since EventSource cannot set headers.
    """
    try:
        decode_token(token)
    except HTTPException:
        async def _empty():
            return
        return EventSourceResponse(_empty())

    async def _generator():
        # Start from the current latest id so we only stream NEW events
        db2 = SessionLocal()
        try:
            last_row = (
                db2.query(ExecutionEvent)
                .order_by(ExecutionEvent.id.desc())
                .first()
            )
            last_id = last_row.id if last_row else 0
        finally:
            db2.close()

        while True:
            await asyncio.sleep(1.5)
            db3 = SessionLocal()
            try:
                new_events = get_events_since(db3, last_id, limit=50)
                for ev in new_events:
                    last_id = ev.id
                    yield {"data": json.dumps(_event_to_dict(ev))}
            finally:
                db3.close()

    return EventSourceResponse(_generator())


# ── Reclassify existing emails ────────────────────────────────────────────────

@app.post("/api/reclassify")
def reclassify_emails(
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    from src.nodes.spam_classifier import _is_spam
    from src.database.db import get_client_by_domain

    emails  = db.query(EmailRecord).all()
    updated = 0
    for e in emails:
        sender        = (e.sender or "").lower()
        sender_domain = (e.sender_domain or "").lower()
        subject       = (e.subject or "").lower()

        if _is_spam(sender, subject):
            e.is_spam    = True
            e.client_name = None
            updated += 1
        else:
            client = get_client_by_domain(db, sender_domain)
            if not client:
                client = get_client_by_domain(db, sender)
            if client:
                e.is_spam    = False
                e.client_name = client.name
                updated += 1
            else:
                e.is_spam    = False
                e.client_name = None

    db.commit()
    return {"status": "ok", "updated": updated, "total": len(emails)}
