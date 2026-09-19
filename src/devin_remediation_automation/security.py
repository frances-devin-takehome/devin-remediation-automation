import hashlib
import hmac

SIGNATURE_PREFIX = "sha256="


def verify_signature(secret: str, payload: bytes, signature_header: str | None) -> bool:
    """Verify GitHub's X-Hub-Signature-256 header against the raw request body."""
    if not signature_header or not signature_header.startswith(SIGNATURE_PREFIX):
        return False

    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header[len(SIGNATURE_PREFIX) :])
