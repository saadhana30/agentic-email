"""
nova_agent.py
-------------
Nova Assistant — AI operations assistant for the dashboard.

Nova has READ access to all operational data:
  emails, analysis, actions, review_queue, clients,
  notifications, execution_traces

Flow:
  1. Receive user question + conversation history
  2. Pull a rich snapshot of the current system state from SQLite
  3. Build a prompt: system_context + data_snapshot + history + question
  4. Call Ollama (llama3.2) with retry
  5. Return the answer

Nova never modifies data — it is read-only.
Nova maintains per-session conversation history on the server side.
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from src.tools.ollama_client import call_ollama, OllamaRetryExhausted
from src.utils.time_utils import now_ist, utc_to_ist, format_datetime_ist
from src.database.db import (
    SessionLocal,
    EmailRecord, AnalysisRecord, ActionRecord,
    ReviewQueueRecord, ClientRecord,
    NotificationRecord, ExecutionTrace,
)

logger = logging.getLogger(__name__)

# ── System prompt — tells Nova what it is and how to behave ──────────────────
NOVA_SYSTEM_PROMPT = """You are Nova, an intelligent AI operations assistant embedded in the NovaSphere Agent dashboard.
You must follow these rules when replying:
- Answer directly and concisely; avoid unnecessary introductions or restating the question.
- Prefer bullet points or very short paragraphs.
- Keep answers under 150 words unless the user explicitly requests more detail.
- Do not invent facts — only use data present in the provided context.
- When a specific `email_id` is provided, use only that email's detailed context for focused answers.

If the context lacks the requested information, reply: "I don't have that data.".

Today's date and time: {now}
"""

# ── In-memory session store: session_id → list of {role, content} ────────────
_sessions: dict[str, list[dict]] = {}

# Cached limited snapshot to avoid rebuilding full DB context on every request
_cached_snapshot: Optional[str] = None
_cached_snapshot_ts: Optional[datetime] = None
CACHE_TTL = timedelta(seconds=30)
_cached_stats: Optional[dict] = None
_cached_stats_ts: Optional[datetime] = None

MAX_HISTORY_TURNS = 20   # keep last 20 message pairs per session


class NovaAgent:

    def chat(
        self,
        question: str,
        session_id: str = "default",
        email_id: Optional[str] = None,
    ) -> str:
        """
        Main entry point.

        Args:
            question:   The user's natural-language question.
            session_id: Identifies the conversation session (per browser tab/user).
            email_id:   Optional — if the user is asking about a specific email,
                        pass its ID so Nova can fetch targeted context.

        Returns:
            Nova's answer as a string.
        """
        # ── Retrieve or create session history ────────────────────────────────
        history = _sessions.setdefault(session_id, [])

        # Intent classification: decide if this is a fast SQL-only informational
        # query (counts, simple stats, today's activity, review queue status)
        is_fast = self._is_informational(question) and email_id is None

        if is_fast:
            db = SessionLocal()
            try:
                answer = self._answer_fast(question, db)
            finally:
                db.close()

            # Save to history and return immediately (no LLM call)
            history.append({"role": "user",      "content": question})
            history.append({"role": "assistant", "content": answer})
            if len(history) > MAX_HISTORY_TURNS * 2:
                _sessions[session_id] = history[-(MAX_HISTORY_TURNS * 2):]
            return answer

        # ── Pull data snapshot (AI mode) ──────────────────────────────────────
        snapshot = self._build_data_snapshot(email_id=email_id)

        # ── Build the full prompt ─────────────────────────────────────────────
        prompt = self._build_prompt(
            question=question,
            snapshot=snapshot,
            history=history,
        )

        # ── Call Ollama ───────────────────────────────────────────────────────
        try:
            answer = call_ollama(prompt, temperature=0.3)
        except OllamaRetryExhausted:
            answer = (
                "I'm unable to reach the AI model right now. "
                "Please check that Ollama is running (`ollama serve`) and try again."
            )
        except Exception as e:
            logger.error(f"NovaAgent: unexpected error — {e}")
            answer = "Something went wrong while processing your question. Please try again."

        # ── Append to history ─────────────────────────────────────────────────
        history.append({"role": "user",      "content": question})
        history.append({"role": "assistant", "content": answer})

        # Trim to last MAX_HISTORY_TURNS turns (each turn = 2 messages)
        if len(history) > MAX_HISTORY_TURNS * 2:
            _sessions[session_id] = history[-(MAX_HISTORY_TURNS * 2):]

        return answer

    def get_history(self, session_id: str) -> list[dict]:
        return _sessions.get(session_id, [])

    def clear_history(self, session_id: str) -> None:
        _sessions.pop(session_id, None)

    # ── Intent classification & fast-path helpers ──────────────────────────
    def _is_informational(self, question: str) -> bool:
        q = (question or "").lower()
        # If user explicitly asks for explanation/summary/analysis, prefer AI mode
        ai_indicators = ["explain", "summar", "why", "trend", "analysis", "diagnos", "failure", "fail", "reason"]
        if any(tok in q for tok in ai_indicators):
            return False

        info_indicators = ["how many", "number of", "total", "count", "what is", "show", "list", "today", "pending", "review", "spam", "ticket", "tickets", "clients", "actions", "stats", "statistics"]
        if any(tok in q for tok in info_indicators):
            # also require one of the concrete topics
            concrete = ["email", "emails", "spam", "ticket", "tickets", "client", "clients", "review", "pending", "action", "actions", "today", "stats"]
            return any(c in q for c in concrete)
        return False

    def _gather_stats(self, db) -> dict:
        """Return a small dict of commonly requested stats (cached for CACHE_TTL)."""
        global _cached_stats, _cached_stats_ts
        now_utc = datetime.now(timezone.utc)
        if _cached_stats and _cached_stats_ts and (now_utc - _cached_stats_ts) < CACHE_TTL:
            return _cached_stats

        # Day boundary in IST, converted to UTC for DB queries
        today_ist = now_ist().replace(hour=0, minute=0, second=0, microsecond=0)
        today_utc = today_ist.astimezone(timezone.utc)

        stats = {}
        stats["total_emails"] = db.query(EmailRecord).count()
        stats["spam_count"] = db.query(EmailRecord).filter(EmailRecord.is_spam == True).count()
        stats["client_emails"] = db.query(EmailRecord).filter(
            EmailRecord.is_spam == False,
            EmailRecord.client_name != None,
        ).count()
        stats["pending_review"] = db.query(ReviewQueueRecord).filter(ReviewQueueRecord.status == "pending").count()
        stats["total_actions"] = db.query(ActionRecord).filter(ActionRecord.status == "success").count()
        stats["failed_actions"] = db.query(ActionRecord).filter(ActionRecord.status == "failed").count()

        stats["today_emails"] = db.query(EmailRecord).filter(EmailRecord.created_at >= today_utc).count()
        stats["today_actions"] = db.query(ActionRecord).filter(ActionRecord.timestamp >= today_utc, ActionRecord.status == "success").count()
        stats["today_spam"] = db.query(EmailRecord).filter(EmailRecord.created_at >= today_utc, EmailRecord.is_spam == True).count()

        _cached_stats = stats
        _cached_stats_ts = now_utc
        return stats

    def _answer_fast(self, question: str, db) -> str:
        stats = self._gather_stats(db)
        q = (question or "").lower()

        # Targeted quick answers
        if "spam" in q and ("how many" in q or "total" in q or "count" in q):
            return f"- Spam emails: {stats['spam_count']}"
        if ("review" in q or "pending" in q) and ("how many" in q or "pending" in q or "review" in q):
            return f"- Pending review items: {stats['pending_review']}"
        if "today" in q:
            return (
                f"- New emails today: {stats['today_emails']}\n"
                f"- Successful actions today: {stats['today_actions']}\n"
                f"- Spam blocked today: {stats['today_spam']}"
            )

        # Client-level quick: most tickets
        if "most" in q and "ticket" in q:
            # compute per-client Jira ticket counts (successful)
            rows = (
                db.query(EmailRecord.client_name, ActionRecord)
                .join(ActionRecord, ActionRecord.email_id == EmailRecord.id)
                .filter(ActionRecord.agent_name == "jira_agent", ActionRecord.status == "success")
                .all()
            )
            # Fallback to general stats if DB join is not available
            return (
                f"- Total emails: {stats['total_emails']} | Client emails: {stats['client_emails']} | "
                f"Pending review: {stats['pending_review']}"
            )

        # Default informational summary
        return (
            f"- Total emails: {stats['total_emails']}\n"
            f"- Client emails: {stats['client_emails']}\n"
            f"- Spam: {stats['spam_count']}\n"
            f"- Pending review: {stats['pending_review']}\n"
            f"- Successful actions: {stats['total_actions']} | Failed actions: {stats['failed_actions']}"
        )

    # ── Data snapshot ─────────────────────────────────────────────────────────

    def _build_data_snapshot(self, email_id: Optional[str] = None) -> str:
        """
        Query the database and format a rich, structured context string
        that Nova can reason over.
        """
        db = SessionLocal()
        try:
            now_utc = datetime.now(timezone.utc)

            # Use cached limited snapshot for non-email-specific requests
            global _cached_snapshot, _cached_snapshot_ts
            if not email_id and _cached_snapshot and _cached_snapshot_ts:
                if (now_utc - _cached_snapshot_ts) < CACHE_TTL:
                    return _cached_snapshot

            parts: list[str] = []

            # 1. System stats (compact)
            total_emails = db.query(EmailRecord).count()
            spam_count = db.query(EmailRecord).filter(EmailRecord.is_spam == True).count()
            client_emails = db.query(EmailRecord).filter(
                EmailRecord.is_spam == False,
                EmailRecord.client_name != None,
            ).count()
            pending_review = db.query(ReviewQueueRecord).filter(
                ReviewQueueRecord.status == "pending"
            ).count()
            total_actions = db.query(ActionRecord).filter(ActionRecord.status == "success").count()
            failed_actions = db.query(ActionRecord).filter(ActionRecord.status == "failed").count()

            parts.append(
                f"=== SYSTEM STATS ===\n"
                f"Total emails: {total_emails} | Client emails: {client_emails} | "
                f"Spam: {spam_count} | Pending review: {pending_review}\n"
                f"Successful actions: {total_actions} | Failed actions: {failed_actions}"
            )

            # 2. Top 10 recent emails (summary only)
            recent_emails = (
                db.query(EmailRecord)
                .order_by(EmailRecord.created_at.desc())
                .limit(10)
                .all()
            )
            if recent_emails:
                email_lines = []
                for e in recent_emails:
                    ts = format_datetime_ist(e.received_at, "%Y-%m-%d %H:%M %Z") if e.received_at else "unknown"
                    source = f"client:{e.client_name}" if e.client_name else ("spam" if e.is_spam else "other")
                    email_lines.append(
                        f"  [{ts}] id={e.id[:12]}... | from={e.sender} | subject=\"{e.subject}\" | source={source}"
                    )
                parts.append("=== RECENT EMAILS (top 10) ===\n" + "\n".join(email_lines))

            # 3. Top 10 recent actions (summary only)
            recent_actions = (
                db.query(ActionRecord)
                .order_by(ActionRecord.timestamp.desc())
                .limit(10)
                .all()
            )
            if recent_actions:
                action_lines = []
                for a in recent_actions:
                    ts = format_datetime_ist(a.timestamp, "%Y-%m-%d %H:%M %Z") if a.timestamp else "unknown"
                    action_lines.append(
                        f"  [{ts}] email_id={a.email_id[:12]}... | agent={a.agent_name} | status={a.status}"
                    )
                parts.append("=== RECENT ACTIONS (top 10) ===\n" + "\n".join(action_lines))

            # 4. Review queue count
            parts.append(f"=== REVIEW QUEUE ===\nPending items: {pending_review}")

            # 6. Today's activity (IST)
            stats = self._gather_stats(db)
            parts.append(
                f"=== TODAY'S ACTIVITY (IST) ===\n"
                f"New emails: {stats.get('today_emails', 0)} | Successful actions: {stats.get('today_actions', 0)} | "
                f"Spam blocked: {stats.get('today_spam', 0)}"
            )

            # 5. Recent failures (execution traces)
            failed_traces = (
                db.query(ExecutionTrace)
                .filter(ExecutionTrace.success == False)
                .order_by(ExecutionTrace.start_time.desc())
                .limit(10)
                .all()
            )
            if failed_traces:
                trace_lines = [
                    f"  email_id={t.email_id or 'N/A'} | node={t.node_name} | error=\"{t.exception_message or 'unknown'}\""
                    for t in failed_traces
                ]
                parts.append("=== RECENT NODE FAILURES ===\n" + "\n".join(trace_lines))

            # If a focused email is requested, append full details for that email only
            if email_id:
                email_rec = db.query(EmailRecord).filter(EmailRecord.id == email_id).first()
                analysis_rec = db.query(AnalysisRecord).filter(AnalysisRecord.email_id == email_id).first()
                actions_for = db.query(ActionRecord).filter(ActionRecord.email_id == email_id).all()
                traces_for = db.query(ExecutionTrace).filter(ExecutionTrace.email_id == email_id).all()
                review_for = db.query(ReviewQueueRecord).filter(ReviewQueueRecord.email_id == email_id).first()

                lines = [f"=== FOCUSED EMAIL: {email_id} ==="]
                if email_rec:
                    lines.append(
                        f"From: {email_rec.sender} | Subject: \"{email_rec.subject}\" | "
                        f"Client: {email_rec.client_name or 'unknown'} | "
                        f"Spam: {email_rec.is_spam} | Attachment: {email_rec.attachment_type}"
                    )
                if analysis_rec:
                    plan = json.loads(analysis_rec.execution_plan or "[]")
                    lines.append(
                        f"Analysis: category={analysis_rec.category} | "
                        f"urgency={analysis_rec.urgency} | confidence={analysis_rec.confidence:.2f} | "
                        f"intent=\"{analysis_rec.intent}\" | plan={plan}"
                    )
                for a in actions_for:
                    result = json.loads(a.action_taken or "{}")
                    lines.append(
                        f"Action: {a.agent_name} | status={a.status} | detail={result}"
                        + (f" | error={a.error_message}" if a.error_message else "")
                    )
                for t in traces_for:
                    sym = "✓" if t.success else "✗"
                    lines.append(
                        f"Trace {sym}: node={t.node_name} | duration={t.duration_ms:.0f}ms"
                        + (f" | error={t.exception_message}" if not t.success else "")
                    )
                if review_for:
                    lines.append(
                        f"Review queue: status={review_for.status} | created={review_for.created_at}"
                    )
                parts.append("\n".join(lines))

            snapshot = "\n\n".join(parts)

            # Cache the limited snapshot (only when not focused on a specific email)
            if not email_id:
                _cached_snapshot = snapshot
                _cached_snapshot_ts = now_utc

            return snapshot

        except Exception as e:
            logger.error(f"NovaAgent: data snapshot failed — {e}")
            return "Data snapshot unavailable due to a database error."
        finally:
            db.close()

    # ── Prompt builder ────────────────────────────────────────────────────────

    def _build_prompt(
        self,
        question: str,
        snapshot: str,
        history: list[dict],
    ) -> str:
        now_str = format_datetime_ist(now_ist(), "%Y-%m-%d %H:%M %Z")
        system = NOVA_SYSTEM_PROMPT.format(now=now_str)

        # Format conversation history
        history_text = ""
        if history:
            turns = []
            for msg in history[-10:]:   # last 5 turns in prompt to save tokens
                role  = "User" if msg["role"] == "user" else "Nova"
                turns.append(f"{role}: {msg['content']}")
            history_text = "\n\n=== CONVERSATION HISTORY ===\n" + "\n\n".join(turns)

        prompt = (
            f"{system}\n\n"
            f"=== LIVE DATA SNAPSHOT ===\n{snapshot}"
            f"{history_text}\n\n"
            f"=== CURRENT QUESTION ===\n"
            f"User: {question}\n\n"
            f"Nova:"
        )
        return prompt
