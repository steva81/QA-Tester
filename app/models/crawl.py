"""Additive Full Site QA persistence; historical runs stay unchanged."""
from app.qa.redaction import redact_string

METRICS = ('pages_discovered', 'pages_eligible', 'pages_tested', 'desktop_pages_tested',
           'mobile_pages_tested', 'redirected', 'auth_required', 'skipped_safely',
           'failed_to_load', 'coverage_gaps', 'pages_attempted', 'pages_fully_evaluated', 'desktop_limited',
           'mobile_limited', 'safety_limited_pages', 'safety_limited_viewports')


def create_crawl_tables(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS crawl_runs (
        run_id INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
        phase TEXT NOT NULL, current_url TEXT, termination_reason TEXT,
        page_limit INTEGER NOT NULL, query_limit INTEGER NOT NULL, depth_limit INTEGER NOT NULL,
        url_limit INTEGER NOT NULL, navigation_limit INTEGER NOT NULL,
        page_limit_reached INTEGER DEFAULT 0, discovery_limit_reached INTEGER DEFAULT 0, coverage_complete INTEGER DEFAULT 0,
        pages_discovered INTEGER DEFAULT 0, pages_eligible INTEGER DEFAULT 0,
        pages_tested INTEGER DEFAULT 0, desktop_pages_tested INTEGER DEFAULT 0,
        mobile_pages_tested INTEGER DEFAULT 0, redirected INTEGER DEFAULT 0,
        auth_required INTEGER DEFAULT 0, skipped_safely INTEGER DEFAULT 0,
        failed_to_load INTEGER DEFAULT 0, coverage_gaps INTEGER DEFAULT 0)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS crawl_pages (
        run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE, url_key TEXT NOT NULL,
        requested_url TEXT NOT NULL, final_url TEXT, state TEXT NOT NULL,
        eligible INTEGER NOT NULL, desktop_tested INTEGER DEFAULT 0, mobile_tested INTEGER DEFAULT 0,
        depth INTEGER NOT NULL, parent_url TEXT, reason TEXT, form_count INTEGER DEFAULT 0,
        PRIMARY KEY (run_id, url_key))''')

    # Additive migration; preserve historical attempts and outcomes.
    additions = {
        'crawl_runs': {**{key: 'INTEGER DEFAULT 0' for key in METRICS}, 'discovery_safety_limited': 'INTEGER DEFAULT 0'},
        'crawl_pages': {'desktop_limited': 'INTEGER DEFAULT 0', 'mobile_limited': 'INTEGER DEFAULT 0', 'discovery_limited': 'INTEGER DEFAULT 0'},
    }
    for table, columns in additions.items():
        existing = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
        for name, declaration in columns.items():
            if name not in existing:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')


def write_coverage(conn, run_id, snapshot):
    progress = snapshot['progress']
    values = dict(run_id=run_id, phase=progress['phase'], current_url=redact_string(progress['current_url']),
                  termination_reason=redact_string(progress['termination_reason']),
                  page_limit=progress['page_limit'], page_limit_reached=progress['page_limit_reached'],
                  discovery_limit_reached=progress.get('discovery_limit_reached', False),
                  coverage_complete=progress['coverage_complete'], discovery_safety_limited=progress.get('discovery_safety_limited',False), **snapshot['limits'])
    values.update({key: progress.get(key,0) for key in METRICS})
    columns = list(values)
    conn.execute(f"INSERT INTO crawl_runs ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
                 f"ON CONFLICT(run_id) DO UPDATE SET {','.join(f'{k}=excluded.{k}' for k in columns if k != 'run_id')}", list(values.values()))
    for page in snapshot['pages']:
        conn.execute('''INSERT INTO crawl_pages
            (run_id,url_key,requested_url,final_url,state,eligible,desktop_tested,mobile_tested,depth,parent_url,reason,form_count,desktop_limited,mobile_limited,discovery_limited)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,url_key) DO UPDATE SET
            final_url=excluded.final_url,state=excluded.state,eligible=excluded.eligible,
            desktop_tested=excluded.desktop_tested,mobile_tested=excluded.mobile_tested,
            reason=excluded.reason,form_count=excluded.form_count,
            desktop_limited=excluded.desktop_limited,mobile_limited=excluded.mobile_limited,discovery_limited=excluded.discovery_limited''',
            (run_id,page['url_key'],redact_string(page['url']),redact_string(page['final_url']),page['state'],
             page['eligible'],page['desktop_tested'],page['mobile_tested'],page['depth'],
             redact_string(page['parent']),redact_string(page['reason']),page['form_count'],page.get('desktop_limited',False),page.get('mobile_limited',False),page.get('discovery_limited',False)))


def finish_coverage(conn, run_id, phase, reason):
    conn.execute("UPDATE crawl_pages SET state='COVERAGE GAP', reason=? WHERE run_id=? AND state IN ('QUEUED','TESTING','DISCOVERED','ELIGIBLE')", (redact_string(reason), run_id))
    gaps = conn.execute("SELECT COUNT(*) FROM crawl_pages WHERE run_id=? AND (state IN ('COVERAGE GAP','FAILED TO LOAD','LIMITED COVERAGE') OR (eligible=1 AND (desktop_tested=0 OR mobile_tested=0)))", (run_id,)).fetchone()[0]
    truncated = conn.execute('SELECT discovery_limit_reached,discovery_safety_limited FROM crawl_runs WHERE run_id=?', (run_id,)).fetchone()
    gaps += int(bool(truncated and (truncated[0] or truncated[1])))
    conn.execute("UPDATE crawl_runs SET phase=?, coverage_complete=0, coverage_gaps=?, termination_reason=? WHERE run_id=?", (phase, gaps, redact_string(reason), run_id))


def get_coverage(conn, run_id, include_pages=False):
    row = conn.execute('SELECT * FROM crawl_runs WHERE run_id=?', (run_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result['mode'] = 'full'
    result['page_limit_reached'] = bool(result['page_limit_reached'])
    result['coverage_complete'] = bool(result['coverage_complete'])
    result['discovery_limit_reached'] = bool(result['discovery_limit_reached'])
    result['discovery_safety_limited'] = bool(result['discovery_safety_limited'])
    if include_pages:
        result['pages'] = [dict(r) for r in conn.execute('SELECT * FROM crawl_pages WHERE run_id=? ORDER BY rowid', (run_id,))]
    return result
