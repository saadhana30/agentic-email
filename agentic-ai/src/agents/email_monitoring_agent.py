"""
EmailMonitoringAgent
--------------------
Polls Gmail inbox for unread emails.

Feature additions:
  1. Thread Awareness — extracts thread_id, in_reply_to, references headers
     and marks emails that are replies to existing threads so the poller
     can skip duplicate-thread processing.
  2. Multi-Attachment Support — discovers ALL PDF and image attachments in
     a single email, downloads and extracts text from each one independently.
     Returns a list of attachment dicts alongside the email body.
  3. Tesseract Availability Detection — checks for the tesseract binary
     before attempting OCR; gracefully skips if unavailable.
"""

import base64
import io
import logging
import shutil
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

from src.config import (
    GOOGLE_CREDENTIALS_FILE,
    GOOGLE_TOKEN_FILE,
    GMAIL_SCOPES,
)
from src.graph.state import AgentState

logger = logging.getLogger(__name__)


# ── Tesseract availability check (Feature 6) ──────────────────────────────────
def _tesseract_available() -> bool:
    """Return True if the tesseract binary is on PATH."""
    available = shutil.which("tesseract") is not None
    if not available:
        logger.warning(
            "EmailMonitoringAgent: tesseract binary not found — "
            "image OCR will be skipped. Install tesseract to enable OCR."
        )
    return available


_TESSERACT_OK: bool | None = None   # cached after first check


def _check_tesseract() -> bool:
    global _TESSERACT_OK
    if _TESSERACT_OK is None:
        _TESSERACT_OK = _tesseract_available()
    return _TESSERACT_OK


# ─────────────────────────────────────────────────────────────────────────────

class EmailMonitoringAgent:

    def __init__(self):
        self.service = self._authenticate()

    def _authenticate(self):
        creds = None
        try:
            creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, GMAIL_SCOPES)
        except Exception:
            pass

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    GOOGLE_CREDENTIALS_FILE, GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(GOOGLE_TOKEN_FILE, "w") as f:
                f.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    # ── Public: called by poller ──────────────────────────────────────────────

    def run(self, state: AgentState) -> AgentState:
        logger.info("EmailMonitoringAgent: polling inbox...")
        try:
            result = self.service.users().messages().list(
                userId="me",
                q="is:unread in:inbox",
                maxResults=10,
            ).execute()

            messages = result.get("messages", [])
            emails = []

            for msg in messages:
                email_data = self._fetch_email(msg["id"])
                if email_data:
                    emails.append(email_data)

            state["raw_emails"] = emails
            state["error"] = None
            logger.info(f"EmailMonitoringAgent: fetched {len(emails)} unread emails")

        except Exception as e:
            logger.error(f"EmailMonitoringAgent error: {e}")
            state["raw_emails"] = []
            state["error"] = str(e)

        return state

    # ── Fetch single email ────────────────────────────────────────────────────

    def _fetch_email(self, message_id: str) -> dict | None:
        try:
            detail = self.service.users().messages().get(
                userId="me",
                id=message_id,
                format="full",
            ).execute()

            headers_raw = {
                h["name"]: h["value"]
                for h in detail["payload"]["headers"]
            }

            # ── Sender / domain ──
            sender_raw = headers_raw.get("From", "")
            _, sender_email = parseaddr(sender_raw)
            sender_domain = sender_email.split("@")[-1] if "@" in sender_email else ""

            # ── Received timestamp ──
            date_str = headers_raw.get("Date", "")
            try:
                # parsedate_to_datetime returns a tz-aware datetime when possible
                parsed = parsedate_to_datetime(date_str)
                if parsed is None:
                    raise ValueError("Could not parse Date header")
                if parsed.tzinfo is None:
                    received_at = parsed.replace(tzinfo=timezone.utc)
                else:
                    received_at = parsed.astimezone(timezone.utc)
            except Exception:
                received_at = datetime.now(timezone.utc)

            # ── Thread / reply metadata (Feature 1) ──
            thread_id   = detail.get("threadId", "")
            in_reply_to = headers_raw.get("In-Reply-To", "").strip() or None
            references  = headers_raw.get("References",  "").strip() or None

            # ── Body + ALL attachments (Feature 3) ──
            body, attachments = self._extract_body_and_all_attachments(
                detail["payload"], message_id
            )

            # Determine top-level attachment_type for backwards-compat column
            attachment_types = {a["attachment_type"] for a in attachments}
            if len(attachment_types) > 1:
                top_type = "mixed"
            elif attachment_types:
                top_type = attachment_types.pop()
            else:
                top_type = "text"

            # Combine body + all extracted attachment texts
            combined_body = body
            for att in attachments:
                if att["extracted_text"]:
                    combined_body = (
                        combined_body
                        + f"\n\n[Attachment: {att['filename']}]:\n"
                        + att["extracted_text"]
                    ).strip()

            return {
                "message_id":      message_id,
                "thread_id":       thread_id,
                "in_reply_to":     in_reply_to,
                "references":      references,
                "sender":          sender_email,
                "sender_domain":   sender_domain,
                "subject":         headers_raw.get("Subject", "(no subject)"),
                "received_at":     received_at,
                "raw_content":     combined_body,
                "attachment_type": top_type,
                "attachments":     attachments,   # list of dicts for DB storage
            }

        except Exception as e:
            logger.error(f"Error fetching email {message_id}: {e}")
            return None

    # ── Extract body + ALL attachments (Feature 3) ───────────────────────────

    def _extract_body_and_all_attachments(
        self, payload: dict, message_id: str
    ) -> tuple[str, list[dict]]:
        """
        Walk the full MIME tree.
        Returns (body_text, list_of_attachment_dicts).
        Each attachment dict: filename, mime_type, attachment_type, extracted_text.
        """
        body = ""
        attachments: list[dict] = []

        def decode_data(data: str) -> str:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")

        def walk(parts: list):
            nonlocal body
            for part in parts:
                mime  = part.get("mimeType", "")
                pbody = part.get("body", {})
                fname = part.get("filename", "") or ""

                if mime == "text/plain" and pbody.get("data") and not body:
                    body = decode_data(pbody["data"])

                elif mime == "text/html" and pbody.get("data") and not body:
                    # HTML body — will be stripped by email_processor_node
                    body = decode_data(pbody["data"])

                elif "pdf" in mime and pbody.get("attachmentId"):
                    extracted = self._download_and_extract(
                        message_id, pbody["attachmentId"], "pdf"
                    )
                    attachments.append({
                        "filename":        fname or "attachment.pdf",
                        "mime_type":       mime,
                        "attachment_type": "pdf",
                        "extracted_text":  extracted,
                    })
                    logger.info(
                        f"EmailMonitoringAgent: PDF '{fname}' extracted "
                        f"({len(extracted)} chars)"
                    )

                elif mime.startswith("image/") and pbody.get("attachmentId"):
                    if _check_tesseract():
                        extracted = self._download_and_extract(
                            message_id, pbody["attachmentId"], "image"
                        )
                        attachments.append({
                            "filename":        fname or "attachment.img",
                            "mime_type":       mime,
                            "attachment_type": "image",
                            "extracted_text":  extracted,
                        })
                        logger.info(
                            f"EmailMonitoringAgent: image OCR '{fname}' "
                            f"({len(extracted)} chars)"
                        )
                    else:
                        logger.warning(
                            f"EmailMonitoringAgent: skipping image OCR for '{fname}' "
                            "— tesseract not available"
                        )

                elif mime.startswith("multipart/") and part.get("parts"):
                    walk(part["parts"])

        # Handle simple (non-multipart) email
        if payload.get("body", {}).get("data"):
            body = decode_data(payload["body"]["data"])

        walk(payload.get("parts", []))

        return body.strip(), attachments

    # ── Download + extract attachment content ─────────────────────────────────

    def _download_and_extract(
        self, message_id: str, attachment_id: str, attachment_type: str
    ) -> str:
        try:
            attachment = self.service.users().messages().attachments().get(
                userId="me",
                messageId=message_id,
                id=attachment_id,
            ).execute()
            data = base64.urlsafe_b64decode(attachment["data"] + "==")

            if attachment_type == "pdf":
                return self._extract_pdf(data)
            elif attachment_type == "image":
                return self._extract_image(data)
            return ""
        except Exception as e:
            logger.error(
                f"EmailMonitoringAgent: attachment download failed "
                f"({attachment_type}) — {e}"
            )
            return ""

    def _extract_pdf(self, data: bytes) -> str:
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                pages = [page.extract_text() or "" for page in pdf.pages]
            return "\n".join(pages).strip()
        except ImportError:
            logger.warning("pdfplumber not installed — PDF extraction skipped")
            return ""
        except Exception as e:
            logger.error(f"PDF extraction failed: {e}")
            return ""

    def _extract_image(self, data: bytes) -> str:
        try:
            import pytesseract
            from PIL import Image
            img = Image.open(io.BytesIO(data))
            return pytesseract.image_to_string(img).strip()
        except ImportError:
            logger.warning("pytesseract/Pillow not installed — image OCR skipped")
            return ""
        except Exception as e:
            logger.error(f"Image OCR failed: {e}")
            return ""

    # ── Utilities ─────────────────────────────────────────────────────────────

    def mark_as_read(self, message_id: str):
        try:
            self.service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"removeLabelIds": ["UNREAD"]},
            ).execute()
        except Exception as e:
            logger.error(f"Failed to mark email as read: {e}")

    def get_service(self):
        return self.service

    def fetch_thread_messages(self, thread_id: str) -> list[dict]:
        """
        Fetch all messages in a Gmail thread.
        Used by EmailAnalysisAgent to reconstruct conversation history.
        Returns list of {sender, subject, body, received_at} dicts, oldest first.
        """
        try:
            result = self.service.users().threads().get(
                userId="me",
                id=thread_id,
                format="full",
            ).execute()
            messages = result.get("messages", [])
            thread_msgs = []
            for msg in messages:
                headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
                _, sender = parseaddr(headers.get("From", ""))
                subject   = headers.get("Subject", "")

                # Extract plain-text body only for context
                body = self._extract_plain_body(msg["payload"])

                date_str = headers.get("Date", "")
                try:
                    parsed = parsedate_to_datetime(date_str)
                    if parsed is None:
                        ts = None
                    else:
                        if parsed.tzinfo is None:
                            ts = parsed.replace(tzinfo=timezone.utc)
                        else:
                            ts = parsed.astimezone(timezone.utc)
                except Exception:
                    ts = None

                thread_msgs.append({
                    "message_id": msg.get("id"),
                    "sender":     sender,
                    "subject":    subject,
                    "body":       body[:500],   # trim for prompt injection
                    "received_at": ts,
                })
            # Oldest first — use timezone-aware minimum if missing
            tz_min = datetime(1970, 1, 1, tzinfo=timezone.utc)
            thread_msgs.sort(key=lambda m: m["received_at"] or tz_min)
            return thread_msgs
        except Exception as e:
            logger.error(f"EmailMonitoringAgent: thread fetch failed for {thread_id} — {e}")
            return []

    def _extract_plain_body(self, payload: dict) -> str:
        """Best-effort plain text extraction for thread history context."""
        def decode(data):
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")

        if payload.get("body", {}).get("data"):
            return decode(payload["body"]["data"])

        for part in payload.get("parts", []):
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                return decode(part["body"]["data"])
            if part.get("mimeType", "").startswith("multipart/"):
                result = self._extract_plain_body(part)
                if result:
                    return result
        return ""
