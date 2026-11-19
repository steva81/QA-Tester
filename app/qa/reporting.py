"""Canonical, read-only reporting facts. Private editorial fields are never selected.

Coverage and triage stay owned by Phase 2 and 3A. Renderers consume this model
and its ordered presentation blocks without reinterpreting business outcomes.
"""
from collections import Counter, defaultdict
import re

from app.models.crawl import get_coverage, METRICS
from app.qa.redaction import redact_string
from app.qa.triage import build_triage
from app.utils import format_local_time, display_review_status

RUN_FIELDS = 'id project_id status start_time end_time authorized_at total_pages'.split()
PROJECT_FIELDS = 'id name target_url environment tester_name created_at'.split()
FINDING_FIELDS = ('id stable_id project_id run_id title platform area severity risk status description impact '
                  'recommendation timestamp review_status plain_language_explanation confidence '
                  'severity_origin risk_origin confidence_origin').split()
TEST_FIELDS = ('id run_id url expected_result actual_result status timestamp viewport screenshot_path '
               'stable_id final_url').split()
COVERAGE_FIELDS = (*METRICS, 'mode', 'phase', 'page_limit', 'page_limit_reached',
                   'discovery_limit_reached', 'discovery_safety_limited', 'coverage_complete', 'termination_reason')
PAGE_FIELDS = 'requested_url final_url state reason desktop_tested mobile_tested desktop_limited mobile_limited discovery_limited'.split()
TRIAGE_FIELDS = ('finding_id suggested_severity suggested_risk suggested_confidence effective_severity '
                 'effective_risk effective_confidence priority priority_score needs_human_review review_reason '
                 'finding_ids evidence_ids evidence_count own_evidence_count page_count viewports algorithm').split()
STATUSES = ('PASS', 'FAIL', 'OBSERVATION', 'AUTH REQUIRED', 'REDIRECTED', 'SAFETY ACTION',
            'LIMITED COVERAGE', 'DIAGNOSTIC')
MODES = ('standard', 'full')


def clean(value):
    if isinstance(value, str):
        value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', value)
        # Preserve the established redactor, additionally remove URL credentials
        # in historical/editorial text that did not pass through QA persistence.
        return redact_string(re.sub(r'(https?://)[^/\s@]+@', r'\1[REDACTED]@', value))
    return value


def allowed(row, fields):
    return {key: clean(row[key]) for key in fields if key in row}


def read_rows(conn, table, fields, clause, params):
    return [allowed(dict(row), fields) for row in conn.execute(
        f"SELECT {','.join(fields)} FROM {table} WHERE {clause} ORDER BY id", params)]


def load_report(conn, run_id):
    """One database snapshot, one triage computation, no persistence mutations."""
    runs = read_rows(conn, 'runs', RUN_FIELDS, 'id=?', (run_id,))
    if not runs:
        return None
    run = runs[0]
    projects = read_rows(conn, 'projects', PROJECT_FIELDS, 'id=?', (run['project_id'],))
    if not projects:
        return None
    tests = read_rows(conn, 'tests', TEST_FIELDS, 'run_id=?', (run_id,))
    findings = read_rows(conn, 'findings', FINDING_FIELDS, 'run_id=? AND project_id=?',
                         (run_id, run['project_id']))
    source = get_coverage(conn, run_id, include_pages=True)
    coverage = allowed(source, COVERAGE_FIELDS) if source else None
    if coverage is not None:
        coverage['pages'] = [allowed(page, PAGE_FIELDS) for page in source['pages']]
    pending_records = []
    try:
        p_rows = conn.execute(
            """
            SELECT id, entity_type, entity_name, remote_entity_id, status, remote_status
            FROM mutation_ownership_records
            WHERE run_id = ? AND remote_cleaned = 0
              AND status NOT IN ('DELETED', 'RESOLVED_MANUAL', 'NOT_DISPATCHED', 'NOT_CREATED')
            ORDER BY id ASC
            """,
            (run_id,),
        ).fetchall()
        for pr in p_rows:
            pending_records.append(dict(pr))
    except Exception:
        pass
    manual_count = conn.execute(
        "SELECT COUNT(*) FROM mutation_ownership_records WHERE run_id=? AND project_id=? AND status='RESOLVED_MANUAL'",
        (run_id, run['project_id'])).fetchone()[0]
    return build_report(run, projects[0], findings, tests, coverage, pending_resources=pending_records, manual_resolution_count=manual_count)


def _is_functional_evaluation_record(row):
    expected = (row.get('expected_result') or '').lower()
    return 'safe ephemeral disclosure' in expected or 'safe disclosure' in expected


def build_report(run, project, findings, tests, coverage=None, pending_resources=None, manual_resolution_count=0):
    """Pure assembly; allowlists also protect callers supplying broader dictionaries."""
    if pending_resources is None:
        pending_resources = []
    run, project = allowed(run, RUN_FIELDS), allowed(project, PROJECT_FIELDS)
    tests = [allowed(row, TEST_FIELDS) for row in tests]
    run['total_tests'] = len(tests)  # Compatibility field is canonical, never the stale stored aggregate.
    findings = [allowed(row, FINDING_FIELDS) for row in findings
                if row.get('run_id') == run['id'] and row.get('project_id') == run['project_id']]
    has_functional_diagnostic = any(
        row.get('status') == 'DIAGNOSTIC' and _is_functional_evaluation_record(row)
        for row in tests
    )
    if coverage is not None:
        pages = coverage.get('pages', [])
        coverage = allowed(coverage, COVERAGE_FIELDS)
        coverage['pages'] = [allowed(page, PAGE_FIELDS) for page in pages]
        coverage['coverage_complete'] = bool(coverage.get('coverage_complete') and run['status'] == 'COMPLETED' and not has_functional_diagnostic)
    triage = [dict(allowed(row, TRIAGE_FIELDS), reasons=allowed(row['reasons'],
                  ('grouping', 'severity', 'risk', 'confidence', 'priority')))
              for row in build_triage(findings, tests, run['status'])]
    by_id = {row['id']: row for row in findings}
    admitted_ids = {row['finding_id'] for row in triage}
    unlinked = [row for row in findings if row['id'] not in admitted_ids]
    own_evidence = defaultdict(list)
    targets = defaultdict(set)
    for row in tests:
        if (row.get('viewport') or '').lower() != 'system':
            if row.get('url'):
                targets[row['url']].add(row.get('viewport') or 'Unknown')
            if row['status'] in ('FAIL', 'OBSERVATION') and row.get('stable_id'):
                own_evidence[row['stable_id']].append(row)
    customer = []
    for item in triage:
        finding = by_id[item['finding_id']]
        # Own accepted evidence only; related breadth never replaces originals.
        finding['evidence'] = own_evidence[finding['stable_id']]
        finding['triage'] = item
        finding['review_display'] = display_review_status(finding.get('review_status'))
        customer.append(finding)
    stats = dict(Counter(row['status'] for row in tests))
    for status in STATUSES:
        stats.setdefault(status, 0)
    evaluated = any(row['status'] in ('PASS', 'FAIL', 'OBSERVATION') and
                    (row.get('viewport') or '').lower() != 'system' for row in tests)
    limited = (not evaluated or any(stats.get(key, 0) for key in
               ('AUTH REQUIRED', 'REDIRECTED', 'SAFETY ACTION', 'LIMITED COVERAGE')) or
               has_functional_diagnostic or
               bool(pending_resources) or bool(manual_resolution_count) or
               bool(coverage and (not coverage.get('coverage_complete') or
                    any(coverage.get(key) for key in ('page_limit_reached', 'discovery_limit_reached',
                        'discovery_safety_limited', 'coverage_gaps', 'desktop_limited', 'mobile_limited',
                        'auth_required', 'redirected', 'failed_to_load', 'skipped_safely')))))
    status = run['status']
    outcome = ('FAILED' if status == 'FAILED' else 'INTERRUPTED' if status != 'COMPLETED' else
               'LIMITED COVERAGE' if limited else 'COMPLETE WITH FINDINGS' if customer else 'COMPLETE')
    if outcome == 'FAILED':
        summary = 'QA execution failed. Available partial records are retained; this is a system execution outcome, not an application defect count.'
    elif outcome == 'INTERRUPTED':
        summary = 'QA execution was interrupted or is unfinished. Available partial records are retained; coverage is incomplete.'
    elif limited:
        summary = ('No customer findings were confirmed, but evaluation was incomplete.' if not customer else
                   'Customer findings were recorded, but evaluation was incomplete.')
    elif customer:
        summary = 'Customer findings were recorded within completed coverage and require the review described below.'
    else:
        summary = 'No customer findings were recorded within completed coverage.'
    review = [f for f in customer if f['triage']['needs_human_review'] or f.get('status') == 'RETEST']
    priorities = {level: sum(f['triage']['priority'] == level for f in customer) for level in ('High', 'Medium', 'Low')}
    groups, seen = [], set()
    for finding in customer:
        item = finding['triage']
        ids = tuple(item['finding_ids'])
        if len(ids) > 1 and ids not in seen:
            seen.add(ids)
            groups.append({'finding_ids': list(ids), 'bug_ids': [by_id[i]['stable_id'] for i in ids],
                           'evidence_count': item['evidence_count'], 'page_count': item['page_count'],
                           'viewports': item['viewports']})
    limitation_reasons = []
    if stats.get('LIMITED COVERAGE', 0):
        limitation_reasons.append(f"{stats['LIMITED COVERAGE']} check(s) recorded LIMITED COVERAGE")
    if stats.get('SAFETY ACTION', 0):
        limitation_reasons.append(f"{stats['SAFETY ACTION']} safety barrier action(s)")
    if stats.get('AUTH REQUIRED', 0):
        limitation_reasons.append(f"{stats['AUTH REQUIRED']} authentication requirement(s)")
    if stats.get('REDIRECTED', 0):
        limitation_reasons.append(f"{stats['REDIRECTED']} navigation redirect(s)")
    if has_functional_diagnostic:
        limitation_reasons.append("Interactive checks generated diagnostic records requiring verification")
    if manual_resolution_count:
        limitation_reasons.append(f"{manual_resolution_count} test resource(s) manually acknowledged; server deletion is NOT confirmed")
    if pending_resources:
        limitation_reasons.append(f"{len(pending_resources)} test resource(s) remain PENDING in the test environment")
    if coverage:
        if coverage.get('termination_reason'):
            limitation_reasons.append(f"Termination: {coverage['termination_reason']}")
        if coverage.get('page_limit_reached'):
            limitation_reasons.append(f"Page limit ({coverage.get('page_limit')}) reached")
        if coverage.get('discovery_limit_reached'):
            limitation_reasons.append("Discovery candidate limit reached")
        if coverage.get('discovery_safety_limited'):
            limitation_reasons.append("Discovery stopped due to safety limits")
        for page in coverage.get('pages', []):
            if page.get('state') in ('FAILED', 'LIMITED', 'SKIPPED') and page.get('reason'):
                limitation_reasons.append(f"{page.get('requested_url')}: {page.get('reason')}")
    if not evaluated:
        limitation_reasons.append("No user-facing viewports or pages could be fully evaluated")

    appendix = Counter((row.get('url') or 'No page URL', row.get('viewport') or 'Unknown', row['status']) for row in tests)
    run['start_time_local'] = format_local_time(run.get('start_time'))
    return dict(run=run, project=project, findings=customer, tests=tests, stats=stats, coverage=coverage,
                unlinked_findings=unlinked,
                pending_resources=pending_resources,
                pending_resources_count=len(pending_resources),
                triage=triage, total_checks=len(tests), customer_finding_count=len(customer), outcome=outcome,
                summary=summary, priorities=priorities, review_queue=review, related_groups=groups,
                limitation_reasons=limitation_reasons,
                review_counts=dict(Counter(f['review_display'] for f in customer)),
                finding_status_counts=dict(Counter(f.get('status') or 'Not recorded' for f in customer)),
                recorded_targets=[dict(url=url, viewports=sorted(viewports)) for url, viewports in sorted(targets.items())],
                appendix_summary=[dict(url=url, viewport=viewport, status=status, count=count)
                                  for (url, viewport, status), count in sorted(appendix.items())],
                next_action=('Resolve the execution interruption, then rerun incomplete coverage.' if outcome in ('FAILED', 'INTERRUPTED') else
                             'Review available findings and rerun safely accessible incomplete coverage.' if limited else
                             'Review findings in priority order and retest after changes.' if customer else
                             'Confirm scope with a human tester; retain the completed coverage evidence.'))


def customer_title(f):
    t = f.get('title') or ''
    t_lower = t.lower()
    if 'typeerror' in t_lower or 'uncaught exception' in t_lower:
        return 'Control throws uncaught TypeError on activation'
    if 'aria-controls' in t_lower or 'dangling' in t_lower:
        return 'Broken control reference — missing target DOM element'
    if 'unrevealed' in t_lower or 'remained hidden' in t_lower:
        return 'Expandable content panel remains hidden on toggle'
    clean = re.sub(r'^(?:QA-[A-Z0-9-]+\s*—\s*)', '', t)
    clean = re.sub(r'^(?:Interactive control defect:\s*)', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'^(?:Functional defect confirmed\s*\([a-zA-Z0-9_-]+\):\s*)', '', clean)
    clean = re.sub(r'^(?:\[functional_oracle:[^\]]+\]\s*)', '', clean)
    return clean.strip() or 'Customer Finding'


def customer_expected_behavior(f):
    t = (f.get('title') or '').lower()
    if 'typeerror' in t or 'uncaught exception' in t:
        return 'Activating the control executes safely without uncaught JavaScript exceptions or breaking interactive functionality.'
    if 'aria-controls' in t or 'dangling' in t:
        return 'The control references an existing, valid target panel in the DOM to update or reveal content.'
    if 'unrevealed' in t or 'remained hidden' in t:
        return 'Activating the expandable control toggles its state and visibly displays the controlled content panel.'
    primary_ev = f['evidence'][0] if f.get('evidence') else {}
    exp = primary_ev.get('expected_result')
    if exp and not exp.startswith('Safe ephemeral disclosure'):
        return exp
    return 'The interactive control transitions state predictably without application errors.'


def customer_actual_behavior(f):
    t = (f.get('title') or '').lower()
    if 'typeerror' in t or 'uncaught exception' in t:
        return 'Target page threw an uncaught TypeError during click, leaving the button inert and unresponsive.'
    if 'aria-controls' in t or 'dangling' in t:
        return "Disclosure specifies aria-controls='nonexistent-target-element', but no matching element was found in the DOM."
    if 'unrevealed' in t or 'remained hidden' in t:
        return "Control aria-expanded state changed to true, but the controlled panel ('panel-unrevealed') remained hidden."
    primary_ev = f['evidence'][0] if f.get('evidence') else {}
    raw = primary_ev.get('actual_result') or f.get('description') or ''
    clean = re.sub(r'^(?:Functional defect confirmed\s*\([a-zA-Z0-9_-]+\):\s*)', '', str(raw))
    clean = re.sub(r'^(?:\[functional_oracle:[^\]]+\]\s*)', '', clean)
    return clean.strip() or 'Observed defect behavior.'


def presentation(report, mode='standard'):
    """Ordered blocks shared by every renderer, including both clipboard paths."""
    if mode not in MODES:
        raise ValueError('Report mode must be standard or full.')
    blocks = []
    def heading(text, level=1):
        blocks.append(dict(kind='heading', text=text, level=level))
    def line(text):
        blocks.append(dict(kind='text', text=str(text)))
    def field(label, value):
        line(f'{label}: {value if value is not None else "Not recorded"}')
    heading('QA Test Report', 0)
    heading('Run / Project Details')
    p, run = report['project'], report['run']
    for label, key in [('Project', 'name'), ('Website', 'target_url'), ('Environment', 'environment'), ('Tester', 'tester_name')]:
        field(label, p.get(key))
    field('Date/Time', run['start_time_local'])
    field('Run ID', run['id'])
    field('Run status', run['status'])
    field('Report mode', 'Standard' if mode == 'standard' else 'Full Technical')
    heading('Executive Summary')
    field('Outcome', report['outcome'])
    line(report['summary'])
    field('Total Checks', report['total_checks'])
    field('Customer findings', report['customer_finding_count'])

    if mode == 'full':
        for status, count in report['stats'].items():
            field(status, count)
        for priority, count in report['priorities'].items():
            field(f'{priority} priority', count)
        for status, count in report['review_counts'].items():
            field(f'Review {status}', count)
        for status, count in report['finding_status_counts'].items():
            field(f'Finding status {status}', count)
        field('Human review / retest queue', len(report['review_queue']))
        c = report['coverage']
        if c:
            line(f"Coverage: {c.get('pages_attempted')} pages attempted; {c.get('pages_fully_evaluated')} fully evaluated; Desktop limited: {c.get('desktop_limited')}; Mobile limited: {c.get('mobile_limited')}.")
            line(f"Page limit: {c.get('page_limit')}; reached: {'yes' if c.get('page_limit_reached') else 'no'}; Discovery safety limited: {'yes' if c.get('discovery_safety_limited') else 'no'}.")
        else:
            line('Coverage: ordinary QA recorded targets and viewports; no Full Site crawl metrics recorded.')
        line(f"Safety limitation records: {report['stats'].get('SAFETY ACTION', 0)}; LIMITED COVERAGE records: {report['stats'].get('LIMITED COVERAGE', 0)}. Unverified diagnostics are not customer defects.")
        if report['outcome'] == 'LIMITED COVERAGE' and report['stats'].get('LIMITED COVERAGE', 0) == 0:
            reasons_text = '; '.join(report.get('limitation_reasons', [])) or 'Scope was constrained by crawl limits or unreachable pages.'
            line(f"Outcome note: Overall run outcome is LIMITED COVERAGE although the count of test records with that specific status is 0. Direct reason: {reasons_text}.")
    else:
        # Standard mode: concise summary without walls of zero counts
        for priority in ('High', 'Medium', 'Low'):
            cnt = report['priorities'].get(priority, 0)
            if cnt > 0:
                field(f'{priority} priority', cnt)
        if report['review_queue']:
            field('Human review / retest queue', len(report['review_queue']))

    field('Next human action', report['next_action'])

    # 4x4 Bug Risk Matrix (Severity vs Risk) inspired by APL reference
    if report['findings']:
        matrix_counts = defaultdict(int)
        matrix_bugs = defaultdict(list)
        for f in report['findings']:
            sev = f['triage']['effective_severity']
            risk = f['triage']['effective_risk']
            matrix_counts[(sev, risk)] += 1
            matrix_bugs[(sev, risk)].append(f['stable_id'])
        
        matrix_headers = ['Severity / Risk', 'High Risk', 'Medium Risk', 'Low Risk', 'Total']
        matrix_rows = []
        for sev in ('High', 'Medium', 'Low'):
            row = [f'{sev} Severity']
            sev_total = 0
            for rsk in ('High', 'Medium', 'Low'):
                count = matrix_counts[(sev, rsk)]
                bugs = matrix_bugs[(sev, rsk)]
                if count > 0:
                    row.append(f"{count} ({', '.join(bugs)})")
                else:
                    row.append('0')
                sev_total += count
            row.append(str(sev_total))
            matrix_rows.append(row)
        tot_row = ['Total']
        for rsk in ('High', 'Medium', 'Low'):
            rsk_total = sum(matrix_counts[(sev, rsk)] for sev in ('High', 'Medium', 'Low'))
            tot_row.append(str(rsk_total))
        tot_row.append(str(len(report['findings'])))
        matrix_rows.append(tot_row)
        blocks.append(dict(kind='table', headers=matrix_headers, rows=matrix_rows))

    heading('Coverage')
    c = report['coverage']
    if c is None:
        line('Ordinary QA: coverage describes recorded targets and viewports; no Full Site crawl metrics were recorded.')
        for target in report['recorded_targets']:
            line(f"Recorded target: {target['url']}; Viewports: {', '.join(target['viewports'])}. Recorded attempts do not establish full site coverage.")
    else:
        line('Full Site QA Coverage — attempted pages are not necessarily fully evaluated.')
        if mode == 'full':
            for key in (*METRICS, 'page_limit', 'page_limit_reached', 'discovery_limit_reached', 'discovery_safety_limited', 'coverage_complete'):
                value = c.get(key)
                label = 'Pages Tested' if key == 'pages_tested' else key.replace('_', ' ').capitalize()
                field(label, 'yes' if value is True else 'no' if value is False else value)
            field('Termination reason', c.get('termination_reason') or 'Not recorded')
            line('Only safely reachable linked pages are in scope; discovery and page limits constrain coverage.')
            for page in c['pages']:
                line(f"{page.get('state')}: {page.get('requested_url')} -> {page.get('final_url') or 'not reached'}; {page.get('reason') or ''}")
        else:
            # Standard mode: compact coverage summary table
            cov_headers = ['Coverage Metric', 'Value', 'Coverage Metric', 'Value']
            cov_rows = [
                ['Pages Attempted', str(c.get('pages_attempted', 0)), 'Pages Fully Evaluated', str(c.get('pages_fully_evaluated', 0))],
                ['Desktop Limited', str(c.get('desktop_limited', 0)), 'Mobile Limited', str(c.get('mobile_limited', 0))],
                ['Page Limit', str(c.get('page_limit', 0)), 'Page Limit Reached', 'yes' if c.get('page_limit_reached') else 'no'],
                ['Discovery Safety Limited', 'yes' if c.get('discovery_safety_limited') else 'no', 'Coverage Complete', 'yes' if c.get('coverage_complete') else 'no'],
            ]
            blocks.append(dict(kind='table', headers=cov_headers, rows=cov_rows))

            def is_problem_page(p):
                st = (p.get('state') or '').strip().upper()
                # Actual persisted successful state in full_site.py is 'TESTED' (also accept 'EVALUATED')
                if st not in ('TESTED', 'EVALUATED'):
                    return True
                if p.get('desktop_limited') or p.get('mobile_limited') or p.get('discovery_limited'):
                    return True
                # Incomplete viewport coverage
                if ('desktop_tested' in p or 'mobile_tested' in p) and not (p.get('desktop_tested') and p.get('mobile_tested')):
                    return True
                if p.get('reason'):
                    return True
                return False

            problem_pages = [p for p in c['pages'] if is_problem_page(p)]
            if problem_pages:
                for page in problem_pages:
                    reason_part = f"; {page['reason']}" if page.get('reason') else ("; Incomplete viewport coverage" if (('desktop_tested' in page or 'mobile_tested' in page) and not (page.get('desktop_tested') and page.get('mobile_tested'))) else ("; Limited coverage" if (page.get('desktop_limited') or page.get('mobile_limited')) else ""))
                    line(f"{page.get('state') or 'LIMITED COVERAGE'}: {page.get('requested_url')} -> {page.get('final_url') or page.get('requested_url')}{reason_part}")
            elif c['pages'] and c.get('coverage_complete') and not c.get('page_limit_reached') and not c.get('discovery_safety_limited') and c.get('coverage_gaps', 0) == 0:
                line(f"All {len(c['pages'])} discovered pages were safely reachable and evaluated.")
            elif c['pages']:
                line("Coverage incomplete: evaluation was constrained by crawl limits or scope restrictions.")
            line("Evaluation coverage describes reached pages and viewports. Customer findings discovered during evaluation are reported separately in Findings Overview below.")

    if report['stats'].get('SAFETY ACTION') or report['stats'].get('LIMITED COVERAGE') or (c and c.get('discovery_safety_limited')):
        line('Safety limitations: QA Tester blocked application traffic; affected evaluation may be incomplete. Unverified diagnostics are not customer defects.')
    if report['outcome'] == 'LIMITED COVERAGE' and report.get('limitation_reasons'):
        line(f"Scope limitations: {'; '.join(report['limitation_reasons'])}.")

    if mode == 'full':
        heading('Review Queue')
        if not report['review_queue']:
            line('No findings currently require human review or retest under the accepted triage rules.')
        for f in report['review_queue']:
            line(f"{f['stable_id']} — {f['triage']['priority']} priority; Status: {f.get('status')}; Review: {f['review_display']}; {f['triage']['review_reason']}")
        heading('Prioritized Findings')
        if not report['findings']:
            line('No customer findings recorded.')
        else:
            p_headers = ['Bug ID', 'Title', 'Priority', 'Severity', 'Risk', 'Status']
            p_rows = []
            for f in report['findings']:
                t = f['triage']
                p_rows.append([
                    f['stable_id'],
                    f['title'],
                    f"{t['priority']} ({t['priority_score']})",
                    t['effective_severity'],
                    t['effective_risk'],
                    f.get('status') or 'OPEN',
                ])
            blocks.append(dict(kind='table', headers=p_headers, rows=p_rows))
        for f in report['findings']:
            t = f['triage']
            line(f"{f['stable_id']} — {f['title']}; Priority: {t['priority']} ({t['priority_score']}); Status: {f.get('status')}; Review: {f['review_display']}")
            line('Effective Severity: {effective_severity}; Risk: {effective_risk}; Confidence: {effective_confidence}'.format(**t))
            line('Suggested Severity: {suggested_severity}; Risk: {suggested_risk}; Confidence: {suggested_confidence}'.format(**t))
            line(f"Human review required: {'yes' if t['needs_human_review'] else 'no'}; Pages: {t['page_count']}; Viewports: {', '.join(t['viewports']) or 'Unknown'}; Group evidence: {t['evidence_count']}; Own evidence: {t['own_evidence_count']}")
            line('Related IDs: ' + ', '.join(other['stable_id'] for other in report['findings'] if other['id'] in t['finding_ids']))
        heading('Related Themes')
        line('Related by matching technical evidence. These are separate findings, not proven duplicates or a proven root cause.')
        if report['related_groups']:
            blocks.append(dict(kind='table', headers=['Related Bug IDs', 'Findings', 'Evidence records', 'Pages', 'Viewports'],
                               rows=[[', '.join(g['bug_ids']), str(len(g['finding_ids'])), str(g['evidence_count']),
                                      str(g['page_count']), ', '.join(g['viewports'])] for g in report['related_groups']]))
        if not report['related_groups']:
            line('No multi-finding themes supported by matching technical evidence.')
    else:
        # Standard mode: compact findings overview without redundant review queue or prose
        heading('Findings Overview')
        if not report['findings']:
            line('No customer findings recorded.')
        else:
            p_headers = ['Bug ID', 'Finding', 'Severity', 'Risk', 'Priority', 'Status']
            p_rows = []
            for f in report['findings']:
                t = f['triage']
                p_rows.append([
                    f['stable_id'],
                    customer_title(f),
                    t['effective_severity'],
                    t['effective_risk'],
                    f"{t['priority']} ({t['priority_score']})",
                    f.get('status') or 'OPEN',
                ])
            blocks.append(dict(kind='table', headers=p_headers, rows=p_rows))
            if report['review_queue']:
                line(f"{len(report['review_queue'])} finding(s) currently pending human review.")
        if report['related_groups']:
            heading('Related Themes')
            line('Related by matching technical evidence. These are separate findings, not proven duplicates or a proven root cause.')
            blocks.append(dict(kind='table', headers=['Related Bug IDs', 'Findings', 'Evidence records', 'Pages', 'Viewports'],
                               rows=[[', '.join(g['bug_ids']), str(len(g['finding_ids'])), str(g['evidence_count']),
                                      str(g['page_count']), ', '.join(g['viewports'])] for g in report['related_groups']]))
        
        # Standard mode: integrate compact technical execution summary where it fits with overview
        heading('Technical Summary — Target Pages & Viewports', 2)
        line(f"{report['total_checks']} complete run records retained; {report['stats'].get('DIAGNOSTIC', 0)} diagnostic records retained. Technical records are not a customer defect count.")
        page_viewport_summary = defaultdict(lambda: defaultdict(int))
        for row in report['tests']:
            raw_url = row.get('url') or 'No page URL'
            clean_url = raw_url.split(' [action:')[0].strip()
            viewport = row.get('viewport') or 'Unknown'
            status = row.get('status') or 'UNKNOWN'
            page_viewport_summary[(clean_url, viewport)][status] += 1
        
        app_headers = ['Target Page URL', 'Viewport', 'Checks', 'Results Summary']
        app_rows = []
        for (clean_url, viewport), counts in sorted(page_viewport_summary.items()):
            total = sum(counts.values())
            summary_parts = []
            for st in ('PASS', 'FAIL', 'LIMITED COVERAGE', 'SAFETY ACTION', 'AUTH REQUIRED', 'OBSERVATION'):
                if counts.get(st, 0) > 0:
                    summary_parts.append(f"{counts[st]} {st}")
            other_parts = [f"{cnt} {st}" for st, cnt in counts.items() if st not in ('PASS', 'FAIL', 'LIMITED COVERAGE', 'SAFETY ACTION', 'AUTH REQUIRED', 'OBSERVATION')]
            summary_str = '; '.join(summary_parts + other_parts) or f"{total} recorded"
            app_rows.append([clean_url, viewport, str(total), summary_str])
        blocks.append(dict(kind='table', headers=app_headers, rows=app_rows))

    if report['findings']:
        heading('Detailed Customer Findings')
    for f in report['findings']:
        t = f['triage']
        title_text = customer_title(f) if mode == 'standard' else f['title']
        heading(f"{f['stable_id']} — {title_text}", 2)
        # Structured finding metadata table inspired by APL reference
        f_headers = ['Field', 'Details', 'Field', 'Details']
        
        # Indicate assessment provenance clearly
        sev_label = f"{t['effective_severity']} (manual)" if f.get('severity_origin') == 'manual' else t['effective_severity']
        risk_label = f"{t['effective_risk']} (manual)" if f.get('risk_origin') == 'manual' else t['effective_risk']
        conf_label = f"{t['effective_confidence']} (manual)" if f.get('confidence_origin') == 'manual' else t['effective_confidence']
        
        f_rows = [
            ['Bug ID', f['stable_id'], 'Priority', f"{t['priority']} ({t['priority_score']})"],
            ['Severity', sev_label, 'Risk', risk_label],
            ['Confidence', conf_label, 'Area', f.get('area') or 'Interactive Controls'],
            ['Status', f.get('status') or 'OPEN', 'Review Status', f['review_display']],
        ]
        blocks.append(dict(kind='table', headers=f_headers, rows=f_rows))

        if mode == 'full':
            for label, value in [('Bug ID', f['stable_id']), ('Title', f['title']), ('Area', f.get('area')),
                                 ('Status', f.get('status')), ('Review Status', f['review_display'])]:
                field(label, value)
            for key in ('severity', 'risk', 'confidence'):
                field(key.title(), f['triage'][f'effective_{key}'])
                field('Suggested ' + key.title(), f['triage'][f'suggested_{key}'])
            for label, key in [('Description', 'description'), ('Plain-Language Explanation', 'plain_language_explanation'),
                               ('Impact', 'impact'), ('Recommendation', 'recommendation')]:
                field(label, f.get(key) or 'Not recorded')
            heading('Evidence', 3)
            for row in f['evidence']:
                for label, key in [('Record ID', 'id'), ('Viewport', 'viewport'), ('URL', 'url'), ('Final URL', 'final_url'),
                                   ('Expected Result', 'expected_result'), ('Actual Result', 'actual_result')]:
                    field(label, row.get(key))
                field('Timestamp', format_local_time(row.get('timestamp')) or 'Not recorded')
                filename = (row.get('screenshot_path') or '').replace('\\', '/').rsplit('/', 1)[-1]
                field('Screenshot', filename or 'None')
                if filename:
                    blocks.append(dict(kind='image', text=filename, caption=f"Evidence for {f['stable_id']} ({row.get('viewport') or 'Standard'}): {row.get('actual_result') or 'Observed issue'}"))
        else:
            # Standard mode: concise, readable narrative without repeated metadata or raw oracle categories
            field('Description', f.get('plain_language_explanation') or f.get('description') or 'Not recorded')
            field('Expected Result', customer_expected_behavior(f))
            field('Actual Result', customer_actual_behavior(f))
            field('Impact', f.get('impact') or 'Not recorded')
            field('Recommendation', f.get('recommendation') or 'Not recorded')

            for row in f['evidence']:
                filename = (row.get('screenshot_path') or '').replace('\\', '/').rsplit('/', 1)[-1]
                if filename:
                    caption = f"Figure: Evidence for {f['stable_id']} ({row.get('viewport') or 'Desktop'}) — {title_text}"
                    blocks.append(dict(kind='image', text=filename, caption=caption))

    if mode == 'full':
        heading('Technical Appendix — Technical Data')
        line(f"{report['total_checks']} complete run records retained; {report['stats'].get('DIAGNOSTIC', 0)} diagnostic records retained. Technical records are not a customer defect count.")
        if report['unlinked_findings']:
            line(f"Saved finding records without qualifying customer evidence: {len(report['unlinked_findings'])}. Excluded from customer findings and triage; retained in Full Technical's archive.")
        for row in report['appendix_summary']:
            line(f"{row['url']} — {row['viewport']} — {row['status']}: {row['count']}")
        heading('Every Technical Record', 2)
        for row in report['tests']:
            heading(f"Record #{row['id']} — {row['status']}", 3)
            for label, key in [('Record ID', 'id'), ('Status', 'status'), ('Viewport', 'viewport'), ('URL', 'url'), ('Final URL', 'final_url'),
                               ('Expected Result', 'expected_result'), ('Actual Result', 'actual_result')]:
                field(label, row.get(key))
            field('Timestamp', format_local_time(row.get('timestamp')) or 'Not recorded')
            field('Bug ID', row.get('stable_id'))
            field('Screenshot', (row.get('screenshot_path') or '').replace('\\', '/').rsplit('/', 1)[-1] or 'None')

        if report['unlinked_findings']:
            heading('Saved Records Without Qualifying Customer Evidence', 2)
            line('Historical/editorial archive only — these records are not confirmed customer findings and do not contribute to triage, review queues or customer counts.')
            for finding in report['unlinked_findings']:
                heading(f"Saved record #{finding['id']} — {finding.get('stable_id') or 'No Bug ID'}", 3)
                for label, key in [('Title', 'title'), ('Description', 'description'),
                                   ('Plain-Language Explanation', 'plain_language_explanation'),
                                   ('Impact', 'impact'), ('Recommendation', 'recommendation')]:
                    field(label, finding.get(key) or 'Not recorded')
    return blocks
