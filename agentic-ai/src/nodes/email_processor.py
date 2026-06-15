"""
EmailProcessorNode
------------------
Processes raw email content regardless of format:
- Plain text → use as-is
- HTML → strip tags
- PDF attachment → extract with pdfplumber
- Image attachment → OCR with pytesseract
Sets state["processed_content"] for LLM analysis.
"""

import io
import re
import logging
import base64

from src.graph.state import AgentState

logger = logging.getLogger(__name__)


def strip_html(html: str) -> str:
    """Remove HTML tags and decode common entities."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", text).strip()


def extract_from_pdf(data: bytes) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        return text.strip()
    except ImportError:
        logger.warning("pdfplumber not installed, skipping PDF extraction")
        return ""
    except Exception as e:
        logger.error(f"PDF extraction failed: {e}")
        return ""


def extract_from_image(data: bytes) -> str:
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        return pytesseract.image_to_string(img).strip()
    except ImportError:
        logger.warning("pytesseract or Pillow not installed, skipping OCR")
        return ""
    except Exception as e:
        logger.error(f"OCR extraction failed: {e}")
        return ""


def email_processor_node(state: AgentState) -> AgentState:
    """
    Process raw email content into clean text.
    Called as a LangGraph node.
    """
    email = state.get("current_email", {})
    if not email:
        return state

    attachment_type = email.get("attachment_type", "text")
    raw_content = email.get("raw_content", "")

    if attachment_type == "html":
        processed = strip_html(raw_content)

    elif attachment_type == "pdf":
        # raw_content is base64-encoded bytes for attachments
        try:
            data = base64.urlsafe_b64decode(raw_content + "==")
            processed = extract_from_pdf(data)
        except Exception:
            processed = raw_content  # fallback

    elif attachment_type == "image":
        try:
            data = base64.urlsafe_b64decode(raw_content + "==")
            processed = extract_from_image(data)
        except Exception:
            processed = ""

    else:
        processed = raw_content

    # Attach processed content back to current_email in state
    email["processed_content"] = processed or raw_content
    state["current_email"] = email

    logger.info(f"EmailProcessorNode: processed {attachment_type} email, length={len(email['processed_content'])}")
    return state
