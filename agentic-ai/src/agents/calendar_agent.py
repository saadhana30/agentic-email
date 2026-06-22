"""
CalendarAgent
-------------
LLM-driven agent that:
1. Uses LLM to extract meeting preferences from email (date, time, title)
2. Finds an available slot on Google Calendar
3. Creates the event in the correct local timezone (IST by default)
"""

import json
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from src.tools.ollama_client import call_ollama
from src.config import GOOGLE_TOKEN_FILE, GMAIL_SCOPES, MAX_RETRIES, RETRY_DELAY_SECONDS, CALENDAR_TIMEZONE
from src.utils.time_utils import format_datetime_ist
from src.graph.state import AgentState

logger = logging.getLogger(__name__)

WORKING_HOURS_START = 9
WORKING_HOURS_END = 17
MEETING_DURATION_MINS = 60

CALENDAR_AGENT_PROMPT = """You are a calendar scheduling assistant. Today's date is {today}.

Given this email, extract the meeting scheduling preferences.

Email Subject: {subject}
Email Body: {body}
Execution Plan: {execution_plan}

Return ONLY a JSON object with:
{{
  "preferred_date": "YYYY-MM-DD using year {year} or null if not mentioned",
  "preferred_time": "HH:MM in 24h format or null if not mentioned",
  "meeting_title": "short title for the meeting",
  "duration_minutes": 60,
  "notes": "any special notes"
}}

Important:
- Always use year {year} for dates
- If email says "next Monday", calculate the actual date from today ({today})
- Return ONLY valid JSON, nothing else
"""


class CalendarAgent:

    def __init__(self):
        self._service = None
        self.tz = ZoneInfo(CALENDAR_TIMEZONE)

    def _get_service(self):
        if self._service is None:
            creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, GMAIL_SCOPES)
            self._service = build("calendar", "v3", credentials=creds)
        return self._service

    def run(self, state: AgentState) -> AgentState:
        if "calendar_agent" not in state.get("next_agents", []):
            return state

        email        = state.get("current_email", {})
        analysis     = state.get("analysis", {})
        client_name  = state.get("client_name") or email.get("sender", "Client")
        thread_context = state.get("thread_context", {})

        logger.info(f"CalendarAgent INVOKED for email from {email.get('sender')}")

        # ── Thread-awareness: update existing event instead of creating new ──
        existing_event_id = thread_context.get("existing_event_id")
        category          = analysis.get("category", "")

        if existing_event_id and category == "meeting_reschedule":
            logger.info(
                f"CalendarAgent: reschedule request — updating event {existing_event_id}"
            )
            preferences = self._reason_about_scheduling(email, analysis)
            new_slot    = self._find_slot(preferences)
            result      = self._update_event(existing_event_id, new_slot, client_name,
                                              email, preferences)
            actions = state.get("actions_taken", [])
            actions.append({"agent": "calendar_agent", "result": result})
            state["actions_taken"] = actions
            if result.get("rescheduled"):
                state["calendar_rescheduled"] = True
                state["proposed_slot"] = new_slot.isoformat() if new_slot else None
            return state

        # ── Normal flow: create new event ────────────────────────────────────
        preferences = self._reason_about_scheduling(email, analysis)
        logger.info(f"CalendarAgent LLM decision: {preferences}")

        slot   = self._find_slot(preferences)
        result = self._create_event(slot, client_name, email, preferences)

        actions = state.get("actions_taken", [])
        actions.append({"agent": "calendar_agent", "result": result})
        state["actions_taken"] = actions

        if result.get("rescheduled"):
            state["calendar_rescheduled"] = True
            state["proposed_slot"] = slot.isoformat() if slot else None

        return state

    def _reason_about_scheduling(self, email: dict, analysis: dict) -> dict:
        now_local = datetime.now(self.tz)
        prompt = CALENDAR_AGENT_PROMPT.format(
            today=now_local.strftime("%Y-%m-%d"),
            year=now_local.year,
            subject=email.get("subject", ""),
            body=email.get("processed_content", "")[:1000],
            execution_plan=", ".join(analysis.get("execution_plan", [])),
        )
        try:
            raw = call_ollama(prompt, temperature=0.1)
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            return json.loads(raw)
        except Exception as e:
            logger.warning(f"CalendarAgent: LLM reasoning failed, using defaults — {e}")
            return {
                "preferred_date": None,
                "preferred_time": None,
                "meeting_title": email.get("subject", "Meeting"),
                "duration_minutes": 60,
                "notes": "",
            }

    def _find_slot(self, preferences: dict) -> datetime:
        """Find available slot in local timezone."""
        now_local = datetime.now(self.tz)

        # Try preferred date/time from LLM
        if preferences.get("preferred_date") and preferences.get("preferred_time"):
            try:
                preferred_dt = datetime.strptime(
                    f"{preferences['preferred_date']} {preferences['preferred_time']}",
                    "%Y-%m-%d %H:%M"
                ).replace(tzinfo=self.tz)

                if preferred_dt > now_local:
                    busy = self._get_busy_times(preferred_dt, preferred_dt + timedelta(minutes=60))
                    if not busy:
                        return preferred_dt
                    logger.info("CalendarAgent: preferred slot busy, finding next available")
                else:
                    logger.info(f"CalendarAgent: preferred date is in the past, finding next slot")
            except Exception as e:
                logger.warning(f"CalendarAgent: could not parse preferred time — {e}")

        # Find next free working-hours slot (skip today, start from tomorrow)
        for days_ahead in range(1, 8):
            check_date = now_local + timedelta(days=days_ahead)
            for hour in range(WORKING_HOURS_START, WORKING_HOURS_END):
                slot_start = check_date.replace(hour=hour, minute=0, second=0, microsecond=0)
                busy = self._get_busy_times(slot_start, slot_start + timedelta(minutes=MEETING_DURATION_MINS))
                if not busy:
                    return slot_start

        return (now_local + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)

    def _get_busy_times(self, start: datetime, end: datetime) -> list:
        try:
            result = self._get_service().freebusy().query(body={
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "items": [{"id": "primary"}],
            }).execute()
            return result.get("calendars", {}).get("primary", {}).get("busy", [])
        except Exception as e:
            logger.error(f"CalendarAgent: freebusy check failed — {e}")
            return []

    def _create_event(self, slot: datetime, client_name: str, email: dict, preferences: dict) -> dict:
        duration = preferences.get("duration_minutes", MEETING_DURATION_MINS)
        preferred_was_unavailable = bool(preferences.get("preferred_date"))

        event = {
            "summary": preferences.get("meeting_title", f"Meeting with {client_name}"),
                "description": (
                    f"Scheduled by NovaSphere Agent\n"
                    f"Client: {client_name}\n"
                    f"Original request: {email.get('subject', '')}\n"
                    f"{preferences.get('notes', '')}"
                ),
            # Use local timezone string — calendar shows correct time
            "start": {"dateTime": slot.isoformat(), "timeZone": CALENDAR_TIMEZONE},
            "end":   {"dateTime": (slot + timedelta(minutes=duration)).isoformat(), "timeZone": CALENDAR_TIMEZONE},
            "attendees": [{"email": email.get("sender", "")}],
            "reminders": {"useDefault": True},
        }

        for attempt in range(MAX_RETRIES + 1):
            try:
                created = self._get_service().events().insert(
                    calendarId="primary",
                    body=event,
                    sendUpdates="all"
                ).execute()
                logger.info(f"CalendarAgent: event created — {created.get('id')}")
                return {
                    "status": "success",
                    "event_id": created.get("id"),
                    "event_link": created.get("htmlLink"),
                    "slot": format_datetime_ist(slot, "%d %b %Y %I:%M %p %Z"),
                    "rescheduled": preferred_was_unavailable,
                }
            except Exception as e:
                logger.warning(f"CalendarAgent: attempt {attempt + 1} failed — {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SECONDS)

        return {"status": "failed", "error": "Calendar event creation failed after retries"}

    def _update_event(
        self,
        event_id: str,
        new_slot: datetime,
        client_name: str,
        email: dict,
        preferences: dict,
    ) -> dict:
        """
        Update an existing Google Calendar event to a new time slot.
        Used when a thread reply indicates the client wants to reschedule.
        """
        duration = preferences.get("duration_minutes", MEETING_DURATION_MINS)
        patch_body = {
            "start": {"dateTime": new_slot.isoformat(), "timeZone": CALENDAR_TIMEZONE},
            "end":   {
                "dateTime": (new_slot + timedelta(minutes=duration)).isoformat(),
                "timeZone": CALENDAR_TIMEZONE,
            },
            "description": (
                f"Rescheduled by NovaSphere Agent\n"
                f"Client: {client_name}\n"
                f"Reschedule request: {email.get('subject', '')}\n"
                f"{preferences.get('notes', '')}"
            ),
        }
        for attempt in range(MAX_RETRIES + 1):
            try:
                updated = self._get_service().events().patch(
                    calendarId="primary",
                    eventId=event_id,
                    body=patch_body,
                    sendUpdates="all",
                ).execute()
                logger.info(f"CalendarAgent: event {event_id} rescheduled to {new_slot}")
                return {
                    "status":     "success",
                    "event_id":   updated.get("id"),
                    "event_link": updated.get("htmlLink"),
                    "slot":       format_datetime_ist(new_slot, "%d %b %Y %I:%M %p %Z"),
                    "rescheduled": True,
                    "action":     "event_updated",
                }
            except Exception as e:
                logger.warning(f"CalendarAgent: update attempt {attempt + 1} failed — {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SECONDS)

        return {"status": "failed", "error": "Calendar event update failed after retries"}
