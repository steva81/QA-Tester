"""Canonical run record counts across clipboard, Results and official exports."""
import io
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from docx import Document


class Text(HTMLParser):
    def __init__(self, source, identifier=None):
        super().__init__()
        self.identifier = identifier
        self.depth = 0
        self.parts = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        if self.identifier and dict(attrs).get('id') == self.identifier:
            self.depth = 1
        elif self.depth:
            self.depth += 1

    def handle_endtag(self, tag):
        if self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self.identifier is None or self.depth:
            self.parts.append(data)

    @property
    def text(self):
        return ''.join(self.parts)


@pytest.mark.parametrize('counts,full_site', [
    ({'PASS': 2, 'OBSERVATION': 2, 'REDIRECTED': 1, 'SAFETY ACTION': 4,
      'LIMITED COVERAGE': 2, 'DIAGNOSTIC': 6}, True),
    ({'PASS': 1, 'FAIL': 1, 'OBSERVATION': 1, 'REDIRECTED': 1,
      'SAFETY ACTION': 1, 'LIMITED COVERAGE': 1, 'DIAGNOSTIC': 1}, True),
    ({'PASS': 2, 'FAIL': 1, 'OBSERVATION': 1, 'REDIRECTED': 1,
      'AUTH REQUIRED': 1}, False),
    ({'PASS': 2}, False),
])
def test_all_reports_use_complete_run_count_and_exclude_private_notes(tmp_path, monkeypatch, counts, full_site):
    from app.models.database import init_db, get_db_connection
    import app.paths as paths
    monkeypatch.setenv('QA_TESTER_DB_PATH', str(tmp_path / 'reports.sqlite'))
    monkeypatch.setattr(paths, 'get_writable_dir', lambda: str(tmp_path))
    paths.ensure_dirs()
    init_db()
    from server import app
    conn = get_db_connection()
    pid = conn.execute("INSERT INTO projects(name,target_url,environment,tester_name) VALUES ('Metrics','http://fixture.invalid','Local','Tester')").lastrowid
    rid = conn.execute("INSERT INTO runs(project_id,status) VALUES (?,'COMPLETED')", (pid,)).lastrowid
    sentinel = 'PRIVATE-TEST-12345'
    fid = None
    for status, count in counts.items():
        for index in range(count):
            stable = f'{status}-{index}' if status in ('FAIL', 'OBSERVATION') else None
            conn.execute('INSERT INTO tests(run_id,url,status,viewport,stable_id,expected_result,actual_result) VALUES (?,?,?,?,?,?,?)',
                         (rid, 'http://fixture.invalid', status, 'Desktop', stable, 'Expected', 'Evidence'))
            if stable:
                fid = conn.execute('INSERT INTO findings(project_id,run_id,stable_id,title,description,area,learning_explanation_sv) VALUES (?,?,?,?,?,?,?)',
                                   (pid, rid, stable, 'Fixture finding', 'Evidence', 'Console', sentinel)).lastrowid
    if full_site:
        conn.execute("INSERT INTO crawl_runs(run_id,phase,page_limit,query_limit,depth_limit,url_limit,navigation_limit,desktop_limited,mobile_limited) VALUES (?,'COMPLETED',10,10,3,100,100,1,1)", (rid,))
    conn.execute("UPDATE findings SET plain_language_explanation='Fixture explanation', impact='Review needed', recommendation='Review needed', severity='Unreviewed', risk='Unreviewed' WHERE run_id=?", (rid,))
    conn.commit()
    conn.close()
    from app.routes.exports import get_report_data
    canonical = len(get_report_data(rid)[3])
    assert canonical == sum(counts.values())
    client = app.test_client()
    response = client.get(f'/run/{rid}')
    assert response.status_code == 200
    results = response.get_data(as_text=True)
    visible = Text(results).text
    assert int(re.search(r'Total Checks\s+(\d+)', visible).group(1)) == canonical
    documents = {'run clipboard': Text(results, 'run-report-text').text}
    if fid:
        page = client.get(f'/finding/{fid}').get_data(as_text=True)
        assert sentinel in page  # Actually populated My Learning Notes.
        for identifier in ('report-text', 'report-html'):
            documents[identifier] = Text(page, identifier).text
    response = client.get(f'/export/{rid}/html')
    assert response.status_code == 200
    documents['html'] = Text(response.get_data(as_text=True)).text
    for format in ('txt', 'markdown', 'docx'):
        response = client.post(f'/api/export/{rid}/{format}')
        assert response.status_code == 200
        data = (Path(paths.get_exports_dir()) / response.json['filename']).read_bytes()
        if format == 'docx':
            doc = Document(io.BytesIO(data))
            documents[format] = '\n'.join(p.text for p in doc.paragraphs)
        else:
            documents[format] = data.decode()
    for name, text in documents.items():
        assert sentinel not in text, name
        assert int(re.search(r'Total Checks:\s*(?:\*\*)?\s*(\d+)', text).group(1)) == canonical, name
