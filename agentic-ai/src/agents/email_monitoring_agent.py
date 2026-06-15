"""
EmailMonitoringAgent
--------------------
Polls Gmail inbox for unread emails.
Extracts metadata, body text, and attachment content (PDF/image).
For PDF: downloads attachment bytes, extracts text via pdfplumber.
For image: downloads attachment bytes, extracts text via pytesseract OCR.
"""

import base64
import io
import logging
from datetime import datetime, timezone
from email.utils import parseaddr

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
                flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDENTIALS_FILE, GMAIL_SCOPES)
                creds = flow.run_local_server(port=0)
            with open(GOOGLE_TOKEN_FILE, "w") as f:
                f.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    def run(self, state: AgentState) -> AgentState:
        logger.info("EmailMonitoringAgent: polling inbox...")
        try:
            result = self.service.users().messages().list(
                userId="me",
                q="is:unread in:inbox",
                maxResults=10
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

    def _fetch_email(self, message_id: str) -> dict | None:
        try:
            detail = self.service.users().messages().get(
                userId="me",
                id=message_id,
                format="full"
            ).execute()

            headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
            sender_raw = headers.get("From", "")
            _, sender_email = parseaddr(sender_raw)
            sender_domain = sender_email.split("@")[-1] if "@" in sender_email else ""

            date_str = headers.get("Date", "")
            try:
                received_at = datetime.strptime(date_str[:25].strip(), "%a, %d %b %Y %H:%M:%S")
            except Exception:
                received_at = datetime.now(timezone.utc)

            # Extract body text and detect attachment type
            body, attachment_type, attachment_id = self._extract_body_and_attachment(detail["payload"])

            # If email has PDF or image attachment, download and extract text
            if attachment_type in ("pdf", "image") and attachment_id:
                extracted = self._download_and_extract(message_id, attachment_id, attachment_type)
                if extracted:
                    # Combine body text with extracted attachment text
                    body = (body + "\n\n[Attachment content]:\n" + extracted).strip()
                    logger.info(f"EmailMonitoringAgent: extracted {attachment_type} attachment ({len(extracted)} chars)")

            return {
                "message_id": message_id,
                "thread_id": detail.get("threadId", ""),
                "sender": sender_email,
                "sender_domain": sender_domain,
                "subject": headers.get("Subject", "(no subject)"),
                "received_at": received_at,
                "raw_content": body,
                "attachment_type": attachment_type,
            }
        except Exception as e:
            logger.error(f"Error fetching email {message_id}: {e}")
            return None

    def _extract_body_and_attachment(self, payload: dict) -> tuple[str, str, str | None]:
        """
        Extract text body and detect any PDF/image attachment.
        Returns (body_text, attachment_type, attachment_id_or_None)
        """
        body = ""
        attachment_type = "text"
        attachment_id = None

        def decode_data(data):
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")

        # Direct body (simple email)
        if payload.get("body", {}).get("data"):
            body = decode_data(payload["body"]["data"])

        # Multipart email — walk all parts
        parts = payload.get("parts", [])
        for part in parts:
            mime = part.get("mimeType", "")
            part_body = part.get("body", {})

            if mime == "text/plain" and part_body.get("data") and not body:
                body = decode_data(part_body["data"])

            elif mime == "text/html" and part_body.get("data") and not body:
                body = decode_data(part_body["data"])
                attachment_type = "html"

            elif "pdf" in mime and part_body.get("attachmentId"):
                attachment_type = "pdf"
                attachment_id = part_body["attachmentId"]

            elif mime.startswith("image/") and part_body.get("attachmentId"):
                attachment_type = "image"
                attachment_id = part_body["attachmentId"]

            # Handle nested multipart (e.g. multipart/mixed containing multipart/alternative)
            elif mime.startswith("multipart/") and part.get("parts"):
                for subpart in part["parts"]:
                    submime = subpart.get("mimeType", "")
                    subbody = subpart.get("body", {})
                    if submime == "text/plain" and subbody.get("data") and not body:
                        body = decode_data(subbody["data"])
                    elif submime == "text/html" and subbody.get("data") and not body:
                        body = decode_data(subbody["data"])
                        if attachment_type == "text":
                            attachment_type = "html"
                    elif "pdf" in submime and subbody.get("attachmentId"):
                        attachment_type = "pdf"
                        attachment_id = subbody["attachmentId"]
                    elif submime.startswith("image/") and subbody.get("attachmentId"):
                        attachment_type = "image"
                        attachment_id = subbody["attachmentId"]

        return body.strip(), attachment_type, attachment_id

    def _download_and_extract(self, message_id: str, attachment_id: str, attachment_type: str) -> str:
        """Download attachment from Gmail and extract text content."""
        try:
            attachment = self.service.users().messages().attachments().get(
                userId="me",
                messageId=message_id,
                id=attachment_id
            ).execute()

            data = base64.urlsafe_b64decode(attachment["data"] + "==")

            if attachment_type == "pdf":
                return self._extract_pdf(data)
            elif attachment_type == "image":
                return self._extract_image(data)

        except Exception as e:
            logger.error(f"EmailMonitoringAgent: attachment download failed — {e}")
            return ""

    def _extract_pdf(self, data: bytes) -> str:
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                pages = [page.extract_text() or "" for page in pdf.pages]
            text = "\n".join(pages).strip()
            logger.info(f"EmailMonitoringAgent: PDF extracted, {len(text)} chars")
            return text
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
            text = pytesseract.image_to_string(img).strip()
            logger.info(f"EmailMonitoringAgent: image OCR extracted, {len(text)} chars")
            return text
        except ImportError:
            logger.warning("pytesseract/Pillow not installed — image OCR skipped")
            return ""
        except Exception as e:
            logger.error(f"Image OCR failed: {e}")
            return ""

    def mark_as_read(self, message_id: str):
        try:
            self.service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"removeLabelIds": ["UNREAD"]}
            ).execute()
        except Exception as e:
            logger.error(f"Failed to mark email as read: {e}")

    def get_service(self):
        return self.service
