import hashlib
import hmac

import pytest

from agents.watcher.main import InvalidSignatureError, verify_signature

SECRET = "test-secret"
BODY = b'{"ref": "refs/heads/main"}'


def _sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signature_passes():
    verify_signature(SECRET, BODY, _sig(SECRET, BODY))


def test_missing_signature_raises():
    with pytest.raises(InvalidSignatureError):
        verify_signature(SECRET, BODY, None)


def test_wrong_secret_raises():
    with pytest.raises(InvalidSignatureError):
        verify_signature(SECRET, BODY, _sig("wrong-secret", BODY))


def test_malformed_header_raises():
    with pytest.raises(InvalidSignatureError):
        verify_signature(SECRET, BODY, "not-a-valid-header")


def test_tampered_body_raises():
    tampered = b'{"ref": "refs/heads/evil"}'
    with pytest.raises(InvalidSignatureError):
        verify_signature(SECRET, tampered, _sig(SECRET, BODY))
