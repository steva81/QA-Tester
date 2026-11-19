"""Security middleware and guards for local QA Tester server.

Guards the loopback API against:
1. DNS rebinding / host header spoofing (rejects any non-loopback Host header).
2. Cross-origin / cross-site requests (rejects foreign Origin, foreign Referer,
   and cross-site/same-site on mutating requests and /api/ endpoints).
3. CSRF via explicit header requirement for headerless non-browser clients (X-QA-Request: 1).
"""
import re
from urllib.parse import urlsplit
from flask import request, jsonify

_AUTHORITY = re.compile(
    r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?",
    re.IGNORECASE,
)
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def parse_local_origin(value, *, origin_only):
    if not isinstance(value, str) or not value:
        raise ValueError("Missing URL")
    if "\\" in value or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("Invalid URL characters")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Invalid scheme")
    if not _AUTHORITY.fullmatch(parsed.netloc):
        raise ValueError("Invalid loopback authority")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Credentials are prohibited")
    if "#" in value:
        raise ValueError("Fragment is prohibited")
    if origin_only and (parsed.path or "?" in value):
        raise ValueError("Origin must not contain path/query")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid port")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Invalid port")
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme, parsed.hostname.lower(), port


def _deny(message, status=403):
    return jsonify(status="error", error=message), status


def validate_request_security():
    try:
        expected = parse_local_origin(
            f"{request.scheme}://{request.host}", origin_only=True
        )
    except (ValueError, TypeError):
        return _deny("Invalid Host header", 400)

    site = request.headers.get("Sec-Fetch-Site", "").strip().lower()
    if site == "cross-site":
        return _deny("Cross-site requests are prohibited")

    mutating = request.method not in _SAFE_METHODS
    sensitive = request.path == "/api" or request.path.startswith("/api/")
    if not (mutating or sensitive):
        return None

    if mutating and site == "same-site":
        return _deny("Cross-origin same-site writes are prohibited")

    has_origin = "Origin" in request.headers
    has_referer = "Referer" in request.headers
    for name, present, origin_only in (
        ("Origin", has_origin, True),
        ("Referer", has_referer, False),
    ):
        if present:
            try:
                actual = parse_local_origin(
                    request.headers[name], origin_only=origin_only
                )
            except (ValueError, TypeError):
                return _deny(f"Invalid {name} header")
            if actual != expected:
                return _deny(f"Cross-origin {name} is prohibited")

    if mutating and not (has_origin or has_referer):
        explicit_client = request.headers.get("X-QA-Request") == "1"
        if not explicit_client or site not in {"", "same-origin"}:
            return _deny("Origin, Referer or explicit local-client header required")
    return None


def init_security(app):
    """Register security guards on the Flask app."""
    app.before_request(validate_request_security)
