from flask import Blueprint, request, jsonify, send_from_directory
from app.models.database import get_db_connection
from app.qa.engine import SmartSmokeQA
from app.qa.explanations import get_explanations
from app.qa.assessment import assess_finding
import asyncio
import threading
import os

api_bp = Blueprint('api', __name__)

# In-memory dictionary to hold running engines so we can stop them
running_engines = {}

def run_qa_async_in_thread(target_url, project_id, run_id):
    # Set up a new event loop for this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    engine = SmartSmokeQA(target_url, project_id, run_id)
    running_engines[run_id] = engine
    results = loop.run_until_complete(engine.run())
    
    # Save results to DB
    conn = get_db_connection()
    c = conn.cursor()
    
    passes = 0
    fails = 0
    obs = 0
    
    created_findings = set()
    
    for r in results:
        c.execute('''
            INSERT INTO tests (run_id, url, expected_result, actual_result, status, viewport, screenshot_path, stable_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (run_id, r['url'], r['expected_result'], r['actual_result'], r['status'], r['viewport'], r['screenshot'], r['finding_id']))
        
        if r['status'] == 'PASS': passes += 1
        elif r['status'] == 'FAIL': fails += 1
        elif r['status'] == 'OBSERVATION': obs += 1
        
        # Create structured finding if FAIL or OBSERVATION and it hasn't been created yet
        if r['status'] in ['FAIL', 'OBSERVATION'] and r['finding_id']:
            if r['finding_id'] not in created_findings:
                title = r.get('title', f"{r['status']} on {r['url']}")
                area = r.get('area', 'Unknown')
                
                assessment = assess_finding(title, r['actual_result'], area, r['viewport'])
                severity = assessment['severity']
                risk = assessment['risk']
                confidence = assessment['confidence']
                draft_impact = assessment['impact']
                draft_rec = assessment['recommendation']
                
                expl_en, expl_sv_learn, expl_sv_verb = get_explanations(actual_result=r['actual_result'])
                
                c.execute('''
                    INSERT INTO findings (stable_id, project_id, run_id, title, area, severity, risk, confidence, description, impact, recommendation, plain_language_explanation, learning_explanation_sv, how_to_explain_sv)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (r['finding_id'], project_id, run_id, title, area, severity, risk, confidence, r['actual_result'], draft_impact, draft_rec, expl_en, expl_sv_learn, expl_sv_verb))
                
                created_findings.add(r['finding_id'])
            
    c.execute("UPDATE runs SET status = 'COMPLETED', total_tests = ? WHERE id = ?", (len(results), run_id))
    conn.commit()
    conn.close()
    
    if run_id in running_engines:
        del running_engines[run_id]

@api_bp.route('/api/projects', methods=['POST'])
def create_project():
    data = request.json
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT INTO projects (name, target_url, environment, tester_name)
        VALUES (?, ?, ?, ?)
    ''', (data['name'], data['target_url'], data['environment'], data['tester_name']))
    project_id = c.lastrowid
    conn.commit()
    conn.close()
    return jsonify({"id": project_id, "status": "success"})

@api_bp.route('/api/runs', methods=['POST'])
def start_run():
    data = request.json
    project_id = data['project_id']
    
    conn = get_db_connection()
    c = conn.cursor()
    
    # Get project url
    c.execute("SELECT target_url FROM projects WHERE id = ?", (project_id,))
    row = c.fetchone()
    if not row:
        return jsonify({"error": "Project not found"}), 404
        
    target_url = row['target_url']
    
    c.execute('''
        INSERT INTO runs (project_id, status, authorized_at)
        VALUES (?, 'RUNNING', CURRENT_TIMESTAMP)
    ''', (project_id,))
    run_id = c.lastrowid
    conn.commit()
    conn.close()
    
    # Run in background thread
    t = threading.Thread(target=run_qa_async_in_thread, args=(target_url, project_id, run_id))
    t.start()
    
    return jsonify({"run_id": run_id, "status": "started"})

@api_bp.route('/api/runs/<int:run_id>/status')
def get_run_status(run_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT status FROM runs WHERE id = ?", (run_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return jsonify({"status": row['status']})
    return jsonify({"status": "not_found"}), 404

@api_bp.route('/api/runs/<int:run_id>/stop', methods=['POST'])
def stop_run(run_id):
    if run_id in running_engines:
        running_engines[run_id].stop()
        return jsonify({"status": "stopping"})
    return jsonify({"status": "not_running"})

@api_bp.route('/api/findings/<int:finding_id>/edit', methods=['POST'])
def update_finding(finding_id):
    data = request.json
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        UPDATE findings 
        SET severity = ?, risk = ?, confidence = ?, impact = ?, recommendation = ?, status = ?,
            plain_language_explanation = ?, learning_explanation_sv = ?, how_to_explain_sv = ?
        WHERE id = ?
    ''', (data.get('severity'), data.get('risk'), data.get('confidence'), data.get('impact'), data.get('recommendation'), data.get('status'),
          data.get('plain_language_explanation'), data.get('learning_explanation_sv'), data.get('how_to_explain_sv'), finding_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@api_bp.route('/api/findings/<int:finding_id>/review', methods=['POST'])
def update_review_status(finding_id):
    data = request.json
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('UPDATE findings SET review_status = ? WHERE id = ?', (data.get('review_status'), finding_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@api_bp.route('/api/screenshots/<path:filename>/open', methods=['POST'])
def open_screenshot(filename):
    from app.paths import get_screenshots_dir
    screenshot_dir = get_screenshots_dir()
    filepath = os.path.join(screenshot_dir, filename)
    if os.path.exists(filepath):
        try:
            if os.name == 'nt':
                os.startfile(filepath)
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})
    return jsonify({"status": "error", "message": "File not found"}), 404

@api_bp.route('/api/exports/<path:filename>/open', methods=['POST'])
def open_export(filename):
    from app.paths import get_exports_dir
    export_dir = get_exports_dir()
    filepath = os.path.join(export_dir, filename)
    if os.path.exists(filepath):
        try:
            if os.name == 'nt':
                os.startfile(filepath)
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})
    return jsonify({"status": "error", "message": "File not found"}), 404

@api_bp.route('/api/open-exports-folder', methods=['POST'])
def open_exports_folder():
    from app.paths import get_exports_dir
    exports_dir = get_exports_dir()
    if os.path.exists(exports_dir):
        try:
            if os.name == 'nt':
                os.startfile(exports_dir)
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})
    return jsonify({"status": "error", "message": "Folder not found"}), 404
