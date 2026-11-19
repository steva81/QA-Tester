import re
from datetime import datetime, timezone

def format_local_time(utc_time_str, fmt='%d %b %Y %H:%M'):
    if not utc_time_str:
        return ""
    if not isinstance(utc_time_str, str):
        utc_time_str = str(utc_time_str)
    raw = utc_time_str.strip()
    if not raw:
        return ""
    try:
        dt = None
        # Check if ISO format with explicit timezone (e.g. 2026-10-18T12:56:00Z or +02:00)
        if 'T' in raw or '+' in raw[10:] or '-' in raw[10:] or raw.endswith('Z'):
            try:
                dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            except Exception:
                pass

        if dt is None:
            # Stored SQLite CURRENT_TIMESTAMP format is in UTC
            for pattern in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
                try:
                    dt = datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc)
                    break
                except Exception:
                    pass

        if dt is None:
            # Non-standard or unparseable datetime string; do not alter or guess timezone
            return utc_time_str

        dt_local = dt.astimezone()
        offset = dt_local.strftime('%z')
        if offset:
            tz_label = f"UTC{offset[:3]}:{offset[3:]}"
        else:
            tz_label = "UTC"
        return f"{dt_local.strftime(fmt)} ({tz_label})"
    except Exception:
        return utc_time_str


def display_severity_risk(val):
    if not val or val == 'Unreviewed':
        return 'Not reviewed yet'
    return val

def display_review_status(val):
    if not val or val == 'PENDING' or val == 'Unreviewed':
        return 'Pending review'
    return val

from urllib.parse import urlparse

def get_canonical_origin(url):
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.hostname:
            return ""
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname.lower().encode('idna').decode('ascii')
        port = parsed.port
        if port is None:
            if scheme == 'http':
                port = 80
            elif scheme == 'https':
                port = 443
        return f"{scheme}://{hostname}:{port}"
    except Exception:
        return ""
