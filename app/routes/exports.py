from flask import Blueprint, request, Response, send_file, abort
from app.models.database import get_db_connection
from app.paths import get_exports_dir, get_screenshots_dir
from datetime import datetime, timezone
import os
try:
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor
    from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

exports_bp = Blueprint('exports', __name__)

def get_report_data(run_id):
    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
    run = dict(c.fetchone())
    
    c.execute("SELECT * FROM projects WHERE id = ?", (run['project_id'],))
    project = dict(c.fetchone())
    
    c.execute("SELECT * FROM findings WHERE run_id = ? ORDER BY timestamp DESC", (run_id,))
    findings = [dict(f) for f in c.fetchall()]
    
    c.execute("SELECT * FROM tests WHERE run_id = ? ORDER BY timestamp DESC", (run_id,))
    tests = [dict(t) for t in c.fetchall()]
    
    c.execute("SELECT status, count(*) as count FROM tests WHERE run_id = ? GROUP BY status", (run_id,))
    stats = {row['status']: row['count'] for row in c.fetchall()}
    
    conn.close()
    
    # Time formatting
    from app.utils import format_local_time, display_review_status, display_severity_risk
    run['start_time_local'] = format_local_time(run['start_time'])
    
    for f in findings:
        f['evidence'] = []
        for t in tests:
            if t['stable_id'] == f['stable_id']:
                t_dict = dict(t)
                t_dict['timestamp_local'] = format_local_time(t_dict['timestamp'])
                f['evidence'].append(t_dict)
        
    return run, project, findings, tests, stats

@exports_bp.route('/export/<int:run_id>/html')
def export_html(run_id):
    run, project, findings, tests, stats = get_report_data(run_id)
    
    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>QA Test Report - {project['name']} Run #{run_id}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; padding: 40px; max-width: 1000px; margin: auto; line-height: 1.6; color: #333; background: #f4f6f9; }}
        .page {{ background: white; padding: 40px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.05); margin-bottom: 30px; }}
        h1, h2, h3, h4 {{ color: #2c3e50; margin-top: 0; }}
        h1 {{ border-bottom: 2px solid #eee; padding-bottom: 10px; margin-bottom: 20px; }}
        .meta-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 30px; }}
        .meta-card {{ background: #f8f9fa; padding: 20px; border-radius: 6px; border: 1px solid #e9ecef; }}
        .meta-card p {{ margin: 5px 0; }}
        .finding-card {{ border: 1px solid #e9ecef; padding: 25px; margin-bottom: 30px; border-radius: 6px; page-break-inside: avoid; }}
        .finding-header {{ border-bottom: 1px solid #eee; padding-bottom: 15px; margin-bottom: 15px; }}
        .evidence-box {{ background: #f8f9fa; padding: 20px; margin-top: 20px; border-radius: 6px; border-left: 4px solid #adb5bd; }}
        .screenshot {{ max-width: 100%; height: auto; max-height: 500px; object-fit: contain; border: 1px solid #dee2e6; margin-top: 15px; display: block; background: #fff; padding: 5px; }}
        .badge {{ display: inline-block; padding: 4px 8px; border-radius: 4px; font-weight: bold; font-size: 0.85rem; margin-right: 5px; }}
        .bg-danger {{ background: #f8d7da; color: #721c24; }}
        .bg-warning {{ background: #fff3cd; color: #856404; }}
        .bg-info {{ background: #d1ecf1; color: #0c5460; }}
        .bg-success {{ background: #d4edda; color: #155724; }}
        .bg-secondary {{ background: #e2e3e5; color: #383d41; }}
        .note-box {{ background: #fffdf5; padding: 10px; border-left: 4px solid #ffc107; margin-bottom: 15px; font-size: 0.9em; }}
    </style>
</head>
<body>
    <div class="page">
        <h1>QA Test Report</h1>
        
        <div class="meta-grid">
            <div class="meta-card">
                <h3 style="margin-bottom: 15px;">Project Details</h3>
                <p><strong>Project:</strong> {project['name']}</p>
                <p><strong>Website:</strong> {project['target_url']}</p>
                <p><strong>Environment:</strong> {project['environment']}</p>
                <p><strong>Tester:</strong> {project['tester_name']}</p>
            </div>
            <div class="meta-card">
                <h3 style="margin-bottom: 15px;">Test Run Details</h3>
                <p><strong>Run ID:</strong> #{run['id']}</p>
                <p><strong>Date/Time:</strong> {run['start_time_local']}</p>
                <p><strong>Total Checks:</strong> {len(tests)}</p>
                <p>
                    <span style="color: #28a745; font-weight: bold;">PASS: {stats.get('PASS', 0)}</span> | 
                    <span style="color: #dc3545; font-weight: bold;">FAIL: {stats.get('FAIL', 0)}</span> | 
                    <span style="color: #ffc107; font-weight: bold;">OBSERVATION: {stats.get('OBSERVATION', 0)}</span>
                </p>
            </div>
        </div>
        
        <h2>Findings Summary</h2>
"""
    if not findings:
        html += "<p>No issues found during this run.</p>"
    else:
        from app.utils import display_review_status, display_severity_risk
        html += """
        <table style="width: 100%; border-collapse: collapse; margin-bottom: 30px;">
            <tr style="background-color: #f8f9fa; text-align: left; border-bottom: 2px solid #dee2e6;">
                <th style="padding: 12px; border: 1px solid #dee2e6;">Bug ID</th>
                <th style="padding: 12px; border: 1px solid #dee2e6;">Finding</th>
                <th style="padding: 12px; border: 1px solid #dee2e6;">Severity</th>
                <th style="padding: 12px; border: 1px solid #dee2e6;">Risk</th>
                <th style="padding: 12px; border: 1px solid #dee2e6;">Confidence</th>
                <th style="padding: 12px; border: 1px solid #dee2e6;">Bug Status</th>
                <th style="padding: 12px; border: 1px solid #dee2e6;">Review Status</th>
            </tr>
"""
        for f in findings:
            html += f"""
            <tr>
                <td style="padding: 12px; border: 1px solid #dee2e6;">{f['stable_id']}</td>
                <td style="padding: 12px; border: 1px solid #dee2e6;">{f['title']}</td>
                <td style="padding: 12px; border: 1px solid #dee2e6;">{display_severity_risk(f['severity'])}</td>
                <td style="padding: 12px; border: 1px solid #dee2e6;">{display_severity_risk(f['risk'])}</td>
                <td style="padding: 12px; border: 1px solid #dee2e6;">{display_severity_risk(f['confidence'])}</td>
                <td style="padding: 12px; border: 1px solid #dee2e6;">{f['status']}</td>
                <td style="padding: 12px; border: 1px solid #dee2e6;">{display_review_status(f['review_status'])}</td>
            </tr>
"""
        html += "        </table>"

    html += """
        <h2>Detailed Findings</h2>
"""
    if not findings:
        pass
        
    for f in findings:
        rev = display_review_status(f['review_status'])
        
        f['severity'] = display_severity_risk(f['severity'])
        f['risk'] = display_severity_risk(f['risk'])
        
        sev_class = "bg-danger" if f['severity'] == 'High' else "bg-warning" if f['severity'] == 'Medium' else "bg-info" if f['severity'] == 'Low' else "bg-secondary"
        risk_class = "bg-danger" if f['risk'] == 'High' else "bg-warning" if f['risk'] == 'Medium' else "bg-info" if f['risk'] == 'Low' else "bg-secondary"
        
        html += f"""
        <div class="finding-card">
            <div class="finding-header">
                <h3>{f['stable_id']} - {f['title']}</h3>
                <div style="margin-top: 10px;">
                    <span class="badge { 'bg-danger' if f['status'] == 'OPEN' else 'bg-secondary' }">Status: {f['status']}</span>
                    <span class="badge {sev_class}">Severity: {f['severity']}</span>
                    <span class="badge {risk_class}">Risk: {f['risk']}</span>
                    <span class="badge bg-secondary">Confidence: {display_severity_risk(f['confidence'])}</span>
                    <span class="badge { 'bg-success' if rev == 'APPROVED' else 'bg-secondary' }">Review: {rev}</span>
                </div>
            </div>
            
            <h4>Description</h4>
            <p style="white-space: pre-wrap; margin-bottom: 20px;">{f['description']}</p>
            
            <h4>Explanation</h4>
            <p style="white-space: pre-wrap; margin-bottom: 20px;">{f['plain_language_explanation']}</p>
            
            <h4>Impact</h4>
            <p style="white-space: pre-wrap; margin-bottom: 20px;">{f['impact']}</p>
            
            <h4>Recommendation</h4>
            <p style="white-space: pre-wrap; margin-bottom: 20px;">{f['recommendation']}</p>
            
            <h4>Evidence</h4>
"""
        for e in f['evidence']:
            html += f"""
            <div class="evidence-box">
                <p style="margin: 0 0 10px 0;"><strong>Viewport:</strong> {e['viewport']} | <strong>URL:</strong> <a href="{e['url']}">{e['url']}</a></p>
                <p style="margin: 5px 0;"><strong>Expected:</strong> {e['expected_result']}</p>
                <p style="margin: 5px 0;"><strong>Actual:</strong> {e['actual_result']}</p>
"""
            if e['screenshot_path']:
                filename = e['screenshot_path'].split('\\')[-1].split('/')[-1]
                
                if e['actual_result'] and "net::ERR_CONNECTION_REFUSED" in e['actual_result']:
                    html += '<div class="note-box"><strong>Note:</strong> The website did not load, so the captured browser view may appear blank.</div>'
                    
                screenshot_full_path = os.path.join(get_screenshots_dir(), filename)
                is_blank = False
                if os.path.exists(screenshot_full_path):
                    try:
                        from PIL import Image
                        with Image.open(screenshot_full_path) as img:
                            gray = img.convert("L")
                            min_val, max_val = gray.getextrema()
                        if max_val - min_val < 5:
                            is_blank = True
                    except Exception:
                        pass
                
                if is_blank:
                    html += '<p><em>Visually blank screenshot omitted.</em></p>'
                else:
                    html += f'<img src="../../screenshots/{filename}" class="screenshot" alt="Screenshot">'
            html += "</div>"
            
        html += "</div>"
        
    html += """
    </div>
</body>
</html>"""
    
    filepath = os.path.join(get_exports_dir(), f"report_{run_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.html")
    with open(filepath, 'w', encoding='utf-8') as file:
        file.write(html)
        
    # We output the html but rewrite the image source for the browser display version so it works immediately
    display_html = html.replace('../../screenshots/', '/screenshots/')
    return Response(display_html, mimetype='text/html')

@exports_bp.route('/api/export/<int:run_id>/docx', methods=['POST'])
def export_docx(run_id):
    if not DOCX_AVAILABLE:
        return {"status": "error", "message": "python-docx is not installed. Document Export unavailable."}, 500
        
    run, project, findings, tests, stats = get_report_data(run_id)
    
    doc = Document()
    
    # Title
    title = doc.add_heading('QA Test Report', 0)
    title.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
    
    # Metadata
    doc.add_heading('Project Details', level=1)
    doc.add_paragraph(f"Project: {project['name']}")
    doc.add_paragraph(f"Website: {project['target_url']}")
    doc.add_paragraph(f"Environment: {project['environment']}")
    doc.add_paragraph(f"Tester: {project['tester_name']}")
    doc.add_paragraph(f"Date/Time: {run['start_time_local']}")
    doc.add_paragraph(f"Run ID: #{run['id']}")
    
    # Summary
    doc.add_heading('Summary', level=1)
    p = doc.add_paragraph(f"Total Checks: {len(tests)}\n")
    p.add_run(f"PASS: {stats.get('PASS', 0)}\n").font.color.rgb = RGBColor(0x28, 0xa7, 0x45)
    p.add_run(f"FAIL: {stats.get('FAIL', 0)}\n").font.color.rgb = RGBColor(0xdc, 0x35, 0x45)
    p.add_run(f"OBSERVATION: {stats.get('OBSERVATION', 0)}").font.color.rgb = RGBColor(0xff, 0xc1, 0x07)
    
    doc.add_heading('Findings Summary', level=1)
    
    if not findings:
        doc.add_paragraph("No issues found during this run.")
    else:
        from app.utils import display_review_status, display_severity_risk
        table = doc.add_table(rows=1, cols=7)
        table.style = 'Table Grid'
        hdr_cells = table.rows[0].cells
        hdr_cells[0].text = 'Bug ID'
        hdr_cells[1].text = 'Finding'
        hdr_cells[2].text = 'Severity'
        hdr_cells[3].text = 'Risk'
        hdr_cells[4].text = 'Confidence'
        hdr_cells[5].text = 'Bug Status'
        hdr_cells[6].text = 'Review Status'
        
        for f in findings:
            row_cells = table.add_row().cells
            row_cells[0].text = str(f['stable_id'])
            row_cells[1].text = str(f['title'])
            row_cells[2].text = str(display_severity_risk(f['severity']))
            row_cells[3].text = str(display_severity_risk(f['risk']))
            row_cells[4].text = str(display_severity_risk(f['confidence']))
            row_cells[5].text = str(f['status'])
            row_cells[6].text = str(display_review_status(f['review_status']))
            
    doc.add_heading('Detailed Findings', level=1)
        
    from app.utils import display_review_status, display_severity_risk
    for f in findings:
        rev = display_review_status(f['review_status'])
        f_severity = display_severity_risk(f['severity'])
        f_risk = display_severity_risk(f['risk'])
        f_confidence = display_severity_risk(f['confidence'])
        
        h2 = doc.add_heading(f"{f['stable_id']} - {f['title']}", level=2)
        
        status_p = doc.add_paragraph()
        status_p.add_run(f"Status: {f['status']} | Severity: {f_severity} | Risk: {f_risk} | Confidence: {f_confidence} | Review: {rev}")
        
        doc.add_heading('Description', level=3)
        doc.add_paragraph(f['description'])
        
        doc.add_heading('Explanation', level=3)
        doc.add_paragraph(f['plain_language_explanation'])
        
        doc.add_heading('Impact', level=3)
        doc.add_paragraph(f['impact'])
        
        doc.add_heading('Recommendation', level=3)
        doc.add_paragraph(f['recommendation'])
        
        doc.add_heading('Evidence', level=3)
        for e in f['evidence']:
            ep = doc.add_paragraph()
            ep.add_run(f"Viewport: {e['viewport']} | URL: {e['url']}\n").bold = True
            ep.add_run(f"Expected: {e['expected_result']}\n")
            ep.add_run(f"Actual: {e['actual_result']}")
            
            if e['screenshot_path']:
                filename = e['screenshot_path'].split('\\')[-1].split('/')[-1]
                screenshot_full_path = os.path.join(get_screenshots_dir(), filename)
                
                if e['actual_result'] and "net::ERR_CONNECTION_REFUSED" in e['actual_result']:
                    doc.add_paragraph("Note: The website did not load, so the captured browser view may appear blank.")
                    
                if os.path.exists(screenshot_full_path):
                    try:
                        from PIL import Image
                        with Image.open(screenshot_full_path) as img:
                            w_px, h_px = img.size
                            gray = img.convert("L")
                            min_val, max_val = gray.getextrema()
                            
                        # If the image is highly uniform (e.g., solid white/black)
                        if max_val - min_val < 5:
                            p = doc.add_paragraph()
                            p.add_run(f"[Visually blank screenshot omitted to save document space]").italic = True
                        else:
                            ratio = w_px / h_px
                            
                            target_w = 6.0
                            target_h = target_w / ratio
                            
                            if target_h > 7.0:
                                target_h = 7.0
                                target_w = target_h * ratio
                                
                            doc.add_picture(screenshot_full_path, width=Inches(target_w), height=Inches(target_h))
                    except Exception:
                        doc.add_paragraph("[Screenshot image could not be embedded]")
                else:
                    doc.add_paragraph(f"[Screenshot not found: {filename}]")
                    
        if f != findings[-1]:
            doc.add_paragraph("-" * 40)
        
    filepath = os.path.join(get_exports_dir(), f"report_{run_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.docx")
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    doc.save(filepath)
    
    return {"status": "success", "filename": os.path.basename(filepath)}


@exports_bp.route('/api/export/<int:run_id>/markdown', methods=['POST'])
def export_md(run_id):
    run, project, findings, tests, stats = get_report_data(run_id)
    
    md = f"""# QA Test Report

**Project:** {project['name']}  
**Website:** {project['target_url']}  
**Environment:** {project['environment']}  
**Tester:** {project['tester_name']}  
**Date/Time:** {run['start_time_local']}  
**Run ID:** {run['id']}  

## Summary
- **Total Checks:** {len(tests)}
- **PASS:** {stats.get('PASS', 0)}
- **FAIL:** {stats.get('FAIL', 0)}
- **OBSERVATION:** {stats.get('OBSERVATION', 0)}

## Findings Summary
"""
    if findings:
        from app.utils import display_review_status, display_severity_risk
        md += "| Bug ID | Finding | Severity | Risk | Confidence | Bug Status | Review Status |\n"
        md += "|---|---|---|---|---|---|---|\n"
        for f in findings:
            md += f"| {f['stable_id']} | {f['title']} | {display_severity_risk(f['severity'])} | {display_severity_risk(f['risk'])} | {display_severity_risk(f['confidence'])} | {f['status']} | {display_review_status(f['review_status'])} |\n"
            
    md += "\n## Detailed Findings\n"
    if not findings:
        md += "No issues found.\n"
        
    from app.utils import display_review_status, display_severity_risk
    for f in findings:
        rev = display_review_status(f['review_status'])
        f_severity = display_severity_risk(f['severity'])
        f_risk = display_severity_risk(f['risk'])
        md += f"""
### {f['stable_id']} - {f['title']}
- **Status:** {f['status']}
- **Severity:** {f_severity}
- **Risk:** {f_risk}
- **Confidence:** {display_severity_risk(f['confidence'])}
- **Review Status:** {rev}
- **Area:** {f['area']}

**Description:**
{f['description']}

**Plain-Language Explanation:**
{f['plain_language_explanation']}

**Impact:**
{f['impact']}

**Recommendation:**
{f['recommendation']}

**Evidence:**
"""
        for e in f['evidence']:
            ss = f"Screenshot: {e['screenshot_path'].split('/')[-1].split(chr(92))[-1]}" if e['screenshot_path'] else "Screenshot: None"
            md += f"""
---
- **Viewport:** {e['viewport']}
- **URL:** {e['url']}
- **Expected:** {e['expected_result']}
- **Actual:** {e['actual_result']}
- {ss}
"""
        md += "\n"
        
    filepath = os.path.join(get_exports_dir(), f"report_{run_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.md")
    with open(filepath, 'w', encoding='utf-8') as file:
        file.write(md)
        
    return {"status": "success", "filename": os.path.basename(filepath)}


@exports_bp.route('/api/export/<int:run_id>/txt', methods=['POST'])
def export_txt(run_id):
    run, project, findings, tests, stats = get_report_data(run_id)
    
    txt = f"""QA TEST REPORT

Project: {project['name']}
Website: {project['target_url']}
Environment: {project['environment']}
Tester: {project['tester_name']}
Date/Time: {run['start_time_local']}
Run ID: {run['id']}

SUMMARY
Total Checks: {len(tests)}
PASS: {stats.get('PASS', 0)}
FAIL: {stats.get('FAIL', 0)}
OBSERVATION: {stats.get('OBSERVATION', 0)}

==================================================
FINDINGS SUMMARY
==================================================
"""
    if findings:
        from app.utils import display_review_status, display_severity_risk
        for f in findings:
            txt += f"[{f['stable_id']}] {f['title']} (Sev: {display_severity_risk(f['severity'])}, Risk: {display_severity_risk(f['risk'])}, Conf: {display_severity_risk(f['confidence'])}, Status: {f['status']}, Review: {display_review_status(f['review_status'])})\n"
            
    txt += """
==================================================
DETAILED FINDINGS
==================================================
"""
    if not findings:
        txt += "No issues found.\n"
        
    from app.utils import display_review_status, display_severity_risk
    for f in findings:
        rev = display_review_status(f['review_status'])
        f_severity = display_severity_risk(f['severity'])
        f_risk = display_severity_risk(f['risk'])
        txt += f"""
Bug ID / Title: {f['stable_id']} - {f['title']}
Status: {f['status']}
Severity: {f_severity}
Risk: {f_risk}
Confidence: {display_severity_risk(f['confidence'])}
Review Status: {rev}
Area: {f['area']}

Description:
{f['description']}

Plain-Language Explanation:
{f['plain_language_explanation']}

Impact:
{f['impact']}

Recommendation:
{f['recommendation']}

Evidence:
"""
        for e in f['evidence']:
            ss = f"Screenshot: {e['screenshot_path'].split('/')[-1].split(chr(92))[-1]}" if e['screenshot_path'] else "Screenshot: None"
            txt += f"""---
Viewport: {e['viewport']}
URL: {e['url']}
Expected: {e['expected_result']}
Actual: {e['actual_result']}
{ss}
"""
        txt += "\n--------------------------------------------------\n"
        
    filepath = os.path.join(get_exports_dir(), f"report_{run_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.txt")
    with open(filepath, 'w', encoding='utf-8') as file:
        file.write(txt)
        
    return {"status": "success", "filename": os.path.basename(filepath)}
