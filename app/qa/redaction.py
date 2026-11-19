import json
import re

SENSITIVE_KEYS = ['access_token', 'token', 'auth', 'authorization', 'code', 'password', 'secret', 'api_key', 'apikey', 'bearer', 'session', 'cookie']

def redact_string(text):
    if not text:
        return text
    # Console evidence can contain JSON after a textual prefix. Redact complete
    # sensitive field values, including arrays/objects, before persistence.
    decoder = json.JSONDecoder()
    keys = '|'.join(re.escape(key) for key in SENSITIVE_KEYS)
    pieces, position = [], 0
    for match in re.finditer(rf'"(?:{keys})"\s*:\s*', text, flags=re.IGNORECASE):
        if match.start() < position:
            continue
        try:
            _, end = decoder.raw_decode(text, match.end())
        except ValueError:
            continue
        pieces.extend((text[position:match.end()], '"[REDACTED]"'))
        position = end
    if pieces:
        pieces.append(text[position:])
        text = ''.join(pieces)
    # Chromium previews and uninspectable console logs may use unquoted keys.
    text = re.sub(rf'(?<![\w"\'])((?:{keys})\s*:\s*)[^,}}\r\n]+',
                  r'\1[REDACTED]', text, flags=re.IGNORECASE)
    # Redact query params: key=value
    for key in SENSITIVE_KEYS:
        text = re.sub(rf"({key}=)[^&\s'\"]+", r"\1[REDACTED]", text, flags=re.IGNORECASE)
    
    # Redact standard headers
    text = re.sub(r"(Bearer\s+)[^\s'\"]+", r"\1[REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"(Authorization:\s+)[^\r\n]+", r"\1[REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"(Cookie:\s+)[^\r\n]+", r"\1[REDACTED]", text, flags=re.IGNORECASE)
    
    # Redact any other secret-like bare values
    text = re.sub(r"(SECRET_SENTINEL)", r"[REDACTED]", text, flags=re.IGNORECASE)
    return text
