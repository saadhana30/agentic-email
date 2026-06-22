"""
log_sanitizer.py
----------------
Centralized log sanitization filter.

Attaches to the root logger as a logging.Filter so every log record —
regardless of which logger or handler emits it — is scrubbed before
it reaches stdout, app.log, or errors.log.

Sensitive patterns masked:
  - JWT access / refresh tokens (Bearer <token>, "access_token": "...", etc.)
  - Authorization headers
  - JIRA_API_TOKEN values (long Atlassian tokens)
  - Google OAuth token values (ya29.*, token.json content)
  - Password hashes ($2b$ bcrypt hashes)
  - Passwords in key=value or JSON form
  - Cookie / session-id header values
  - Any remaining high-entropy token-like strings

Masking format:  first 3 chars + ***** + last 3 chars
  e.g.  eyJhbGciOiJIUzI1NiJ9...xyz  →  eyJ*****xyz
        ATATT3xFf...D732             →  ATA*****732
        $2b$12$abc...xyz             →  $2b*****xyz

Business logic and agent behaviour are NOT touched.
Existing logger.info() / warning() / error() calls are unchanged.
"""

import re
import logging

# ── Masking helper ─────────────────────────────────────────────────────────────

def _mask(value: str, keep: int = 3) -> str:
    """
    Keep `keep` characters at each end, replace the middle with *****.
    Short values (≤ keep*2) are fully masked as *****.
    """
    if not value:
        return value
    v = value.strip()
    if len(v) <= keep * 2:
        return "*****"
    return f"{v[:keep]}*****{v[-keep:]}"


# ── Regex patterns ─────────────────────────────────────────────────────────────
# Each entry is (compiled_regex, replacement_callable_or_string).
# The replacement receives the match object and returns the sanitized string.

def _mask_group(n: int):
    """Return a replacement function that masks capture group n."""
    def _replace(m: re.Match) -> str:
        prefix = m.string[m.start():m.start(n)]   # text before group n within match
        # Reconstruct: everything up to group n + masked group n + everything after
        full    = m.group(0)
        val     = m.group(n)
        masked  = _mask(val)
        return full.replace(val, masked, 1)
    return _replace


# Patterns are applied in order; earlier patterns take precedence.
# Each tuple: (pattern, group_index_to_mask)
# group 1 = the sensitive value to replace.
_RAW_PATTERNS: list[tuple[str, int]] = [

    # Bearer tokens in Authorization header:  Bearer eyJ...
    (r'(?i)(Bearer\s+)([A-Za-z0-9\-_\.]{20,})', 2),

    # Authorization header full value in log strings
    (r'(?i)(Authorization["\s:=]+["\']?)([A-Za-z0-9\-_\.\s]{20,})(["\']?)', 2),

    # JSON / dict: "access_token": "eyJ..."
    (r'(?i)(["\']?access_token["\']?\s*[:=]\s*["\']?)([A-Za-z0-9\-_\.]{20,})(["\']?)', 2),

    # JSON / dict: "refresh_token": "eyJ..."
    (r'(?i)(["\']?refresh_token["\']?\s*[:=]\s*["\']?)([A-Za-z0-9\-_\.]{20,})(["\']?)', 2),

    # Atlassian JIRA API tokens (start with ATATT)
    (r'(ATATT[A-Za-z0-9\-_\.]{10,})', 1),

    # Google OAuth ya29.* access tokens
    (r'(ya29\.[A-Za-z0-9\-_\.]{10,})', 1),

    # Google token JSON values: "token": "...", "id_token": "...", "access_token": "..."
    (r'(?i)(["\'](?:token|id_token|refresh_token|access_token)["\']?\s*:\s*["\'])([A-Za-z0-9\-_\.]{20,})(["\'])', 2),

    # Bcrypt password hashes:  $2b$12$...  /  $2a$...
    (r'(\$2[abxy]\$\d{2}\$[A-Za-z0-9\.\/]{20,})', 1),

    # Passwords in log strings: password="...", password: "...", hashed_password=...
    (r'(?i)(["\']?(?:password|hashed_password|passwd)["\']?\s*[:=]\s*["\']?)([^\s"\']{6,})(["\']?)', 2),

    # Cookie header values:  Cookie: session=abc...; token=xyz...
    (r'(?i)(Cookie["\s:=]+)([^\n]{10,})', 2),

    # Set-Cookie values
    (r'(?i)(Set-Cookie["\s:=]+)([^\n]{10,})', 2),

    # session_id / sessionid values
    (r'(?i)(["\']?session[_\-]?id["\']?\s*[:=]\s*["\']?)([A-Za-z0-9\-_\.]{8,})(["\']?)', 2),

    # Generic high-entropy JWT-shaped strings: three base64url segments separated by dots
    # Matches full JWTs even if not preceded by "Bearer"
    (r'(eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+)', 1),
]

# Compile all patterns once at import time
_PATTERNS: list[tuple[re.Pattern, int]] = [
    (re.compile(pat, re.MULTILINE), grp)
    for pat, grp in _RAW_PATTERNS
]


def sanitize(text: str) -> str:
    """
    Apply all masking patterns to `text` and return the sanitized version.
    Pure function — does not raise, always returns a string.
    """
    if not text:
        return text
    try:
        for pattern, group_idx in _PATTERNS:
            text = _apply_pattern(pattern, group_idx, text)
    except Exception:
        # Never let sanitization break logging
        pass
    return text


def _apply_pattern(pattern: re.Pattern, group_idx: int, text: str) -> str:
    """Replace the target capture group in every match with a masked value."""
    def replacer(m: re.Match) -> str:
        try:
            val    = m.group(group_idx)
            masked = _mask(val)
            # Reconstruct the full match with only group_idx replaced
            full   = m.group(0)
            return full.replace(val, masked, 1)
        except Exception:
            return m.group(0)   # leave unchanged on any error

    return pattern.sub(replacer, text)


# ── logging.Filter subclass ───────────────────────────────────────────────────

class SanitizingFilter(logging.Filter):
    """
    A logging.Filter that sanitizes log record messages in-place
    before they reach any handler.

    Attach to the root logger (or any specific logger) via:
        logger.addFilter(SanitizingFilter())
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Sanitize the pre-formatted message
        try:
            if isinstance(record.msg, str):
                record.msg = sanitize(record.msg)

            # Also sanitize args — these are interpolated into msg by the formatter
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        k: sanitize(v) if isinstance(v, str) else v
                        for k, v in record.args.items()
                    }
                elif isinstance(record.args, tuple):
                    record.args = tuple(
                        sanitize(a) if isinstance(a, str) else a
                        for a in record.args
                    )

            # Sanitize exception text if present
            if record.exc_text:
                record.exc_text = sanitize(record.exc_text)

        except Exception:
            pass   # never block a log record due to sanitization failure

        return True   # always allow the record through


# ── Convenience install function ──────────────────────────────────────────────

def install_sanitizer() -> None:
    """
    Attach SanitizingFilter to the root logger.
    Call this once, early in setup_logging(), before handlers are added.
    The filter runs for every logger in the process automatically.
    """
    root = logging.getLogger()
    # Avoid double-installing
    for f in root.filters:
        if isinstance(f, SanitizingFilter):
            return
    root.addFilter(SanitizingFilter())
    # Also install a LogRecord factory that sanitizes messages and args at
    # creation time. This ensures sanitization even for handlers attached to
    # child loggers or handlers created after this call (tests create handlers
    # dynamically and expect sanitization to apply).
    try:
        # Use a module-level attribute to avoid double-wrapping the factory
        if getattr(install_sanitizer, "_factory_installed", False):
            return
        orig_factory = logging.getLogRecordFactory()

        def _sanitizing_factory(*args, **kwargs):
            record = orig_factory(*args, **kwargs)
            try:
                if isinstance(record.msg, str):
                    record.msg = sanitize(record.msg)

                if record.args:
                    if isinstance(record.args, dict):
                        record.args = {
                            k: sanitize(v) if isinstance(v, str) else v
                            for k, v in record.args.items()
                        }
                    elif isinstance(record.args, tuple):
                        record.args = tuple(
                            sanitize(a) if isinstance(a, str) else a
                            for a in record.args
                        )

                if getattr(record, "exc_text", None):
                    record.exc_text = sanitize(record.exc_text)
            except Exception:
                # Never let sanitization break logging
                pass
            return record

        logging.setLogRecordFactory(_sanitizing_factory)
        install_sanitizer._factory_installed = True
    except Exception:
        # Don't fail if the record factory couldn't be set
        pass
