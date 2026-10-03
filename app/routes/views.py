from flask import Blueprint, render_template, request, redirect, url_for, jsonify, abort, send_from_directory
from app.models.database import get_db_connection
from datetime import datetime, timedelta, timezone
import os

views_bp = Blueprint('views', __name__)

NEEDS_REVIEW_CONDITION = "(f.review_status = 'Pending review' OR f.review_status = 'Unreviewed' OR f.review_status IS NULL OR f.review_status = 'PENDING' OR f.severity = 'Unreviewed' OR f.severity IS NULL OR f.risk = 'Unreviewed' OR f.risk IS NULL OR f.impact = '[Requires Human Review]' OR f.recommendation = '[Requires Human Review]')"

@views_bp.route('/')
def index():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT count(*) as count FROM projects")
    projects_count = c.fetchone()['count']
    
    c.execute("SELECT count(*) as count FROM runs")
    runs_count = c.fetchone()['count']
    
    c.execute("SELECT count(*) as count FROM findings WHERE status = 'OPEN'")
    open_findings = c.fetchone()['count']
    
    c.execute(f"SELECT count(*) as count FROM findings f WHERE {NEEDS_REVIEW_CONDITION}")
    needs_review = c.fetchone()['count']
    
    # Needs attention list
    c.execute(f'''
        SELECT f.*, p.name as project_name 
        FROM findings f
        JOIN projects p ON f.project_id = p.id
        WHERE {NEEDS_REVIEW_CONDITION}
        ORDER BY f.timestamp DESC LIMIT 5
    ''')
    attention_items = c.fetchall()
    
    # Recent activity
    c.execute('''
        SELECT r.id, r.start_time, p.name as project_name,
               (SELECT COUNT(*) FROM findings WHERE run_id = r.id) as finding_count
        FROM runs r
        JOIN projects p ON r.project_id = p.id
        ORDER BY r.start_time DESC LIMIT 5
    ''')
    recent_runs = c.fetchall()
    
    # Format times
    from app.utils import format_local_time
    recent_activity = []
    for r in recent_runs:
        r_dict = dict(r)
        r_dict['time_str'] = format_local_time(r['start_time'])
        recent_activity.append(r_dict)
        
    conn.close()
    return render_template('dashboard.html', 
                          projects_count=projects_count,
                          runs_count=runs_count,
                          open_findings=open_findings,
                          needs_review=needs_review,
                          attention_items=attention_items,
                          recent_activity=recent_activity,
                          active_page='home')

@views_bp.route('/projects')
def projects():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        SELECT p.*, 
               (SELECT MAX(start_time) FROM runs WHERE project_id = p.id) as last_run,
               (SELECT COUNT(*) FROM findings WHERE project_id = p.id AND status = 'OPEN') as open_findings
        FROM projects p 
        ORDER BY p.created_at DESC
    ''')
    projects = [dict(row) for row in c.fetchall()]
    conn.close()
    
    from app.utils import format_local_time
    for p in projects:
        if p['last_run']:
            p['last_run_local'] = format_local_time(p['last_run'])
        else:
            p['last_run_local'] = 'Never'
        p['created_at_local'] = format_local_time(p['created_at'])
            
    return render_template('projects.html', projects=projects, active_page='projects')

@views_bp.route('/run-test')
def run_test_step1():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM projects ORDER BY name")
    projects = c.fetchall()
    conn.close()
    return render_template('run_test_step1.html', projects=projects, active_page='run-test')

@views_bp.route('/project/<int:project_id>')
def project(project_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    project = c.fetchone()
    
    c.execute("SELECT * FROM runs WHERE project_id = ? ORDER BY start_time DESC LIMIT 10", (project_id,))
    runs = c.fetchall()
    conn.close()
    
    from app.utils import format_local_time
    project_dict = dict(project)
    project_dict['created_at_local'] = format_local_time(project['created_at'])
    
    formatted_runs = []
    for r in runs:
        r_dict = dict(r)
        r_dict['start_time_local'] = format_local_time(r['start_time'])
        formatted_runs.append(r_dict)
    
    return render_template('project.html', project=project_dict, runs=formatted_runs, active_page='projects')

@views_bp.route('/project/<int:project_id>/dry-run')
def dry_run(project_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    project = c.fetchone()
    conn.close()
    return render_template('dry_run.html', project=project, active_page='run-test')

@views_bp.route('/run/<int:run_id>')
def run_view(run_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
    run = dict(c.fetchone())
    
    c.execute("SELECT * FROM projects WHERE id = ?", (run['project_id'],))
    project = c.fetchone()
    
    c.execute("SELECT status, count(*) as count FROM tests WHERE run_id = ? GROUP BY status", (run_id,))
    stats = {row['status']: row['count'] for row in c.fetchall()}
    
    c.execute("SELECT * FROM tests WHERE run_id = ? ORDER BY timestamp DESC", (run_id,))
    tests = c.fetchall()
    
    c.execute("SELECT * FROM findings WHERE run_id = ? ORDER BY timestamp DESC", (run_id,))
    findings_rows = c.fetchall()
    conn.close()
    
    from app.utils import format_local_time
    run['start_time_local'] = format_local_time(run['start_time'])
        
    findings = []
    for f in findings_rows:
        fd = dict(f)
        fd['time_str'] = format_local_time(fd['timestamp'], '%H:%M')
        findings.append(fd)
        
    formatted_tests = []
    for t in tests:
        td = dict(t)
        td['timestamp_local'] = format_local_time(td['timestamp'])
        formatted_tests.append(td)
        
    if run['status'] == 'RUNNING':
        return render_template('running.html', run=run, project=project, tests=formatted_tests, active_page='run-test')
    else:
        return render_template('results.html', run=run, project=project, stats=stats, tests=formatted_tests, findings=findings, active_page='run-test')

@views_bp.route('/screenshots/<path:filename>')
def serve_screenshot(filename):
    from app.paths import get_screenshots_dir
    screenshot_dir = get_screenshots_dir()
    if not os.path.exists(os.path.join(screenshot_dir, filename)):
        abort(404)
    return send_from_directory(screenshot_dir, filename)

@views_bp.route('/finding/<int:finding_id>')
def finding_view(finding_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM findings WHERE id = ?", (finding_id,))
    finding = dict(c.fetchone())
    c.execute("SELECT * FROM projects WHERE id = ?", (finding['project_id'],))
    project = c.fetchone()
    
    c.execute("SELECT * FROM tests WHERE run_id = ? AND stable_id = ?", (finding['run_id'], finding['stable_id']))
    tests = c.fetchall()
    
    c.execute("SELECT * FROM runs WHERE id = ?", (finding['run_id'],))
    run = dict(c.fetchone())
    
    c.execute("SELECT status, count(*) as count FROM tests WHERE run_id = ? GROUP BY status", (finding['run_id'],))
    stats = {row['status']: row['count'] for row in c.fetchall()}
    
    c.execute("SELECT * FROM findings WHERE run_id = ? ORDER BY timestamp DESC", (finding['run_id'],))
    all_findings_rows = c.fetchall()
    
    c.execute("SELECT * FROM tests WHERE run_id = ? ORDER BY timestamp DESC", (finding['run_id'],))
    all_tests_rows = c.fetchall()
    conn.close()
    
    from app.utils import format_local_time
    finding['timestamp_local'] = format_local_time(finding['timestamp'])
    run['start_time_local'] = format_local_time(run['start_time'])
    
    all_findings = []
    for f_row in all_findings_rows:
        fd = dict(f_row)
        fd['evidence'] = []
        for t_row in all_tests_rows:
            if t_row['stable_id'] == fd['stable_id']:
                td = dict(t_row)
                td['timestamp_local'] = format_local_time(td['timestamp'])
                fd['evidence'].append(td)
        all_findings.append(fd)
        
    formatted_tests = []
    for t in tests:
        td = dict(t)
        td['timestamp_local'] = format_local_time(td['timestamp'])
        formatted_tests.append(td)
        
    return render_template('finding.html', finding=finding, project=project, tests=formatted_tests, run=run, stats=stats, all_findings=all_findings, active_page='findings')

def group_items_by_date(rows, date_field='timestamp'):
    grouped = {}
    
    for row in rows:
        r = dict(row)
        try:
            dt_utc = datetime.strptime(r[date_field], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            dt_local = dt_utc.astimezone()
        except:
            dt_local = datetime.now()
            
        date_str = dt_local.strftime('%d %B %Y')
        r['time_str'] = dt_local.strftime('%H:%M')
        
        if date_str not in grouped:
            grouped[date_str] = []
        grouped[date_str].append(r)
        
    return grouped

def group_findings_by_date(findings_rows):
    grouped = {}
    seen = {}
    
    for row in findings_rows:
        f = dict(row)
        fid = f['id']
        
        if fid in seen:
            seen[fid]['viewport_count'] += 1
            vp = f.get('viewport')
            if vp and vp not in seen[fid]['viewports']:
                seen[fid]['viewports'].append(vp)
            continue
            
        try:
            dt_utc = datetime.strptime(f['timestamp'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            dt_local = dt_utc.astimezone()
        except:
            dt_local = datetime.now()
            
        date_str = dt_local.strftime('%d %B %Y')
        f['time_str'] = dt_local.strftime('%H:%M')
        
        f['viewport_count'] = 1
        f['viewports'] = []
        vp = f.get('viewport')
        if vp:
            f['viewports'].append(vp)
            
        seen[fid] = f
        if date_str not in grouped:
            grouped[date_str] = []
        grouped[date_str].append(f)
        
    return grouped

@views_bp.route('/findings')
def findings():
    conn = get_db_connection()
    c = conn.cursor()
    
    query = '''
        SELECT f.*, p.name as project_name, t.status as result_type, t.viewport
        FROM findings f
        JOIN projects p ON f.project_id = p.id
        LEFT JOIN tests t ON f.run_id = t.run_id AND f.stable_id = t.stable_id
        WHERE 1=1
    '''
    params = []
    
    project_id = request.args.get('project_id')
    result_type = request.args.get('result_type')
    status = request.args.get('status')
    review_status = request.args.get('review_status')
    
    # Filter mapping
    filter_val = request.args.get('filter')
    if filter_val == 'needs_review':
        query += f" AND {NEEDS_REVIEW_CONDITION}"
    elif filter_val == 'open':
        query += " AND f.status = 'OPEN'"
    elif filter_val == 'retest':
        query += " AND f.status = 'RETEST'"
    elif filter_val == 'fixed':
        query += " AND f.status = 'FIXED'"
    elif filter_val == 'closed':
        query += " AND f.status = 'CLOSED'"
        
    if project_id:
        query += " AND f.project_id = ?"
        params.append(project_id)
    if result_type:
        query += " AND t.status = ?"
        params.append(result_type)
    if status and not filter_val:
        query += " AND f.status = ?"
        params.append(status)
    if review_status and not filter_val:
        query += " AND f.review_status = ?"
        params.append(review_status)
        
    query += " ORDER BY f.timestamp DESC, f.id DESC"
    
    c.execute(query, params)
    findings_rows = c.fetchall()
    
    c.execute("SELECT id, name FROM projects ORDER BY name")
    projects_list = c.fetchall()
    conn.close()
    
    grouped = group_findings_by_date(findings_rows)
    
    today_str = datetime.now().astimezone().strftime('%d %B %Y')
    yesterday_str = (datetime.now().astimezone() - timedelta(days=1)).strftime('%d %B %Y')
    
    return render_template('history.html', grouped_findings=grouped, projects_list=projects_list, project=None, today_str=today_str, yesterday_str=yesterday_str, active_page='findings')

@views_bp.route('/history')
def run_history():
    conn = get_db_connection()
    c = conn.cursor()
    
    query = '''
        SELECT r.*, p.name as project_name,
               (SELECT COUNT(*) FROM tests WHERE run_id = r.id AND status = 'PASS') as pass_count,
               (SELECT COUNT(*) FROM tests WHERE run_id = r.id AND status = 'FAIL') as fail_count,
               (SELECT COUNT(*) FROM tests WHERE run_id = r.id AND status = 'OBSERVATION') as obs_count,
               (SELECT COUNT(*) FROM findings WHERE run_id = r.id) as finding_count
        FROM runs r
        JOIN projects p ON r.project_id = p.id
        WHERE 1=1
    '''
    params = []
    
    project_id = request.args.get('project_id')
    if project_id:
        query += " AND r.project_id = ?"
        params.append(project_id)
        
    query += " ORDER BY r.start_time DESC"
    
    c.execute(query, params)
    runs = c.fetchall()
    conn.close()
    
    grouped = group_items_by_date(runs, 'start_time')
    
    today_str = datetime.now().astimezone().strftime('%d %B %Y')
    yesterday_str = (datetime.now().astimezone() - timedelta(days=1)).strftime('%d %B %Y')
    
    return render_template('run_history.html', grouped_runs=grouped, today_str=today_str, yesterday_str=yesterday_str, active_page='history')

@views_bp.route('/reports')
def reports():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        SELECT r.*, p.name as project_name,
               (SELECT COUNT(*) FROM findings WHERE run_id = r.id) as finding_count
        FROM runs r
        JOIN projects p ON r.project_id = p.id
        ORDER BY r.start_time DESC
    ''')
    runs = [dict(row) for row in c.fetchall()]
    conn.close()
    
    for r in runs:
        try:
            dt_utc = datetime.strptime(r['start_time'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            r['start_time_local'] = dt_utc.astimezone().strftime('%d %b %Y %H:%M')
        except:
            r['start_time_local'] = r['start_time']
            
    return render_template('reports.html', runs=runs, active_page='reports')
