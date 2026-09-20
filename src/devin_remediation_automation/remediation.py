import re
from dataclasses import dataclass

REMEDIATION_LABEL = "devin-remediation"
REMEDIATION_MARKER = "Remediation-ID"
MARKER_PATTERN = re.compile(rf"^\s*{REMEDIATION_MARKER}:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)


def extract_remediation_id(pull_request_body: str | None) -> str | None:
    """Read the `Remediation-ID: <delivery_id>` marker Devin is asked to put in the PR body."""
    if not pull_request_body:
        return None
    match = MARKER_PATTERN.search(pull_request_body)
    return match.group(1) if match else None


@dataclass(frozen=True)
class RemediationRequest:
    repository_full_name: str
    issue_number: int
    issue_title: str
    issue_url: str
    issue_body: str


def build_session_title(request: RemediationRequest) -> str:
    return f"Remediate {request.repository_full_name}#{request.issue_number}: {request.issue_title}"


def build_session_prompt(request: RemediationRequest, delivery_id: str) -> str:
    body = request.issue_body.strip() or "(no issue body provided)"
    return "\n".join(
        [
            (
                f"Remediate GitHub issue {request.repository_full_name}"
                f"#{request.issue_number}, which was labeled `{REMEDIATION_LABEL}`."
            ),
            "",
            f"Repository: {request.repository_full_name}",
            f"Issue: #{request.issue_number} — {request.issue_title}",
            f"Issue URL: {request.issue_url}",
            "",
            "Issue body / acceptance criteria:",
            body,
            "",
            "Please:",
            "1. Investigate the issue in the repository and determine the root cause.",
            "2. Make the smallest appropriate fix that resolves the issue.",
            "3. Run the relevant validation for the code you touched (tests, lint, type checks).",
            (
                f"4. Open a pull request against {request.repository_full_name} describing "
                f"the fix, and link it back to issue #{request.issue_number}. Do not merge it."
            ),
            (
                "5. Include this exact line on its own line in the pull request description so "
                "the automation can correlate the pull request with this remediation:"
            ),
            f"   {REMEDIATION_MARKER}: {delivery_id}",
            "",
            (
                "Decide the implementation yourself based on what you find in the codebase. "
                "If the issue is ambiguous or the fix looks risky, explain the tradeoffs in "
                "the pull request description instead of guessing."
            ),
        ]
    )
