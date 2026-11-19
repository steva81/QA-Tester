"""Conservative URL identity and read-only navigation policy."""
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, unquote
from app.utils import get_canonical_origin

TRACKING_KEYS = {'fbclid', 'gclid', 'dclid', 'msclkid', 'mc_cid', 'mc_eid'}
UNSAFE = re.compile(r'(?:^|[^a-z])(logout|log[ -]?out|sign[ -]?out|delete|remove|deactivate|destroy|unsubscribe|cancel|deletion)(?:$|[^a-z])', re.I)
AUTH_PATH = re.compile(r'/(login|signin|sign-in|auth)(?:/|$)', re.I)


def normalize_crawl_url(url):
    p = urlsplit(url)
    if p.scheme.lower() not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('Only HTTP(S) URLs without embedded credentials are eligible')
    host = p.hostname.lower().encode('idna').decode('ascii')
    host = f'[{host}]' if ':' in host else host
    port = p.port
    if port is not None and port != (443 if p.scheme.lower() == 'https' else 80):
        host += f':{port}'
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in TRACKING_KEYS]
    # Stable key sort preserves repeated-value order, which can be meaningful.
    query.sort(key=lambda pair: pair[0])
    return urlunsplit((p.scheme.lower(), host, p.path or '/', urlencode(query), ''))


def link_reason(url, origin, label=''):
    try:
        p = urlsplit(url)
        if p.scheme.lower() not in ('http', 'https'):
            return 'Non-HTTP navigation'
        if p.username or p.password:
            return 'Embedded credentials'
        if get_canonical_origin(url) != origin:
            return 'External origin'
        action_text = re.sub(r'([a-z])([A-Z])', r'\1 \2', unquote(p.path + '?' + p.query) + ' ' + label)
        if UNSAFE.search(action_text):
            return 'Potential destructive or sign-out action'
        if AUTH_PATH.search(p.path):
            return 'Authentication entry point'
        if re.search(r'\.(pdf|zip|png|jpe?g|gif|svg|mp4|mp3|exe|dmg)$', p.path, re.I):
            return 'Non-page resource'
        return None
    except (ValueError, TypeError):
        return 'Invalid URL'


def query_family(url):
    p = urlsplit(url)
    return p.path, tuple(sorted(set(k for k, _ in parse_qsl(p.query, keep_blank_values=True))))
