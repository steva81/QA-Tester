"""Export routes backed by the same reporting model as Results and Copy Report."""
from datetime import datetime
from pathlib import Path
from flask import Blueprint, request, Response, abort, jsonify, make_response, has_request_context
from app.models.database import get_db_connection
from app.paths import get_exports_dir
from app.qa.reporting import load_report, MODES
from app.qa.report_renderers import render_text, render_markdown, render_html, render_docx
try:
    import docx
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

exports_bp = Blueprint('exports', __name__)


def report_error(status, message):
    if has_request_context() and request.path.startswith('/api/export/'):
        abort(make_response(jsonify(status='error', message=message), status))
    abort(status, description=message)


def get_report(run_id):
    conn = get_db_connection()
    try:
        conn.execute('BEGIN')
        report = load_report(conn, run_id)
    finally:
        conn.close()
    if report is None:
        report_error(404, 'Run or project not found.')
    return report


def get_report_data(run_id):
    """Compatibility API with strict allowlists and accepted customer evidence."""
    report = get_report(run_id)
    run = dict(report['run'], coverage=report['coverage'])
    return run, report['project'], report['findings'], report['tests'], report['stats']


def report_mode():
    mode = None
    if request.is_json and request.json and isinstance(request.json, dict):
        mode = request.json.get('mode')
    if not mode:
        mode = request.form.get('mode')
    if not mode:
        mode = request.args.get('mode')
    if not mode:
        mode = 'full'
    if mode not in MODES:
        report_error(400, 'Report mode must be standard or full.')
    return mode


def write_export(run_id, extension, content):
    path = Path(get_exports_dir()) / f'report_{run_id}_{datetime.now():%Y%m%d%H%M%S%f}.{extension}'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if extension == 'docx':
            content.save(path)
        else:
            path.write_text(content, encoding='utf-8')
    except OSError:
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass
        return {'status': 'error', 'message': 'Report could not be saved. Check the exports directory and try again.'}, 500
    return {'status': 'success', 'filename': path.name, 'path': str(path)}


@exports_bp.route('/export/<int:run_id>/html')
def export_html(run_id):
    mode = report_mode()
    content = render_html(get_report(run_id), mode)
    result = write_export(run_id, 'html', content.replace('/screenshots/', '../screenshots/'))
    if isinstance(result, tuple):
        return result
    return Response(content, mimetype='text/html')


@exports_bp.route('/api/export/<int:run_id>/html', methods=['POST'])
def export_html_api(run_id):
    mode = report_mode()
    content = render_html(get_report(run_id), mode)
    return write_export(run_id, 'html', content.replace('/screenshots/', '../screenshots/'))




@exports_bp.route('/api/export/<int:run_id>/docx', methods=['POST'])
def export_docx(run_id):
    mode = report_mode()
    if not DOCX_AVAILABLE:
        return {'status': 'error', 'message': 'python-docx is not installed. Document Export unavailable.'}, 500
    return write_export(run_id, 'docx', render_docx(get_report(run_id), mode))


@exports_bp.route('/api/export/<int:run_id>/markdown', methods=['POST'])
def export_md(run_id):
    mode = report_mode()
    return write_export(run_id, 'md', render_markdown(get_report(run_id), mode))


@exports_bp.route('/api/export/<int:run_id>/txt', methods=['POST'])
def export_txt(run_id):
    mode = report_mode()
    return write_export(run_id, 'txt', render_text(get_report(run_id), mode))
