import re
from datetime import datetime, timezone

def format_local_time(utc_time_str, fmt='%d %b %Y %H:%M'):
    if not utc_time_str:
        return ""
    try:
        if '.' in utc_time_str:
            dt_utc = datetime.strptime(utc_time_str, '%Y-%m-%d %H:%M:%S.%f').replace(tzinfo=timezone.utc)
        else:
            dt_utc = datetime.strptime(utc_time_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        return dt_utc.astimezone().strftime(fmt)
    except:
        return utc_time_str

def display_severity_risk(val):
    if not val or val == 'Unreviewed':
        return 'Not reviewed yet'
    return val

def display_review_status(val):
    if not val or val == 'PENDING' or val == 'Unreviewed':
        return 'Pending review'
    return val
