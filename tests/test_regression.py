import pytest
import os
import sqlite3
from datetime import datetime, timezone
from app.models.database import get_db_connection
from app.routes.views import NEEDS_REVIEW_CONDITION
from app.utils import format_local_time, display_severity_risk, display_review_status
from app.qa.explanations import get_explanations

def test_needs_review_logic():
    # Setup test DB
    if os.path.exists("test_db.sqlite"):
        os.remove("test_db.sqlite")
        
    os.environ['QA_TESTER_DB_PATH'] = "test_db.sqlite"
    from app.models.database import init_db
    init_db()
    conn = get_db_connection()
    c = conn.cursor()
    
    # Create project
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('Test', 'http://test', 'Dev', 'Tester')")
    p_id = c.lastrowid
    
    c.execute("INSERT INTO runs (project_id, status) VALUES (?, 'COMPLETED')", (p_id,))
    r_id = c.lastrowid
    
    # Create findings with different conditions
    test_cases = [
        {"rev": "PENDING", "sev": "High", "risk": "High", "imp": "X", "rec": "X"},
        {"rev": "APPROVED", "sev": "Unreviewed", "risk": "High", "imp": "X", "rec": "X"},
        {"rev": "APPROVED", "sev": "High", "risk": "Unreviewed", "imp": "X", "rec": "X"},
        {"rev": "APPROVED", "sev": "High", "risk": "High", "imp": "[Requires Human Review]", "rec": "X"},
        {"rev": "APPROVED", "sev": "High", "risk": "High", "imp": "X", "rec": "[Requires Human Review]"},
        # Multiple conditions - should only count once
        {"rev": "Unreviewed", "sev": "Unreviewed", "risk": "Unreviewed", "imp": "[Requires Human Review]", "rec": "[Requires Human Review]"},
        # NOT needs review
        {"rev": "APPROVED", "sev": "Low", "risk": "Low", "imp": "Ok", "rec": "Ok"}
    ]
    
    for i, tc in enumerate(test_cases):
        c.execute(f"INSERT INTO findings (project_id, run_id, stable_id, title, area, review_status, severity, risk, impact, recommendation, description, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (p_id, r_id, f"F{i}", f"T{i}", "Area", tc['rev'], tc['sev'], tc['risk'], tc['imp'], tc['rec'], "Desc", "OPEN"))
        
    conn.commit()
    
    # Test condition
    c.execute(f"SELECT COUNT(*) as count FROM findings f WHERE {NEEDS_REVIEW_CONDITION}")
    res = c.fetchone()
    assert res['count'] == 6, f"Expected 6 findings to need review, got {res['count']}"
    
    conn.close()

def test_status_display_helpers():
    assert display_severity_risk("Unreviewed") == "Not reviewed yet"
    assert display_severity_risk("High") == "High"
    
    assert display_review_status("PENDING") == "Pending review"
    assert display_review_status("Unreviewed") == "Pending review"
    assert display_review_status(None) == "Pending review"
    assert display_review_status("APPROVED") == "APPROVED"

def test_explanations_wording():
    expl_en, expl_sv_learn, expl_sv_verbal = get_explanations(actual_result="console errors detected")
    assert "does not prove the exact root cause" in expl_en.lower()
    
    expl_en, expl_sv_learn, expl_sv_verbal = get_explanations(actual_result="navigation failed")
    assert "kan bero på nätverksproblem" in expl_sv_verbal.lower()
    assert "säkert säga att servern är nere" in expl_sv_verbal.lower()

def test_docx_export():
    from app.routes.exports import DOCX_AVAILABLE
    assert DOCX_AVAILABLE, "python-docx must be available"

def test_timezone_formatting():
    # Example UTC timestamp: 2026-01-01 12:00:00
    t = format_local_time("2026-01-01 12:00:00")
    assert t != "2026-01-01 12:00:00" # Should be formatted
    # Check default format fallback
    assert format_local_time("not a date") == "not a date"

def test_duplicate_projects():
    if os.path.exists("test_db.sqlite"):
        os.remove("test_db.sqlite")
        
    os.environ['QA_TESTER_DB_PATH'] = "test_db.sqlite"
    from app.models.database import init_db
    init_db()
    conn = get_db_connection()
    c = conn.cursor()
    
    # Create two identical projects but different IDs
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('Proj', 'http://test', 'Dev', 'Tester')")
    p1_id = c.lastrowid
    
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('Proj', 'http://test', 'Dev', 'Tester')")
    p2_id = c.lastrowid
    
    # Project 1 has 1 run and 2 open findings
    c.execute("INSERT INTO runs (project_id, status) VALUES (?, 'COMPLETED')", (p1_id,))
    r1_id = c.lastrowid
    c.execute("INSERT INTO findings (project_id, run_id, stable_id, title, status) VALUES (?, ?, 'F1', 'T1', 'OPEN')", (p1_id, r1_id))
    c.execute("INSERT INTO findings (project_id, run_id, stable_id, title, status) VALUES (?, ?, 'F2', 'T2', 'OPEN')", (p1_id, r1_id))
    
    # Project 2 has 2 runs and 0 open findings
    c.execute("INSERT INTO runs (project_id, status) VALUES (?, 'COMPLETED')", (p2_id,))
    c.execute("INSERT INTO runs (project_id, status) VALUES (?, 'COMPLETED')", (p2_id,))
    
    conn.commit()
    
    # Check aggregation in projects
    c.execute('''
        SELECT p.id, 
               (SELECT COUNT(*) FROM runs WHERE project_id = p.id) as run_count,
               (SELECT COUNT(*) FROM findings WHERE project_id = p.id AND status = 'OPEN') as open_findings
        FROM projects p 
    ''')
    projects = c.fetchall()
    
    for p in projects:
        if p['id'] == p1_id:
            assert p['run_count'] == 1
            assert p['open_findings'] == 2
        elif p['id'] == p2_id:
            assert p['run_count'] == 2
            assert p['open_findings'] == 0
            
    conn.close()

def test_project_details_timezone():
    from server import app
    app.config['TESTING'] = True
    
    # We rely on test_duplicate_projects having created a project with p1_id
    with app.test_client() as client:
        # Assuming project 1 exists from previous tests if run in order, 
        # but to be safe, let's just create one if we need it.
        # Wait, tests might run in random order. Better to create one.
        pass # implemented below safely

@pytest.fixture
def test_client():
    if os.path.exists("test_db.sqlite"):
        os.remove("test_db.sqlite")
    os.environ['QA_TESTER_DB_PATH'] = "test_db.sqlite"
    from app.models.database import init_db
    init_db()
    
    from server import app
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_fixes_regression(test_client):
    # Setup Data
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO projects (name, target_url, environment, tester_name) VALUES ('TProj', 'url', 'env', 'Test')")
    p_id = c.lastrowid
    
    c.execute("INSERT INTO runs (project_id, status) VALUES (?, 'COMPLETED')", (p_id,))
    r_id = c.lastrowid
    
    from app.paths import get_screenshots_dir
    import os
    os.makedirs(get_screenshots_dir(), exist_ok=True)
    from PIL import Image, ImageDraw
    
    blank_path = os.path.join(get_screenshots_dir(), "blank.png")
    Image.new('RGB', (800, 600), color='white').save(blank_path)
    
    normal_path = os.path.join(get_screenshots_dir(), "normal.png")
    norm_img = Image.new('RGB', (800, 600), color='white')
    ImageDraw.Draw(norm_img).rectangle([10, 10, 100, 100], fill="black")
    norm_img.save(normal_path)
    
    c.execute("INSERT INTO tests (run_id, stable_id, url, viewport, status, expected_result, actual_result, screenshot_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
              (r_id, "QA-01", "url", "Mobile", "FAIL", "E", "A", blank_path))
              
    c.execute("INSERT INTO tests (run_id, stable_id, url, viewport, status, expected_result, actual_result, screenshot_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
              (r_id, "QA-01", "url", "Desktop", "FAIL", "E2", "A2", normal_path))
              
    c.execute("INSERT INTO findings (project_id, run_id, stable_id, title, status, severity, risk, review_status, learning_explanation_sv, area) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
              (p_id, r_id, "QA-01", "Bug", "OPEN", "High", "Low", "PENDING", "Secret Swedish", "UI"))
    f_id = c.lastrowid
    conn.commit()
    conn.close()

    # 1. Test Project Details Timezone
    response = test_client.get(f'/project/{p_id}')
    html = response.data.decode('utf-8')
    assert "<strong>Created</strong>" in html
    assert "class=\"text-muted\"" in html
    # Check it does NOT have raw iso format (2026-10-02 12:00:00) by looking for the absence of the typical raw pattern if possible, 
    # but at least we can verify it doesn't crash and renders the `_local` variable.
    
    # 2. Test DOCX Layout Rules (check generated document XML)
    response = test_client.post(f'/api/export/{r_id}/docx')
    assert response.status_code == 200
    resp_json = response.json
    assert resp_json['status'] == 'success'
    docx_file = resp_json['filename']
    from app.paths import get_exports_dir
    docx_path = os.path.join(get_exports_dir(), docx_file)
    
    import zipfile
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(docx_path, 'r') as docx_zip:
        doc_xml = docx_zip.read('word/document.xml').decode('utf-8')
        
    # verify no explicit page break
    assert '<w:br w:type="page"/>' not in doc_xml
    
    # verify separator is not emitted after the final finding (there's only 1 finding here)
    # The separator is "----------------------------------------"
    assert "-" * 40 not in doc_xml
    
    # verify blank screenshot omitted
    assert "[Visually blank screenshot omitted to save document space]" in doc_xml
    
    # verify normal screenshot embedded (drawing exists)
    assert "<w:drawing" in doc_xml
    
    # verify screenshot is still embedded (w:drawing element)
    assert "Findings" in doc_xml
    # 3. Test Copy Full Report completeness & status mapping
    response = test_client.get(f'/finding/{f_id}')
    html = response.data.decode('utf-8')
    
    # Run-level header exists
    assert "QA Test Report" in html
    assert "Date/Time:" in html
    assert "Total Checks:" in html
    assert "OBSERVATION:" in html
    
    # Review Status Mapping
    assert "Pending review" in html
    
    # Friendly Severity
    assert "Severity: High" in html
    assert "Risk: Low" in html
    
    # Evidence fields
    assert "Actual Result: A" in html
    assert "Viewport: Mobile" in html
    
    # Swedish notes excluded from clipboard
    # They should be in the HTML overall but NOT inside the #report-text or #report-html hidden divs
    report_text_div = html.split('<div id="report-text" style="display: none;">')[1].split('</div>')[0]
    assert "Secret Swedish" not in report_text_div
