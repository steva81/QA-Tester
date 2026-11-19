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
    from app.utils import format_local_time
    
    # 1. Look for the most recent run for "Continue where you left off"
    c.execute('''
        SELECT r.id, r.project_id, r.status, r.start_time, p.name as project_name,
               (SELECT COUNT(*) FROM findings WHERE run_id = r.id) as finding_count
        FROM runs r
        JOIN projects p ON r.project_id = p.id
        ORDER BY r.start_time DESC, r.id DESC
        LIMIT 1
    ''')
    latest_run = c.fetchone()
    
    recent_activity = None
    if latest_run:
        r_dict = dict(latest_run)
        time_str = format_local_time(r_dict['start_time'])
        if r_dict['status'] == 'RUNNING':
            status_text = 'In progress'
            action_label = 'Resume run'
            badge_class = 'badge-info'
            description = f"Run #{r_dict['id']} is currently running · Started {time_str}"
        else:
            status_text = r_dict['status']
            action_label = 'View results'
            badge_class = 'badge-success' if r_dict['status'] == 'COMPLETED' else ('badge-danger' if r_dict['status'] == 'FAILED' else 'badge-warning')
            count = r_dict['finding_count']
            count_str = f"{count} findings" if count != 1 else "1 finding"
            description = f"Run #{r_dict['id']} · {count_str} · {time_str}"
            
        recent_activity = {
            'type': 'run',
            'id': r_dict['id'],
            'project_name': r_dict['project_name'],
            'project_id': r_dict['project_id'],
            'status': status_text,
            'badge_class': badge_class,
            'description': description,
            'destination': f"/run/{r_dict['id']}",
            'action_label': action_label
        }
    else:
        # No runs exist. Check if a project exists.
        c.execute('''
            SELECT id, name, environment, created_at
            FROM projects
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        ''')
        latest_project = c.fetchone()
        if latest_project:
            p_dict = dict(latest_project)
            time_str = format_local_time(p_dict['created_at'])
            recent_activity = {
                'type': 'project',
                'id': p_dict['id'],
                'project_name': p_dict['name'],
                'project_id': p_dict['id'],
                'status': 'Ready',
                'badge_class': 'badge-neutral',
                'description': f"Project created {time_str} · Environment: {p_dict['environment']}",
                'destination': f"/project/{p_dict['id']}",
                'action_label': 'Open project'
            }
            
    c.execute("SELECT COUNT(*) FROM projects")
    has_projects = (c.fetchone()[0] > 0)
    
    conn.close()
    return render_template('dashboard.html',
                           recent_activity=recent_activity,
                           has_projects=has_projects,
                           active_page='home')


@views_bp.route('/projects')
def projects():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        SELECT p.*, 
               (SELECT id FROM runs WHERE project_id=p.id AND status='COMPLETED' ORDER BY start_time DESC, id DESC LIMIT 1) as latest_completed_id,
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
    
    c.execute("SELECT * FROM runs WHERE project_id = ? ORDER BY start_time DESC, id DESC", (project_id,))
    runs = c.fetchall()

    from app.qa.scoped_mutation import ScopedMutationRegistry
    pending_resources = ScopedMutationRegistry.get_pending_records_for_display(project_id=project_id)

    c.execute("SELECT * FROM validation_contracts WHERE project_id = ? ORDER BY id DESC", (project_id,))
    contracts_rows = c.fetchall()
    from app.routes.api import inspect_stored_contract_row
    contracts = []
    for cr in contracts_rows:
        item = dict(cr)
        info = inspect_stored_contract_row(cr)
        info.pop("vcontract", None)
        item.update(info)
        contracts.append(item)
    conn.close()
    if project is None:
        abort(404)
    
    from app.utils import format_local_time
    project_dict = dict(project)
    project_dict['created_at_local'] = format_local_time(project['created_at'])
    
    formatted_runs = []
    for r in runs:
        r_dict = dict(r)
        r_dict['start_time_local'] = format_local_time(r['start_time'])
        formatted_runs.append(r_dict)
    
    latest_completed = next((r for r in formatted_runs if r['status'] == 'COMPLETED'), None)
    return render_template('project.html', project=project_dict, runs=formatted_runs,
                           latest_completed=latest_completed, pending_resources=pending_resources,
                           contracts=contracts, active_page='projects')

@views_bp.route('/project/<int:project_id>/dry-run')
def dry_run(project_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    project = c.fetchone()
    conn.close()
    if project is None:
        abort(404)
    return render_template('dry_run.html', project=project, active_page='run-test')

@views_bp.route('/run/<int:run_id>')
def run_view(run_id):
    from app.routes.exports import get_report
    from app.qa.report_renderers import render_text, render_html
    from app.utils import format_local_time
    report = get_report(run_id)
    run = report['run']
    tests = [dict(t, timestamp_local=format_local_time(t['timestamp'])) for t in report['tests']]
    if run['status'] == 'RUNNING':
        return render_template('running.html', run=run, project=report['project'], tests=tests, active_page='run-test')
    run_tab = request.args.get('tab', 'summary')
    if run_tab not in ('summary', 'findings', 'reports', 'technical'):
        run_tab = 'summary'
    # Presentation groups retain every original record and its order within a
    # URL/viewport diagnostic group. Customer findings and counts are unchanged.
    technical_groups = []
    diagnostic_groups = {}
    for test in tests:
        if test['status'] == 'DIAGNOSTIC':
            key = (test['url'], test['viewport'])
            if key not in diagnostic_groups:
                diagnostic_groups[key] = []
                technical_groups.append(diagnostic_groups[key])
            diagnostic_groups[key].append(test)
        else:
            technical_groups.append([test])
    return render_template('results.html', run=run, project=report['project'], stats=report['stats'],
                           tests=tests, findings=report['findings'], coverage=report['coverage'],
                           triage=report['triage'], report=report,
                           run_tab=run_tab,
                           technical_groups=technical_groups,
                           report_text=render_text(report), report_full_text=render_text(report, 'full'),
                           report_html=render_html(report, document=False) if run_tab == 'reports' else '',
                           report_full_html=render_html(report, 'full', document=False) if run_tab == 'reports' else '',
                           active_page='run-test')

@views_bp.route('/run/<int:run_id>/preview')
def report_preview(run_id):
    """Preview the canonical report without creating an export file."""
    from app.routes.exports import get_report
    from app.qa.report_renderers import render_html
    mode = request.args.get('mode', 'standard')
    if mode not in ('standard', 'full'):
        abort(400)
    return render_html(get_report(run_id), mode)

@views_bp.route('/screenshots/<path:filename>')
def serve_screenshot(filename):
    from app.paths import get_screenshots_dir
    screenshot_dir = get_screenshots_dir()
    if not os.path.exists(os.path.join(screenshot_dir, filename)):
        abort(404)
    return send_from_directory(screenshot_dir, filename)

@views_bp.route('/finding/<int:finding_id>')
def finding_view(finding_id):
    # The private editor is a separate read path; its dictionary is never
    # passed to the reporting model, triage, renderers or clipboard blocks.
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT * FROM findings WHERE id=?', (finding_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        abort(404)
    finding = dict(row)
    from app.routes.exports import get_report
    from app.qa.report_renderers import render_text, render_html
    from app.utils import format_local_time
    report = get_report(finding['run_id'])
    triage = next((item for item in report['triage'] if item['finding_id'] == finding_id), None)
    finding['timestamp_local'] = format_local_time(finding['timestamp'])
    tests = [dict(t, timestamp_local=format_local_time(t['timestamp'])) for t in report['tests']
             if t.get('stable_id') == finding['stable_id']]
    return render_template('finding.html', finding=finding, project=report['project'], tests=tests,
                           run=report['run'], stats=report['stats'], all_findings=report['findings'],
                           coverage=report['coverage'], technical_tests=report['tests'],
                           total_checks=report['total_checks'], triage=triage, report=report,
                           report_text=render_text(report), report_html=render_html(report, document=False),
                           report_full_text=render_text(report, 'full'),
                           report_full_html=render_html(report, 'full', document=False), active_page='findings')


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
        ORDER BY r.start_time DESC, r.id DESC
    ''')
    runs = [dict(row) for row in c.fetchall()]
    conn.close()
    
    from app.utils import format_local_time
    for r in runs:
        r['start_time_local'] = format_local_time(r['start_time'])
            
    return render_template('reports.html', runs=runs, active_page='reports')


@views_bp.route('/historical-resources')
def historical_resources():
    from app.qa.scoped_mutation import ScopedMutationRegistry
    state = request.args.get('state', 'pending')
    if state not in ('pending', 'all'):
        state = 'pending'
    records = ScopedMutationRegistry.get_historical_records(state=state)
    return render_template('historical_resources.html', records=records, state=state, active_page='historical_resources')

