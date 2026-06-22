"""
test_sanitizer.py
-----------------
Verifies that log_sanitizer.sanitize() masks every sensitive pattern
and leaves normal log text untouched.
Run with:  python test_sanitizer.py
"""
import os, sys
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ".")

from src.log_sanitizer import sanitize, SanitizingFilter
import logging

RESET = "\033[0m"
GREEN = "\033[32m"
RED   = "\033[31m"
BOLD  = "\033[1m"

def check(label: str, raw: str, should_mask: bool = True):
    result = sanitize(raw)
    has_mask  = "*****" in result
    not_empty = len(result) >= 5
    passed = (has_mask == should_mask) and not_empty
    sym = f"{GREEN}PASS{RESET}" if passed else f"{RED}FAIL{RESET}"
    print(f"  {sym}  {label}")
    print(f"        IN : {raw[:90]}")
    print(f"        OUT: {result[:90]}")
    print()
    return passed

results = []

print(f"\n{BOLD}=== Log Sanitizer Tests ==={RESET}\n")

# ── Should be masked ──────────────────────────────────────────────────────────

results.append(check(
    "JWT Bearer token",
    "Auth: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6",
))

results.append(check(
    "Raw JWT (three-part)",
    "token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc123defghijklmnopqrst",
))

results.append(check(
    "access_token in JSON",
    '{"access_token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.longsecretvalue123"}',
))

results.append(check(
    "refresh_token value",
    "refresh_token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.anotherlong123456789",
))

results.append(check(
    "Jira ATATT token",
    "JIRA_API_TOKEN=ATATT3xFfGF0baaNcsKkQlJDkXH_q1YxfALUTzcNrJxLbsPge8UBSOHqUauxJ_0MqV2",
))

results.append(check(
    "Google ya29 OAuth token",
    "Google token: ya29.A0ARrdaM_longGoogleAccessToken123456789xyz",
))

results.append(check(
    "Bcrypt password hash",
    "hashed_password=$2b$12$abcdefghijklmnopqrstuvwxyz0123456789ABCDEF",
))

results.append(check(
    "Password in log line",
    "password=supersecretpassword123",
))

results.append(check(
    "Cookie header",
    "Cookie: session=abc123def456ghi789; token=xyz987654321",
))

results.append(check(
    "session_id value",
    "session_id=abcdef123456789xyz",
))

results.append(check(
    "Authorization header",
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIyIn0.tokenvalue123456",
))

results.append(check(
    "Google token in JSON dict",
    '{"token": "ya29.A0ARrdaM_longGoogleToken", "expiry": "2026-07-01"}',
))

# ── Should NOT be masked ──────────────────────────────────────────────────────

results.append(check(
    "Normal log line (unchanged)",
    "EmailAnalysisAgent: confidence=0.87, agents=[jira_agent, reply_agent]",
    should_mask=False,
))

results.append(check(
    "Jira ticket key (unchanged)",
    "JiraAgent: issue created — PROJ-42, assigned to Alice",
    should_mask=False,
))

results.append(check(
    "Email sender (unchanged)",
    "Poller: invoking graph for email from client@example.com",
    should_mask=False,
))

results.append(check(
    "Confidence threshold (unchanged)",
    "SupervisorAgent: confidence=0.91 — auto executing",
    should_mask=False,
))

# ── Test the logging.Filter integration ───────────────────────────────────────

print(f"{BOLD}=== Filter Integration Test ==={RESET}\n")

from src.log_sanitizer import install_sanitizer
install_sanitizer()

import io
stream = io.StringIO()
h = logging.StreamHandler(stream)
h.setLevel(logging.DEBUG)
test_logger = logging.getLogger("sanitizer.test")
test_logger.addHandler(h)
test_logger.setLevel(logging.DEBUG)

test_logger.info(
    "Auth: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.longtokensecretvalue"
)
output = stream.getvalue()
passed = "*****" in output and "longtokensecretvalue" not in output
sym = f"{GREEN}PASS{RESET}" if passed else f"{RED}FAIL{RESET}"
print(f"  {sym}  Filter masks token in logger.info() output")
print(f"        OUT: {output.strip()[:100]}")
results.append(passed)
print()

# ── Summary ───────────────────────────────────────────────────────────────────
total  = len(results)
passed = sum(results)
print(f"{BOLD}{'='*40}{RESET}")
print(f"Results: {passed}/{total} passed")
if passed == total:
    print(f"{GREEN}{BOLD}ALL TESTS PASSED{RESET}")
else:
    print(f"{RED}{BOLD}{total - passed} TEST(S) FAILED{RESET}")
    sys.exit(1)
