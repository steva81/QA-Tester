"""Local, read-only triage of Phase 2 customer findings.

Groups are candidate relationships within one project/run, never destructive
merges. Only linked FAIL/OBSERVATION evidence outside the System viewport is
eligible. See PHASE3A_TRIAGE_VALIDATION.md for normalization and scoring rules.
"""
from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit

from app.qa.redaction import redact_string

VERSION = '3a-v1'
LEVELS = {'Unreviewed': 0, 'Low': 1, 'Medium': 2, 'High': 3}
CUSTOMER_STATUSES = ('FAIL', 'OBSERVATION')


def normalize_message(value: str) -> str:
    """Keep case, targets and substantive numbers; remove bounded log volatility."""
    text = redact_string(value or '')
    text = re.sub(r'\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\b', '<timestamp>', text)
    text = re.sub(r'([\w./:\\-]+\.(?:m?js|cjs|tsx?|jsx)):\d+:\d+\b', r'\1:<location>', text)
    text = re.sub(r'(?i)(\b(?:request_id|trace_id)\s*[=:]\s*)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', r'\1<generated-id>', text)
    text = re.sub(r'^\[(?:Desktop|Mobile)(?: \d+x\d+)?\]\s*', '', text)
    return ' '.join(text.split())


def canonical_url(value: str) -> str:
    """Drop fragments and credentials, preserve path and ordered query identity."""
    clean = redact_string(value or '')
    try:
        parsed = urlsplit(clean)
        if parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname:
            return clean
        host = parsed.hostname.lower()
        if ':' in host:
            host = '[' + host + ']'
        port = parsed.port
        if port and not (parsed.scheme.lower() == 'http' and port == 80 or parsed.scheme.lower() == 'https' and port == 443):
            host += f':{port}'
        return urlunsplit((parsed.scheme.lower(), host, parsed.path or '/', parsed.query, ''))
    except ValueError:
        return clean


@dataclass(frozen=True)
class Signal:
    category: str
    identity: str
    strong: bool
    severity: str
    blocking: bool
    reason: str


def technical_signal(finding: dict, evidence: dict) -> Signal:
    """Recognize only captured technical signals, never private/editorial text."""
    actual = normalize_message(evidence['actual_result'])
    page = canonical_url(evidence.get('final_url') or evidence['url'])
    area = finding.get('area') or ''
    http = re.fullmatch(r'Server returned HTTP (\d{3})', actual)
    if http and area == 'Page Load / Server Response':
        code = int(http[1])
        severity = 'High' if code >= 500 else 'Medium'
        return Signal('http', f'{page}|HTTP {code}', True, severity, True,
                      'The page returned an HTTP error; availability is affected at the recorded URL.')
    if area == 'Page Load / Server Response' and (actual == 'Failed to get response status' or actual.startswith('Navigation failed:')):
        return Signal('navigation', f'{page}|{actual}', True, 'High', True,
                      'Navigation or response retrieval failed; local reachability still needs verification.')
    js = re.match(r'^(?:Console/JS errors detected|Console errors detected):\s*(.+)$', actual)
    if js and area == 'JavaScript Execution':
        message = js[1]
        exception = bool(re.search(r'\b(?:TypeError|ReferenceError|SyntaxError|RangeError|URIError|EvalError|Error):\s*\S', message))
        detail = re.sub(r'^(?:\w*Error:\s*|console\.error:\s*)', '', message, flags=re.I)
        headline = detail.split(' at ', 1)[0]
        # Volatile metadata cannot turn a generic error into a specific defect.
        headline = re.sub(r'(?i)\b(?:request_id|trace_id)\s*[=:]\s*<generated-id>', '', headline)
        headline = re.sub(r'(?i)(?:\btimestamp\s*[=:]\s*)?<timestamp>', '', headline).strip()
        generic = headline.lower() in ('error', 'failed', 'warning', 'failed to fetch', 'request failed', 'network error', 'unknown error')
        meaningful = [word for word in re.findall(r'\w+', headline) if word.lower() not in ('error', 'failed', 'warning', 'at', 'timestamp')]
        strong = not generic and (len(meaningful) >= 2 or bool(re.fullmatch(r'[A-Z][A-Z0-9_]{7,}', headline)))
        category = 'js-exception' if exception else 'console'
        # Generic messages have no reliable root identity. Keep their page scope.
        identity = message if strong else f'{page}|{message}'
        return Signal(category, identity, strong, 'Medium' if exception else 'Low', False,
                      'A browser exception was recorded; affected functionality is not established.' if exception else
                      'A console signal was recorded; user-visible impact is not established.')
    resource = re.fullmatch(r'Failed resource: (\w+) failed: (\S+) \((.+)\)', actual)
    if resource and area == 'Network':
        kind, url, failure = resource.groups()
        return Signal('resource', f'{kind}|{canonical_url(url)}|{failure}', True, 'Medium', False,
                      'A GET resource failed; its importance to the user workflow is unverified.')
    if area == 'Responsive Layout' and actual == 'Horizontal overflow detected on mobile':
        return Signal('layout', f'{page}|{actual}', True, 'Low', False,
                      'Horizontal overflow affects the recorded layout; frequency does not imply a critical defect.')
    if area == 'DOM / Basic Quality' and actual.startswith('Duplicate DOM ID found: '):
        return Signal('dom-id', f'{page}|{actual}', True, 'Low', False,
                      'A duplicate DOM ID was recorded on this page; functional impact is unverified.')
    if area == 'DOM / Basic Quality' and actual in ('Missing or empty document title', 'Broken image detected'):
        return Signal('dom', f'{page}|{actual}', True, 'Low', False,
                      'A basic document-quality issue was recorded on this page.')
    if area == 'Form Inspection' and actual == 'Missing form label for input':
        # The engine does not capture the input target; do not invent it.
        return Signal('form-label', f'{page}|{actual}', False, 'Medium', False,
                      'An input lacks a label; the affected target and accessibility impact need human inspection.')
    actual_lower = actual.lower()
    if 'dangling_aria_controls' in actual_lower or 'nonexistent-target-element' in actual_lower:
        return Signal('interactive-dangling', f'{page}|{actual}', True, 'Medium', False,
                      'An interactive disclosure references a missing DOM element via aria-controls.')
    if 'content_not_revealed' in actual_lower or 'panel-unrevealed' in actual_lower:
        return Signal('interactive-unrevealed', f'{page}|{actual}', True, 'Medium', False,
                      'An interactive control toggled state but its controlled content remained hidden.')
    if 'page_script_error' in actual_lower or 'leaving control inert' in actual_lower or 'typeerror' in actual_lower:
        return Signal('interactive-crash', f'{page}|{actual}', True, 'High', True,
                      'Activating the interactive control caused an uncaught script error.')
    return Signal('unknown', f'{page}|{actual}', False, 'Unreviewed', False,
                  'Captured evidence does not match a supported triage rule; human assessment is required.')



def fingerprint(finding: dict, evidence: list[dict]) -> tuple[str, list[Signal], bool]:
    signals = [technical_signal(finding, row) for row in evidence]
    strong = all(signal.strong for signal in signals)
    identities = sorted({(signal.category, signal.identity) for signal in signals})
    # Redaction can remove the distinguishing detail. Do not equate two such
    # findings merely because their sanitized strings now match.
    reliable = strong and not any('[REDACTED]' in identity for _, identity in identities)
    canonical = {'version': VERSION, 'project': finding['project_id'],
                 'area': normalize_message(finding.get('area')),
                 'title': normalize_message(finding.get('title')), 'signals': identities}
    if not reliable:
        canonical['isolated_finding'] = finding['id']
    payload = json.dumps(canonical, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest(), signals, reliable


def effective_value(finding: dict, field: str, suggested: str) -> tuple[str, str]:
    origin = finding.get(field + '_origin', 'legacy')
    current = finding.get(field)
    if origin != 'baseline' and current in LEVELS:
        return current, 'manual' if origin == 'manual' else 'legacy/current'
    return suggested, 'suggested'


def build_triage(findings: list[dict], tests: list[dict], run_status: str) -> list[dict]:
    """Pure projection. Inputs and authoritative findings/evidence are untouched."""
    evidence_by_identity = defaultdict(list)
    for row in tests:
        if row.get('stable_id') and row.get('status') in CUSTOMER_STATUSES and (row.get('viewport') or '').strip().lower() != 'system':
            evidence_by_identity[(row['run_id'], row['stable_id'])].append(row)
    groups = defaultdict(list)
    for finding in findings:
        evidence = evidence_by_identity[(finding['run_id'], finding['stable_id'])]
        if not evidence:
            continue
        signature, signals, reliable = fingerprint(finding, evidence)
        # Even callers supplying multiple projects/runs cannot cross-group them.
        groups[(finding['project_id'], finding['run_id'], signature)].append((finding, evidence, signals, reliable))
    output = []
    for (_, _, signature), members in groups.items():
        evidence = {row['id']: row for _, rows, _, _ in members for row in rows}
        signals = [signal for _, _, items, _ in members for signal in items]
        reliable = all(member[3] for member in members)
        pages = {canonical_url(row.get('final_url') or row['url']) for row in evidence.values()}
        viewports = set()
        for row in evidence.values():
            label = row.get('viewport') or ''
            if re.match(r'^Desktop(?:\b)', label):
                viewports.add('Desktop')
            elif re.match(r'^Mobile(?:\b)', label):
                viewports.add('Mobile')
        count, page_count = len(evidence), len(pages)
        both = viewports == {'Desktop', 'Mobile'}
        severity = max((signal.severity for signal in signals), key=LEVELS.get)
        blocking = any(signal.blocking for signal in signals)
        unknown = any(signal.category == 'unknown' for signal in signals)
        risk = ('Unreviewed' if unknown else 'High' if blocking and (both or page_count >= 3) else
                'Medium' if blocking or severity == 'Medium' and (both or page_count >= 2) else 'Low')
        confidence = 'High' if reliable and count >= 2 else 'Medium' if reliable else 'Low'
        grouping_reason = ('Matching specific technical signatures; grouping indicates related evidence, not a proven root cause.' if reliable else
                           'Insufficient or redacted identity; this finding is kept separate from other findings.')
        severity_reason = ' '.join(sorted({signal.reason for signal in signals}))
        risk_reason = (f'{page_count} affected page(s), {len(viewports)} recognized viewport(s); ' +
                       ('page access failed in customer evidence.' if blocking else 'business importance and workflow impact were not measured.'))
        confidence_reason = (f'{count} customer evidence record(s); ' +
                             ('a specific matching signature is available.' if reliable else 'identity or impact is ambiguous.') +
                             ' Safety, diagnostic and coverage records do not contribute.')
        breadth = min(max(page_count - 1, 0), 2) + int(both) + min(max(count - 1, 0), 2)
        for finding, own_evidence, _, _ in members:
            sev, sev_source = effective_value(finding, 'severity', severity)
            exposure, risk_source = effective_value(finding, 'risk', risk)
            certainty, conf_source = effective_value(finding, 'confidence', confidence)
            pending = finding.get('review_status') not in ('APPROVED', 'REJECTED')
            score = 3 * LEVELS[sev] + 2 * LEVELS[exposure] + LEVELS[certainty] + breadth + int(pending)
            inactive = finding.get('status') == 'FIXED' or finding.get('review_status') == 'REJECTED'
            if inactive:
                score = 0
            priority = 'High' if score >= 17 else 'Medium' if score >= 10 else 'Low'
            priority_reason = (f'3×Severity({sev}, {sev_source}) + 2×Risk({exposure}, {risk_source}) + '
                               f'Confidence({certainty}, {conf_source}) + breadth({breadth}) + pending review({int(pending)}).')
            if inactive:
                priority_reason += ' Fixed or rejected findings have priority score 0.'
            review_reasons = []
            if pending:
                review_reasons.append('Review status is pending.')
            if unknown:
                review_reasons.append('The technical signal is outside supported triage rules.')
            if confidence == 'Low':
                review_reasons.append('The technical identity or affected target is ambiguous.')
            if any(value == 'Unreviewed' for value in (sev, exposure, certainty)):
                review_reasons.append('An authoritative assessment remains Unreviewed.')
            if run_status != 'COMPLETED':
                review_reasons.append('The run did not complete; only available customer evidence is triaged.')
            review = bool(review_reasons)
            decision = finding.get('review_status')
            decision_reason = (
                'The tester has rejected this finding; suggestions remain advisory.' if decision == 'REJECTED' else
                'The tester has approved this finding; suggestions remain advisory.' if decision == 'APPROVED' else '')
            output.append({'finding_id': finding['id'], 'fingerprint': signature,
                           'suggested_severity': severity, 'suggested_risk': risk,
                           'suggested_confidence': confidence, 'priority': priority, 'priority_score': score,
                           'effective_severity': sev, 'effective_risk': exposure, 'effective_confidence': certainty,
                           'needs_human_review': review,
                           'review_reason': ' '.join(([decision_reason] if decision_reason else []) + review_reasons),
                           'finding_ids': sorted(member[0]['id'] for member in members),
                           'evidence_ids': sorted(evidence), 'evidence_count': count,
                           'own_evidence_count': len(own_evidence), 'page_count': page_count,
                           'viewports': sorted(viewports), 'algorithm': VERSION,
                           'reasons': {'grouping': grouping_reason, 'severity': severity_reason,
                                       'risk': risk_reason, 'confidence': confidence_reason, 'priority': priority_reason}})
    return sorted(output, key=lambda item: (-item['priority_score'], item['finding_id']))


def triage_run(conn, run_id: int) -> list[dict]:
    """Allowlist source fields; never read My Learning Notes or report prose."""
    run = conn.execute('SELECT project_id,status FROM runs WHERE id=?', (run_id,)).fetchone()
    if run is None:
        return []
    findings = [dict(row) for row in conn.execute('''SELECT id,project_id,run_id,stable_id,title,area,
        severity,risk,confidence,severity_origin,risk_origin,confidence_origin,status,review_status
        FROM findings WHERE run_id=? AND project_id=? ORDER BY id''', (run_id, run['project_id']))]
    tests = [dict(row) for row in conn.execute('''SELECT id,run_id,stable_id,status,viewport,url,final_url,actual_result
        FROM tests WHERE run_id=? AND status IN ('FAIL','OBSERVATION') AND stable_id IS NOT NULL ORDER BY id''', (run_id,))]
    return build_triage(findings, tests, run['status'])
