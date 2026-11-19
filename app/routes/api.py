from flask import Blueprint, request, jsonify, send_from_directory
from app.models.database import get_db_connection
from app.qa.engine import SmartSmokeQA
from app.qa.full_site import FullSiteQA, SessionLost
from app.models.crawl import write_coverage, finish_coverage, get_coverage
from app.qa.explanations import get_explanations
from app.qa.assessment import assess_finding
from app.qa.interactive import start_session, stop_session, get_session
import asyncio
import threading
import os
import shutil
import time
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
import sqlite3
import json

api_bp = Blueprint('api', __name__)

running_engines = {}
active_worker_threads = []
worker_lock = threading.RLock()
worker_records = {}
accepting_runs = True
WORKER_SHUTDOWN_TIMEOUT = 10

@dataclass
class QAWorker:
    run_id: int
    thread: object = None
    engine: object = None
    cancel: threading.Event = field(default_factory=threading.Event)
    loop: object = None
    task: object = None
    future: object = None
    cleanup_done: threading.Event = field(default_factory=threading.Event)

def _mark_terminal(run_id, reason, status='ABORTED'):
    from app.qa.redaction import redact_string
    conn = get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        current = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if not current:
            conn.commit()
            return
        curr_status = current[0]
        if curr_status == 'COMPLETED':
            conn.commit()
            return

        should_record = False
        if curr_status == 'RUNNING':
            conn.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))
            should_record = True
        elif curr_status == 'ABORTED' and status == 'FAILED':
            conn.execute("UPDATE runs SET status='FAILED' WHERE id=?", (run_id,))
            should_record = True
        elif curr_status == 'FAILED' and status == 'FAILED':
            should_record = True

        if should_record:
            try:
                finish_coverage(conn, run_id, status, reason)
            except Exception:
                pass
            conn.execute(
                "INSERT INTO tests (run_id, url, expected_result, actual_result, status, viewport) VALUES (?, '', 'Run completes', ?, 'FAIL', 'System')",
                (run_id, redact_string(reason))
            )
        conn.commit()
    finally:
        conn.close()

def _cancel_worker(record, force=False):
    record.cancel.set()
    if record.engine:
        record.engine.stop()
    if record.future:
        record.future.cancel()
    elif force and record.loop and record.task and not record.loop.is_closed():
        def cancel_task():
            if not record.task.cancelling():
                record.task.cancel()
        try:
            record.loop.call_soon_threadsafe(cancel_task)
        except RuntimeError:
            # The worker may have closed its loop between inspection and dispatch.
            pass

def shutdown_qa_workers():
    global accepting_runs
    deadline = time.monotonic() + WORKER_SHUTDOWN_TIMEOUT
    with worker_lock:
        accepting_runs = False
        records = list(worker_records.values())
        for record in records:
            _cancel_worker(record, force=True)
            _mark_terminal(record.run_id, 'QA run interrupted during application shutdown.')
    for engine in list(running_engines.values()):
        engine.stop()
    from app.qa.interactive import sessions, session_lock, InteractiveSession
    with session_lock:
        closing = list(sessions.values())
        sessions.clear()
    for s in closing:
        if isinstance(s, InteractiveSession):
            s.stop(timeout=max(0, deadline - time.monotonic()))
        else:
            s.stop()
    for t in list(active_worker_threads):
        if t.is_alive() and t is not threading.current_thread():
            t.join(timeout=max(0, deadline - time.monotonic()))

def staged_delete(files_to_delete):
    import tempfile
    import shutil
    from pathlib import Path
    # Canonicalize identity for duplicate detection, but move the directory entry
    # itself: resolving a symlink for the move would move its target instead.
    paths = [Path(os.path.abspath(f)) for f in files_to_delete]
    keys = [os.path.normcase(str(p.resolve())) for p in paths]
    if len(keys) != len(set(keys)):
        raise ValueError('Duplicate normalized staging source')
    staged = []
    temp_dir = tempfile.mkdtemp(prefix="qa_staged_")
    try:
        for index, p in enumerate(paths):
            if p.exists():
                destination_dir = Path(temp_dir) / str(index)
                destination_dir.mkdir()
                dest = destination_dir / p.name
                shutil.move(str(p), str(dest))
                staged.append((p, dest))
        return staged, temp_dir
    except Exception as e:
        rollback_staged_delete(staged, temp_dir)
        raise e

def commit_staged_delete(temp_dir):
    import shutil
    if temp_dir and os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)

def rollback_staged_delete(staged, temp_dir):
    import shutil
    failures = []
    for orig, dest in staged:
        try:
            if dest.exists():
                if orig.exists():
                    raise FileExistsError('Restore would overwrite an existing file')
                shutil.move(str(dest), str(orig))
            elif not orig.exists():
                raise FileNotFoundError('Both staged and original file are missing')
        except Exception as exc:
            failures.append(f'{dest} -> {orig}: {exc}')
    if failures:
        raise RuntimeError(f'Rollback incomplete; staging preserved at {temp_dir}: ' + '; '.join(failures))
    if temp_dir and os.path.exists(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)

def _is_background_work_busy():
    """Check if any worker, engine, active worker thread, or interactive session is active.
    
    Must acquire worker_lock briefly for worker registers snapshot,
    then acquire session_lock separately for session snapshot.
    Never hold both locks simultaneously or over DB/blocking work.
    Returns (is_busy: bool, reason: str, status_code: int).
    """
    global accepting_runs
    with worker_lock:
        if not accepting_runs:
            return True, "Application is shutting down and cannot accept requests.", 503
        has_workers = bool(worker_records)
        has_engines = bool(running_engines)
        has_alive_threads = any(t.is_alive() for t in active_worker_threads)

    if has_workers or has_engines or has_alive_threads:
        return True, "Background QA runs or workers are currently active.", 409

    from app.qa.interactive import sessions, session_lock
    with session_lock:
        for s in sessions.values():
            thread_alive = bool(s.thread and s.thread.is_alive())
            if s.is_running or s.state == 'STARTING' or thread_alive:
                return True, "An interactive session is currently active, starting, or stopping.", 409

    return False, "", 200

from app.qa.redaction import redact_string

def _save_results(results, run_id, project_id, finish=True, coverage=None, terminal_status='COMPLETED', terminal_evidence=False):
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT status FROM runs WHERE id=?', (run_id,)).fetchone()
        stopped = row and row['status'] in ('ABORTED', 'FAILED')
        if not row or (terminal_evidence and (not stopped or finish)) or (stopped and not terminal_evidence):
            conn.rollback()
            return
        terminal_reason = None
        if stopped:
            previous = get_coverage(conn, run_id)
            terminal_reason = previous['termination_reason'] if previous else 'QA run interrupted.'
            # Only finalized safety evidence may be appended after termination.
            # Normal checkpoints and late successful results remain rejected.
            results = [r for r in results if r['status'] in ('SAFETY ACTION', 'LIMITED COVERAGE', 'DIAGNOSTIC')]
        passes, fails, obs = 0, 0, 0
        created_findings = {r[0] for r in c.execute('SELECT stable_id FROM findings WHERE run_id=?', (run_id,))}
        
        for r in results:
            url = redact_string(r['url'])
            final_url = redact_string(r.get('final_url'))
            actual_result = redact_string(r['actual_result'])
            expected_result = redact_string(r['expected_result'])
            
            c.execute('''
                INSERT INTO tests (run_id, url, expected_result, actual_result, status, viewport, screenshot_path, stable_id, final_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (run_id, url, expected_result, actual_result, r['status'], r['viewport'], r['screenshot'], r['finding_id'], final_url))
            
            if r['status'] == 'PASS': passes += 1
            elif r['status'] == 'FAIL': fails += 1
            elif r['status'] == 'OBSERVATION': obs += 1
            
            if r['status'] in ['FAIL', 'OBSERVATION'] and r['finding_id']:
                if r['finding_id'] not in created_findings:
                    title = redact_string(r.get('title', f"{r['status']} on {url}"))
                    area = redact_string(r.get('area', 'Unknown'))
                    assessment = assess_finding(title, actual_result, area, r['viewport'])
                    expl_en, expl_sv_learn, expl_sv_verb = get_explanations(actual_result=actual_result)
                    
                    c.execute('''
                        INSERT INTO findings (stable_id, project_id, run_id, title, area, severity, risk, confidence, description, impact, recommendation, plain_language_explanation, learning_explanation_sv, how_to_explain_sv, severity_origin, risk_origin, confidence_origin)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'baseline', 'baseline', 'baseline')
                    ''', (r['finding_id'], project_id, run_id, title, area, assessment['severity'], assessment['risk'], assessment['confidence'], actual_result, assessment['impact'], assessment['recommendation'], redact_string(expl_en), redact_string(expl_sv_learn), redact_string(expl_sv_verb)))
                    created_findings.add(r['finding_id'])
                
        if coverage:
            write_coverage(conn, run_id, coverage)
            if stopped:
                finish_coverage(conn, run_id, row['status'], terminal_reason)
        if finish:
            c.execute("UPDATE runs SET status = ?, end_time=CURRENT_TIMESTAMP, total_tests=(SELECT COUNT(*) FROM tests WHERE run_id=?) WHERE id=?", (terminal_status, run_id, run_id))
        else:
            c.execute("UPDATE runs SET total_tests=(SELECT COUNT(*) FROM tests WHERE run_id=?) WHERE id=?", (run_id, run_id))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_qa_async_in_thread(target_url, project_id, run_id, mode="crawl", collected_urls=None, full_options=None):
    with worker_lock:
        record = worker_records.setdefault(run_id, QAWorker(run_id, threading.current_thread()))
        record.thread = threading.current_thread()
    own_loop = None
    try:
        if record.cancel.is_set():
            _mark_terminal(run_id, 'QA run interrupted before startup.')
            return
        session = get_session(project_id)
        live = session and session.loop and not session._stop_event.is_set() and session.context
        if mode in ('current', 'collected', 'full') and not live:
            _mark_terminal(run_id, 'Interactive QA session is no longer active.')
            return
        if mode == 'full':
            def session_valid():
                return get_session(project_id) is session and session.is_ready()
            if not session_valid():
                raise SessionLost('Interactive session is not READY.')
            def checkpoint(batch, coverage):
                with worker_lock:
                    if not record.cancel.is_set():
                        _save_results(batch, run_id, project_id, finish=False, coverage=coverage)
            engine = FullSiteQA(target_url, project_id, run_id, context=session.context,
                                session_validator=session_valid, checkpoint=checkpoint, **(full_options or {}))
        else:
            engine_kwargs = {}
            if full_options and isinstance(full_options, dict):
                for k in ('enable_interactive_disclosure', 'interactive_options', 'enable_authorized_mutations'):
                    if k in full_options:
                        engine_kwargs[k] = full_options[k]
            engine = SmartSmokeQA(target_url, project_id, run_id, mode=mode,
                                  collected_urls=collected_urls, context=session.context if live else None,
                                  **engine_kwargs)
        with worker_lock:
            record.engine = engine
            running_engines[run_id] = engine
            if record.cancel.is_set():
                _mark_terminal(run_id, 'QA run interrupted before startup.')
                return
            if live:
                record.loop = session.loop
                async def interactive_run():
                    task = asyncio.current_task()
                    # A concurrent future can be cancelled before its asyncio
                    # task completes cleanup. Retrieve that eventual outcome too.
                    task.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)
                    record.task = task
                    if hasattr(session, 'qa_tasks'):
                        session.qa_tasks.add(task)
                    try:
                        return await engine.run()
                    finally:
                        if hasattr(session, 'qa_tasks'):
                            session.qa_tasks.discard(task)
                        record.cleanup_done.set()
                record.future = asyncio.run_coroutine_threadsafe(interactive_run(), session.loop)
            else:
                own_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(own_loop)
                record.loop = own_loop
                record.task = own_loop.create_task(engine.run())
        if live:
            deadline = time.monotonic() + 1800
            while True:
                try:
                    results = record.future.result(timeout=.25)
                    break
                except FutureTimeout:
                    if session._stop_event.is_set() or session.loop.is_closed() or not session.loop.is_running() or (mode == 'full' and not session_valid()):
                        record.cancel.set()
                        record.future.cancel()
                        _mark_terminal(run_id, 'Interactive QA session closed during the run.')
                        return
                    if time.monotonic() >= deadline:
                        record.future.cancel()
                        raise RuntimeError('Interactive QA worker timed out.')
        else:
            results = own_loop.run_until_complete(record.task)
        with worker_lock:
            cleanup_err = getattr(engine, 'cleanup_error', None)
            if cleanup_err:
                _mark_terminal(run_id, f'Worker cleanup failure: {cleanup_err}', 'FAILED')
            elif record.cancel.is_set() or getattr(engine, 'is_stopped', False):
                _mark_terminal(run_id, 'QA run interrupted before results were saved.')
            elif live and session._stop_event.is_set():
                _mark_terminal(run_id, 'Interactive QA session closed during the run.')
            elif mode == 'full' and not session_valid():
                _mark_terminal(run_id, 'Authenticated Interactive session was lost before completion.')
            else:
                if mode == 'full':
                    _save_results(results[engine.saved_offset:], run_id, project_id,
                                  coverage=engine.snapshot(), terminal_status=engine.terminal_status)
                else:
                    _save_results(results, run_id, project_id)
    except BaseException as exc:
        interrupted = record.cancel.is_set() or isinstance(exc, (asyncio.CancelledError, SessionLost))
        if 'session' in locals() and session and session._stop_event.is_set():
            interrupted = True
        cleanup_err = getattr(record.engine, 'cleanup_error', None) if record.engine else None
        if cleanup_err and not isinstance(exc, asyncio.CancelledError):
            _mark_terminal(run_id, f'Worker exception: {cleanup_err}', 'FAILED')
        elif cleanup_err and interrupted:
            _mark_terminal(run_id, f'QA run interrupted with cleanup failure: {cleanup_err}', 'FAILED')
        else:
            _mark_terminal(run_id, 'QA run interrupted.' if interrupted else f'Worker exception: {exc}',
                           'ABORTED' if interrupted else 'FAILED')
        if record.future:
            record.future.cancel()
    finally:
        try:
            # Cancelling the concurrent Future releases result() before the
            # browser coroutine has torn down guarded targets. Keep the worker
            # registered until that cleanup finishes, with a bounded wait.
            if record.future and record.task and not record.task.done():
                record.cleanup_done.wait(timeout=5)
            if mode == 'full' and record.engine and record.cleanup_done.is_set():
                with worker_lock:
                    engine = record.engine
                    _save_results(engine.results[engine.saved_offset:], run_id, project_id,
                                  finish=False, coverage=engine.snapshot(), terminal_evidence=True)
            if own_loop:
                try:
                    if record.task and not record.task.done():
                        try:
                            own_loop.run_until_complete(record.task)
                        except (asyncio.CancelledError, Exception):
                            pass
                    pending = [t for t in asyncio.all_tasks(own_loop) if not t.done()]
                    for task in pending:
                        task.cancel()
                    if pending:
                        own_loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    own_loop.run_until_complete(own_loop.shutdown_asyncgens())
                finally:
                    own_loop.close()
        finally:
            with worker_lock:
                running_engines.pop(run_id, None)
                worker_records.pop(run_id, None)
                if threading.current_thread() in active_worker_threads:
                    active_worker_threads.remove(threading.current_thread())

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

@api_bp.route('/api/projects/<int:project_id>/edit', methods=['POST'])
def edit_project(project_id):
    data = request.json
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        UPDATE projects SET name = ?, target_url = ?, environment = ?, tester_name = ?
        WHERE id = ?
    ''', (data['name'], data['target_url'], data['environment'], data['tester_name'], project_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@api_bp.route('/api/projects/<int:project_id>/delete', methods=['POST'])
def delete_project(project_id):
    is_busy, busy_msg, busy_status = _is_background_work_busy()
    if is_busy:
        return jsonify({"status": "error", "message": busy_msg}), busy_status

    from app.paths import get_screenshots_dir, get_exports_dir, get_writable_dir
    from pathlib import Path
    import json
    from datetime import datetime, timezone
    from app.qa.validation_contract import (
        prepare_bindings_after_project_delete,
        publish_prepared_bindings
    )

    managed_root = Path(get_writable_dir()).resolve()
    screenshots_dir = Path(get_screenshots_dir()).resolve()
    exports_dir = Path(get_exports_dir()).resolve()

    try:
        screenshots_dir.relative_to(managed_root)
        exports_dir.relative_to(managed_root)
    except ValueError:
        return jsonify({"status": "error", "message": "Path escape detected."}), 400

    conn = get_db_connection()
    c = conn.cursor()

    staged = []
    temp_dir = None
    db_committed = False

    try:
        # 1. BEGIN IMMEDIATE before reading records to delete
        c.execute("BEGIN IMMEDIATE")

        c.execute("SELECT name FROM projects WHERE id = ?", (project_id,))
        p_row = c.fetchone()
        if not p_row:
            conn.rollback()
            conn.close()
            return jsonify({"status": "error", "message": "Project not found"}), 404
        project_name = p_row['name']

        # 2. Read metadata and prepare all changes
        c.execute("SELECT id FROM runs WHERE project_id = ?", (project_id,))
        runs = c.fetchall()
        run_ids = [r['id'] for r in runs]

        files_to_delete = []
        if screenshots_dir.exists():
            for f in screenshots_dir.glob(f"proj{project_id}_*.png"):
                if f.is_file():
                    files_to_delete.append(f)

        if run_ids:
            placeholders = ','.join('?' * len(run_ids))
            c.execute(f"SELECT screenshot_path FROM tests WHERE run_id IN ({placeholders}) AND screenshot_path IS NOT NULL", run_ids)
            for row in c.fetchall():
                path_str = row['screenshot_path']
                c.execute("SELECT COUNT(*) FROM tests t JOIN runs r ON t.run_id = r.id WHERE t.screenshot_path = ? AND r.project_id != ?", (path_str, project_id))
                if c.fetchone()[0] == 0:
                    path = Path(path_str).resolve()
                    try:
                        path.relative_to(managed_root)
                        if path.is_file() and path not in files_to_delete:
                            files_to_delete.append(path)
                    except ValueError:
                        pass

        if exports_dir.exists():
            for rid in run_ids:
                for f in exports_dir.glob(f"report_{rid}_*.*"):
                    if f.is_file() and f not in files_to_delete:
                        files_to_delete.append(f)

        # 3. Prepare bindings list without modifying _REGISTERED_BINDINGS
        prepared_bindings = prepare_bindings_after_project_delete(project_id)

        # 4. Stage allowed files
        staged, temp_dir = staged_delete(files_to_delete)

        # 5. DB deletion & journal detachment in the same transaction
        c.execute("DELETE FROM validation_contracts WHERE project_id = ?", (project_id,))

        import uuid
        batch_key = str(uuid.uuid4())
        project_key = str(uuid.uuid4())

        c.execute("SELECT id, run_id, details_json FROM mutation_ownership_records WHERE project_id = ? AND project_id > 0 AND run_id > 0", (project_id,))
        m_rows = c.fetchall()

        # Validate all records before modifying anything
        for m_row in m_rows:
            m_id = m_row['id']
            try:
                d = json.loads(m_row['details_json'] or '{}')
                if not isinstance(d, dict):
                    conn.rollback()
                    conn.close()
                    if temp_dir is not None:
                        rollback_staged_delete(staged, temp_dir)
                    return jsonify({"status": "error", "message": f"Corrupt details_json in record {m_id}"}), 409
            except Exception:
                conn.rollback()
                conn.close()
                if temp_dir is not None:
                    rollback_staged_delete(staged, temp_dir)
                return jsonify({"status": "error", "message": f"Corrupt details_json in record {m_id}"}), 409

            if "historical_detachment" in d:
                conn.rollback()
                conn.close()
                if temp_dir is not None:
                    rollback_staged_delete(staged, temp_dir)
                return jsonify({"status": "error", "message": f"Contradictory historical_detachment in record {m_id}"}), 409

        for m_row in m_rows:
            m_id = m_row['id']
            r_id = m_row['run_id']
            d = json.loads(m_row['details_json'] or '{}')
            d['historical_detachment'] = {
                'archive_batch_key': batch_key,
                'archive_project_key': project_key,
                'detached_at': datetime.now(timezone.utc).isoformat(),
                'detached_reason': 'project_deleted',
                'original_project_id': project_id,
                'original_project_name': project_name,
                'original_run_id': r_id,
            }
            c.execute("""
                UPDATE mutation_ownership_records
                SET project_id = -abs(project_id),
                    run_id = -abs(run_id),
                    details_json = ?
                WHERE id = ?
            """, (json.dumps(d), m_id))

        c.execute("DELETE FROM findings WHERE project_id = ?", (project_id,))
        if run_ids:
            c.execute(f"DELETE FROM crawl_pages WHERE run_id IN ({placeholders})", run_ids)
            c.execute(f"DELETE FROM crawl_runs WHERE run_id IN ({placeholders})", run_ids)
            c.execute(f"DELETE FROM tests WHERE run_id IN ({placeholders})", run_ids)
        c.execute("DELETE FROM runs WHERE project_id = ?", (project_id,))
        c.execute("DELETE FROM projects WHERE id = ?", (project_id,))

        # 6. Commit DB
        conn.commit()
        db_committed = True

        # 7. Publish prepared bindings through simple assignment
        publish_prepared_bindings(prepared_bindings)

    except Exception as e:
        if not db_committed:
            try:
                conn.rollback()
            except Exception:
                pass
            conn.close()
            if temp_dir is not None:
                rollback_staged_delete(staged, temp_dir)
            return jsonify({"status": "error", "message": f"Project deletion failed: {e}"}), 500
        else:
            conn.close()
            import logging
            logging.error(f"Post-commit error during project delete (temp_dir={temp_dir}): {e}")
            return jsonify({
                "status": "error",
                "message": f"Database committed but post-commit operations failed: {e}",
                "staged_dir": temp_dir
            }), 500

    conn.close()

    # 8. Complete file staging separately from DB transaction
    try:
        commit_staged_delete(temp_dir)
    except Exception as e:
        import logging
        logging.error(f"Database committed but file cleanup failed at {temp_dir}: {e}")
        return jsonify({
            "status": "error",
            "message": f"Database committed but staging cleanup failed: {e}",
            "staged_dir": temp_dir
        }), 500

    return jsonify({"status": "success"})


@api_bp.route('/api/reset', methods=['POST'])
def reset_data():
    is_busy, busy_msg, busy_status = _is_background_work_busy()
    if is_busy:
        return jsonify({"status": "error", "message": busy_msg}), busy_status

    from app.paths import get_screenshots_dir, get_exports_dir, get_writable_dir
    from pathlib import Path
    import json
    from datetime import datetime, timezone
    from app.qa.validation_contract import publish_prepared_bindings

    managed_root = Path(get_writable_dir()).resolve()
    sd = Path(get_screenshots_dir()).resolve()
    ed = Path(get_exports_dir()).resolve()

    try:
        sd.relative_to(managed_root)
        ed.relative_to(managed_root)
    except ValueError:
        return jsonify({"status": "error", "message": "Path escape detected."}), 400

    files_to_delete = []
    if sd.exists():
        for f in sd.iterdir():
            if f.is_file() and f.name.endswith('.png'):
                files_to_delete.append(f)
    if ed.exists():
        for f in ed.iterdir():
            if f.is_file():
                files_to_delete.append(f)

    # 3. Prepare bindings list (empty for reset)
    prepared_bindings = []

    staged = []
    temp_dir = None
    db_committed = False

    conn = get_db_connection()
    c = conn.cursor()

    try:
        # 1. BEGIN IMMEDIATE before reading records
        c.execute("BEGIN IMMEDIATE")

        # 4. Stage allowed files
        staged, temp_dir = staged_delete(files_to_delete)

        # 5. DB deletion & journal detachment in the same transaction
        c.execute("DELETE FROM validation_contracts")

        import uuid
        batch_key = str(uuid.uuid4())

        c.execute("SELECT id, name FROM projects")
        project_names = {row['id']: row['name'] for row in c.fetchall()}
        project_keys = {}

        c.execute("SELECT id, project_id, run_id, details_json FROM mutation_ownership_records WHERE project_id > 0 AND run_id > 0")
        records_to_detach = c.fetchall()

        # Validate all records before modifying anything
        for m_row in records_to_detach:
            m_id = m_row['id']
            try:
                d = json.loads(m_row['details_json'] or '{}')
                if not isinstance(d, dict):
                    conn.rollback()
                    conn.close()
                    if temp_dir is not None:
                        rollback_staged_delete(staged, temp_dir)
                    return jsonify({"status": "error", "message": f"Corrupt details_json in record {m_id}"}), 409
            except Exception:
                conn.rollback()
                conn.close()
                if temp_dir is not None:
                    rollback_staged_delete(staged, temp_dir)
                return jsonify({"status": "error", "message": f"Corrupt details_json in record {m_id}"}), 409

            if "historical_detachment" in d:
                conn.rollback()
                conn.close()
                if temp_dir is not None:
                    rollback_staged_delete(staged, temp_dir)
                return jsonify({"status": "error", "message": f"Contradictory historical_detachment in record {m_id}"}), 409

        for m_row in records_to_detach:
            m_id = m_row['id']
            p_id = m_row['project_id']
            r_id = m_row['run_id']
            if p_id not in project_keys:
                project_keys[p_id] = str(uuid.uuid4())
            p_key = project_keys[p_id]
            p_name = project_names.get(p_id, f"Project {p_id}")

            d = json.loads(m_row['details_json'] or '{}')
            d['historical_detachment'] = {
                'archive_batch_key': batch_key,
                'archive_project_key': p_key,
                'detached_at': datetime.now(timezone.utc).isoformat(),
                'detached_reason': 'data_reset',
                'original_project_id': p_id,
                'original_project_name': p_name,
                'original_run_id': r_id,
            }
            c.execute("""
                UPDATE mutation_ownership_records
                SET project_id = -abs(project_id),
                    run_id = -abs(run_id),
                    details_json = ?
                WHERE id = ?
            """, (json.dumps(d), m_id))

        c.execute("DELETE FROM crawl_pages")
        c.execute("DELETE FROM crawl_runs")
        c.execute("DELETE FROM findings")
        c.execute("DELETE FROM tests")
        c.execute("DELETE FROM runs")
        c.execute("DELETE FROM projects")
        c.execute("DELETE FROM sqlite_sequence WHERE name IN ('findings', 'tests', 'runs', 'projects', 'validation_contracts')")

        # 6. Commit DB
        conn.commit()
        db_committed = True

        # 7. Publish prepared bindings
        publish_prepared_bindings(prepared_bindings)

    except Exception as e:
        if not db_committed:
            try:
                conn.rollback()
            except Exception:
                pass
            conn.close()
            if temp_dir is not None:
                rollback_staged_delete(staged, temp_dir)
            return jsonify({"status": "error", "message": f"Reset failed: {e}"}), 500
        else:
            conn.close()
            import logging
            logging.error(f"Post-commit error during data reset (temp_dir={temp_dir}): {e}")
            return jsonify({
                "status": "error",
                "message": f"Database committed but post-commit operations failed: {e}",
                "staged_dir": temp_dir
            }), 500

    conn.close()

    # 8. Complete file staging separately from DB transaction
    try:
        commit_staged_delete(temp_dir)
    except Exception as e:
        import logging
        logging.error(f"Database committed but file cleanup failed at {temp_dir}: {e}")
        return jsonify({
            "status": "error",
            "message": f"Database committed but staging cleanup failed: {e}",
            "staged_dir": temp_dir
        }), 500

    return jsonify({"status": "success"})


def inspect_stored_contract_row(cr) -> dict:
    """Inspect a stored validation_contracts row without auto-migrating legacy/invalid rules."""
    import json
    from app.qa.validation_contract import ValidationContract, ValidationTestCase

    row_dict = dict(cr)
    info = {
        "config_valid": False,
        "config_error": None,
        "field_selectors": {},
        "test_cases": [],
        "positive_count": 0,
        "negative_count": 0,
        "completion_mode": None,
        "completion_marker_attr": None,
        "completion_marker_type": None,
        "completion_timestamp_attr": None,
    }
    raw_cfg = row_dict.get("config_json")
    if not raw_cfg or not isinstance(raw_cfg, str):
        info["config_error"] = "Legacy or incomplete rule: missing explicit field selectors and test cases. Review and replace before running tests."
        return info
    try:
        cfg = json.loads(raw_cfg)
    except Exception:
        info["config_error"] = "Stored rule configuration JSON is malformed. Review and replace or delete this rule."
        return info
    if not isinstance(cfg, dict):
        info["config_error"] = "Stored rule configuration must be a JSON object."
        return info

    info["completion_mode"] = cfg.get("completion_mode")
    info["completion_marker_attr"] = cfg.get("completion_marker_attr")
    info["completion_marker_type"] = cfg.get("completion_marker_type")
    info["completion_timestamp_attr"] = cfg.get("completion_timestamp_attr")

    fselectors = cfg.get("field_selectors")
    tcases_raw = cfg.get("test_cases")
    if (
        not isinstance(fselectors, dict)
        or not fselectors
        or any(not isinstance(k, str) or not k.strip() or not isinstance(v, str) or not v.strip() for k, v in fselectors.items())
    ):
        info["config_error"] = "Rule requires at least one explicit form field mapping (field name -> CSS selector)."
        return info
    if not isinstance(tcases_raw, list) or not tcases_raw:
        info["config_error"] = "Rule requires at least one explicit positive or negative test case."
        return info

    info["field_selectors"] = fselectors
    try:
        tcases = tuple(ValidationTestCase(**tc) for tc in tcases_raw)
        if any(
            not isinstance(tc.case_id, str)
            or not tc.case_id.strip()
            or not isinstance(tc.description, str)
            or not tc.description.strip()
            or not isinstance(tc.input_values, dict)
            or not tc.input_values
            or type(tc.expected_valid) is not bool
            or not set(tc.input_values).issubset(fselectors)
            for tc in tcases
        ):
            raise ValueError("Each test case requires a case ID, description, input values mapped to configured fields, and boolean expected_valid.")
    except Exception as exc:
        info["config_error"] = f"Invalid test cases in stored rule: {exc}"
        return info

    info["test_cases"] = tcases_raw
    info["positive_count"] = sum(1 for tc in tcases if tc.expected_valid is True)
    info["negative_count"] = sum(1 for tc in tcases if tc.expected_valid is False)

    fb_sel = (row_dict.get("feedback_selector") or "").strip()
    ctrl_id = (row_dict.get("target_control_id") or "").strip()
    acc_name = (row_dict.get("accessible_name") or "").strip()
    if not fb_sel or not (ctrl_id or acc_name):
        info["config_error"] = "Rule requires a Feedback Element Selector and either a Trigger Button ID or Accessible Name."
        return info

    vcontract = ValidationContract(
        contract_id=row_dict.get("contract_id") or "",
        rule_name=row_dict.get("rule_name") or "",
        trigger_control_id=ctrl_id or None,
        field_selectors=fselectors,
        feedback_selector=fb_sel,
        test_cases=tcases,
        target_url_pattern=row_dict.get("target_url_pattern"),
        trigger_name=acc_name or None,
        trigger_selector=row_dict.get("trigger_selector"),
        completion_marker_attr=info["completion_marker_attr"],
        completion_marker_type=info["completion_marker_type"],
        completion_timestamp_attr=info["completion_timestamp_attr"],
        completion_mode=info["completion_mode"],
    )
    valid_comp, comp_err = vcontract.validate_completion_configuration()
    if not valid_comp:
        info["config_error"] = comp_err or "Invalid completion condition configuration."
        return info

    info["config_valid"] = True
    info["vcontract"] = vcontract
    return info


def _load_project_validation_contracts(conn, project_id: int) -> int:
    """Load and register persisted contracts for this project across all modes."""
    try:
        c_contracts = conn.cursor()
        c_rows = c_contracts.execute("SELECT * FROM validation_contracts WHERE project_id = ?", (project_id,)).fetchall()
        if c_rows:
            from app.qa.validation_contract import register_validation_contract
            for cr in c_rows:
                info = inspect_stored_contract_row(cr)
                if not info["config_valid"]:
                    raise ValueError(info["config_error"] or "Stored contract requires explicit cases and fields")
                vcontract = info["vcontract"]
                register_validation_contract(
                    contract=vcontract,
                    target_url_pattern=cr["target_url_pattern"],
                    target_control_id=cr["target_control_id"],
                    trigger_selector=cr["trigger_selector"],
                    accessible_name=cr["accessible_name"],
                    feedback_selector=cr["feedback_selector"],
                    project_id=project_id,
                )
            return len(c_rows)
        return 0
    except Exception as exc:
        raise ValueError("Stored validation contracts could not be loaded") from exc


@api_bp.route('/api/runs', methods=['POST'])
def start_run():
    data = request.json or {}
    project_id = data.get('project_id')
    mode = data.get('mode', 'crawl')
    collected_urls = data.get('collected_urls', [])
    if mode not in ('crawl', 'current', 'collected', 'full'):
        return jsonify({'error': 'Unknown QA mode.'}), 400
    raw_disclosure = data.get('enable_interactive_disclosure', False)
    if not isinstance(raw_disclosure, bool):
        return jsonify({'error': 'enable_interactive_disclosure must be a boolean.'}), 400
    raw_mutations = data.get('enable_authorized_mutations', False)
    if not isinstance(raw_mutations, bool):
        return jsonify({'error': 'enable_authorized_mutations must be a boolean.'}), 400
    if raw_mutations and mode not in ('full', 'current'):
        return jsonify({'error': 'Authorized profile workflows are only supported in Full Site QA or Current page mode.'}), 400

    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute("SELECT target_url FROM projects WHERE id = ?", (project_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Project not found"}), 404

    # Load and register persisted contracts for this project across all modes
    try:
        contracts_count = _load_project_validation_contracts(conn, project_id)
    except ValueError as exc:
        conn.close()
        return jsonify({"error": str(exc)}), 400
    eff_disclosure = raw_disclosure or (contracts_count > 0)

    run_options = {}
    if eff_disclosure:
        run_options['enable_interactive_disclosure'] = True
        if 'interactive_options' in data:
            io_data = data['interactive_options']
            if not isinstance(io_data, dict):
                conn.close()
                return jsonify({'error': 'interactive_options must be an object.'}), 400
            from app.qa.interactive_bridge import InteractiveWorkflowConfig
            try:
                InteractiveWorkflowConfig(enabled=True, **io_data)
            except (TypeError, ValueError) as val_err:
                conn.close()
                return jsonify({'error': f'Invalid interactive options: {val_err}'}), 400
            run_options['interactive_options'] = io_data
        else:
            run_options['interactive_options'] = {
                'max_evaluations_per_page': 20,
                'max_evaluations_per_run': 50,
            }
    elif 'interactive_options' in data:
        conn.close()
        return jsonify({'error': 'Interactive options require enable_interactive_disclosure to be true.'}), 400

    if raw_mutations:
        run_options['enable_authorized_mutations'] = True
        
    if mode == 'full':
        session = get_session(project_id)
        if not session or not session.is_ready():
            conn.close()
            return jsonify({'error': 'A READY Interactive QA session is required.'}), 400
        page_limit = data.get('page_limit', 250)
        if type(page_limit) is not int or not 1 <= page_limit <= 1000:
            conn.close()
            return jsonify({'error': 'Page limit must be an integer between 1 and 1000.'}), 400
        target_url = row['target_url']
        from app.qa.crawl_policy import link_reason
        from app.utils import get_canonical_origin
        if link_reason(target_url, get_canonical_origin(target_url)):
            conn.close()
            return jsonify({'error': 'Project root is not a safe Full Site QA starting URL.'}), 400
        current_url = session.get_current_url()
        if not current_url or get_canonical_origin(current_url) != get_canonical_origin(target_url):
            conn.close()
            return jsonify({'error': 'The Interactive browser must be on the project origin.'}), 400
        run_options['page_limit'] = page_limit
        run_options['current_url'] = current_url
    elif mode == "current":
        session = get_session(project_id)
        if not session or not session.is_running:
            conn.close()
            return jsonify({"error": "Interactive QA session is required."}), 400
        target_url = session.get_current_url()
        if not target_url:
            conn.close()
            return jsonify({"error": "Could not get current URL from live session."}), 400
        from app.utils import get_canonical_origin
        p_origin = get_canonical_origin(row['target_url'])
        if get_canonical_origin(target_url) != p_origin:
            conn.close()
            return jsonify({"error": "Current URL origin mismatch."}), 400
    elif mode == "collected":
        session = get_session(project_id)
        if not session or not session.is_running:
            conn.close()
            return jsonify({"error": "Interactive QA session is required."}), 400
        from urllib.parse import urlparse
        from app.utils import get_canonical_origin
        p_origin = get_canonical_origin(row['target_url'])
        clean_urls = []
        for url in collected_urls:
            if not url.startswith('http'): continue
            if get_canonical_origin(url) != p_origin: continue
            parsed = urlparse(url)
            path = parsed.path.rstrip('/') or '/'
            norm_url = parsed._replace(fragment="", path=path).geturl()
            if norm_url not in clean_urls:
                clean_urls.append(norm_url)
        if not clean_urls:
            conn.close()
            return jsonify({"error": "No valid collected URLs."}), 400
        collected_urls = clean_urls
        target_url = row['target_url']
    else:
        target_url = row['target_url']
    
    with worker_lock:
        if not accepting_runs:
            conn.close()
            return jsonify({"error": "Application is shutting down."}), 503
        c.execute("INSERT INTO runs (project_id, status, authorized_at) VALUES (?, 'RUNNING', CURRENT_TIMESTAMP)", (project_id,))
        run_id = c.lastrowid
        if mode == 'full':
            from app.models.crawl import METRICS
            progress = dict(phase='QUEUED', current_url=None, termination_reason='', page_limit=run_options.get('page_limit', 250),
                            page_limit_reached=False, coverage_complete=False, **dict.fromkeys(METRICS, 0))
            write_coverage(conn, run_id, dict(progress=progress,
                limits=dict(query_limit=10,depth_limit=50,url_limit=min(10000,run_options.get('page_limit', 250)*10+20),navigation_limit=run_options.get('page_limit', 250)*4+100), pages=[]))
        conn.commit()
        conn.close()
        args = (target_url, project_id, run_id, mode, collected_urls)
        if run_options:
            args += (run_options,)
        t = threading.Thread(target=run_qa_async_in_thread, args=args)
        t.daemon = True
        worker_records[run_id] = QAWorker(run_id, t)
        active_worker_threads.append(t)
        try:
            t.start()
        except Exception as exc:
            worker_records.pop(run_id, None)
            active_worker_threads.remove(t)
            _mark_terminal(run_id, f'Worker startup failed: {exc}', 'FAILED')
            return jsonify({"error": "QA worker could not start."}), 500

    return jsonify({"run_id": run_id, "status": "started"})

@api_bp.route('/api/runs/<int:run_id>/status')
def get_run_status(run_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT status FROM runs WHERE id = ?", (run_id,))
    row = c.fetchone()
    coverage = get_coverage(conn, run_id)
    conn.close()
    if row:
        response = {'status': row['status']}
        if coverage:
            engine = running_engines.get(run_id)
            if row['status'] == 'RUNNING' and engine and hasattr(engine, 'get_progress'):
                coverage.update(engine.get_progress())
            response['coverage'] = coverage
        return jsonify(response)
    return jsonify({"status": "not_found"}), 404

@api_bp.route('/api/runs/<int:run_id>/stop', methods=['POST'])
def stop_run(run_id):
    with worker_lock:
        record = worker_records.get(run_id)
        if record or run_id in running_engines:
            if record:
                _cancel_worker(record)
                _mark_terminal(run_id, 'QA run interrupted by the user.')
            else:
                running_engines[run_id].stop()
            return jsonify({"status": "stopping"})
    return jsonify({"status": "not_running"})

@api_bp.route('/api/findings/<int:finding_id>/edit', methods=['POST'])
def update_finding(finding_id):
    data = request.json or {}
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        SELECT severity, risk, confidence, severity_origin, risk_origin, confidence_origin,
               status, impact, recommendation, plain_language_explanation,
               learning_explanation_sv, how_to_explain_sv
        FROM findings WHERE id = ?
    ''', (finding_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"status": "not_found"}), 404

    curr_sev = row['severity'] if isinstance(row, sqlite3.Row) else row[0]
    curr_risk = row['risk'] if isinstance(row, sqlite3.Row) else row[1]
    curr_conf = row['confidence'] if isinstance(row, sqlite3.Row) else row[2]
    curr_sev_orig = (row['severity_origin'] if isinstance(row, sqlite3.Row) else row[3]) or 'legacy'
    curr_risk_orig = (row['risk_origin'] if isinstance(row, sqlite3.Row) else row[4]) or 'legacy'
    curr_conf_orig = (row['confidence_origin'] if isinstance(row, sqlite3.Row) else row[5]) or 'legacy'
    curr_status = row['status'] if isinstance(row, sqlite3.Row) else row[6]
    curr_impact = row['impact'] if isinstance(row, sqlite3.Row) else row[7]
    curr_rec = row['recommendation'] if isinstance(row, sqlite3.Row) else row[8]
    curr_plain = row['plain_language_explanation'] if isinstance(row, sqlite3.Row) else row[9]
    curr_learn = row['learning_explanation_sv'] if isinstance(row, sqlite3.Row) else row[10]
    curr_verb = row['how_to_explain_sv'] if isinstance(row, sqlite3.Row) else row[11]

    # Partial update preservation: preserve existing values if field not in data
    new_status = data['status'] if 'status' in data else curr_status
    new_impact = data['impact'] if 'impact' in data else curr_impact
    new_rec = data['recommendation'] if 'recommendation' in data else curr_rec
    new_plain = data['plain_language_explanation'] if 'plain_language_explanation' in data else curr_plain
    new_learn = data['learning_explanation_sv'] if 'learning_explanation_sv' in data else curr_learn
    new_verb = data['how_to_explain_sv'] if 'how_to_explain_sv' in data else curr_verb

    # Field-level assessment update & provenance resolution:
    # Distinguishes deliberate user edits from unchanged displayed/effective values.
    # When initial field metadata is supplied, unchanged form values preserve stored database values
    # and baseline provenance. Unrelated automatic fields are not frozen into manual provenance.
    def resolve_assessment_field(field_name, curr_val, curr_orig):
        initial_key = f'initial_{field_name}'
        explicit_origin = data.get(f'{field_name}_origin')

        if initial_key in data:
            # Client provided initial metadata (e.g. form initialized with stored or effective value).
            # A deliberate user edit occurred if and only if the submitted value differs from initial.
            if field_name in data and data[field_name] != data[initial_key]:
                return data[field_name], (explicit_origin or 'manual')
            else:
                # User did not modify this field from its initial form state.
                # Strictly preserve the stored database value and existing provenance.
                return curr_val, (explicit_origin or curr_orig)
        else:
            # Caller did not supply initial metadata (direct API caller, script, or partial update).
            if field_name in data:
                if data[field_name] != curr_val:
                    # Deliberate change relative to stored database value
                    return data[field_name], (explicit_origin or 'manual')
                else:
                    # Submitted value matches stored value
                    return curr_val, (explicit_origin or curr_orig)
            else:
                # Field not included in submission (e.g. partial notes-only update)
                return curr_val, (explicit_origin or curr_orig)

    new_sev, sev_origin = resolve_assessment_field('severity', curr_sev, curr_sev_orig)
    new_risk, risk_origin = resolve_assessment_field('risk', curr_risk, curr_risk_orig)
    new_conf, conf_origin = resolve_assessment_field('confidence', curr_conf, curr_conf_orig)

    c.execute('''
        UPDATE findings 
        SET severity = ?, risk = ?, confidence = ?, impact = ?, recommendation = ?, status = ?,
            plain_language_explanation = ?, learning_explanation_sv = ?, how_to_explain_sv = ?,
            severity_origin = ?, risk_origin = ?, confidence_origin = ?
        WHERE id = ?
    ''', (new_sev, new_risk, new_conf, new_impact, new_rec, new_status,
          new_plain, new_learn, new_verb,
          sev_origin, risk_origin, conf_origin, finding_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@api_bp.route('/api/runs/<int:run_id>/triage', methods=['GET', 'POST'])
def run_triage(run_id):
    from app.qa.triage import triage_run
    conn = get_db_connection()
    try:
        run = conn.execute('SELECT id FROM runs WHERE id=?', (run_id,)).fetchone()
        if run is None:
            return jsonify({'status': 'not_found'}), 404
        return jsonify({'run_id': run_id, 'findings': triage_run(conn, run_id)})
    finally:
        conn.close()

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
    screenshot_dir = os.path.abspath(get_screenshots_dir())
    filepath = os.path.abspath(os.path.join(screenshot_dir, filename))
    if not (filepath == screenshot_dir or filepath.startswith(screenshot_dir + os.sep)):
        return jsonify({"status": "error", "message": "Access denied"}), 403
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
    export_dir = os.path.abspath(get_exports_dir())
    filepath = os.path.abspath(os.path.join(export_dir, filename))
    if not (filepath == export_dir or filepath.startswith(export_dir + os.sep)):
        return jsonify({"status": "error", "message": "Access denied"}), 403
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
    os.makedirs(exports_dir, exist_ok=True)
    try:
        if os.name == 'nt':
            os.startfile(exports_dir)
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

# --- Interactive QA Endpoints ---

@api_bp.route('/api/projects/<int:project_id>/interactive/start', methods=['POST'])
def interactive_start(project_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT target_url FROM projects WHERE id = ?", (project_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Project not found"}), 404
        
    start_session(project_id, row['target_url'])
    return jsonify({"status": "success"})

@api_bp.route('/api/projects/<int:project_id>/interactive/stop', methods=['POST'])
def interactive_stop(project_id):
    stopped = stop_session(project_id)
    if not stopped:
        return jsonify({"status": "error", "message": "Interactive session thread is still stopping."}), 409
    return jsonify({"status": "success"})

@api_bp.route('/api/projects/<int:project_id>/interactive/state', methods=['GET'])
def interactive_state(project_id):
    session = get_session(project_id)
    if session and session.is_running:
        current_url = session.get_current_url()
        return jsonify({
            "active": True,
            "state": getattr(session, 'state', 'READY'),
            "current_url": current_url,
            "collected_urls": list(session.collected_urls)
        })
    return jsonify({"active": False})

@api_bp.route('/api/projects/<int:project_id>/interactive/collect', methods=['POST'])
def interactive_collect(project_id):
    session = get_session(project_id)
    if not session or not session.is_running:
        return jsonify({"error": "No active session"}), 400
        
    current_url = session.get_current_url()
    if current_url:
        urls = session.add_collected_url(current_url)
        return jsonify({"status": "success", "collected_urls": urls})
    return jsonify({"error": "Could not get current URL"}), 400

@api_bp.route('/api/projects/<int:project_id>/interactive/clear', methods=['POST'])
def interactive_clear(project_id):
    session = get_session(project_id)
    if session:
        session.clear_collected_urls()
    return jsonify({"status": "success"})


@api_bp.route('/api/projects/<int:project_id>/contracts', methods=['POST'])
def register_project_contract(project_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, target_url FROM projects WHERE id = ?", (project_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Project not found"}), 404

    data = request.json or {}
    raw_contract_id = data.get("contract_id", "")
    raw_rule_name = data.get("rule_name", "")
    if not isinstance(raw_contract_id, str) or not raw_contract_id.strip() or not isinstance(raw_rule_name, str) or not raw_rule_name.strip():
        conn.close()
        return jsonify({"error": "Rule ID (contract_id) and Rule Name (rule_name) are required"}), 400
    contract_id = raw_contract_id.strip()
    rule_name = raw_rule_name.strip()

    target_url_pattern = data.get("target_url_pattern") or f"*{row['target_url']}*"
    target_control_id = (data.get("target_control_id") or "").strip() or None
    trigger_selector = (data.get("trigger_selector") or "").strip() or None
    accessible_name = (data.get("accessible_name") or "").strip() or None
    feedback_selector = (data.get("feedback_selector") or "").strip() or None
    field_selectors = data.get("field_selectors", {})
    test_cases_data = data.get("test_cases", [])
    replace_row_id = data.get("replace_row_id")

    import json
    from app.qa.validation_contract import (
        register_validation_contract,
        unregister_validation_contract,
        ValidationContract,
        ValidationTestCase,
    )

    if (
        not isinstance(field_selectors, dict)
        or not field_selectors
        or any(not isinstance(k, str) or not k.strip() or not isinstance(v, str) or not v.strip() for k, v in field_selectors.items())
        or not isinstance(test_cases_data, list)
        or not test_cases_data
    ):
        conn.close()
        return jsonify({"error": "Explicit field_selectors and non-empty test_cases are required"}), 400

    fields = {k.strip(): v.strip() for k, v in field_selectors.items()}
    if len(fields) != len(field_selectors):
        conn.close()
        return jsonify({"error": "Field names must be unique after trimming"}), 400
    try:
        test_cases = tuple(ValidationTestCase(**tc) for tc in test_cases_data)
        if len({tc.case_id.strip() for tc in test_cases if isinstance(tc.case_id, str)}) != len(test_cases):
            raise ValueError("Case IDs must be unique and non-empty")
        if any(
            not isinstance(tc.case_id, str)
            or not tc.case_id.strip()
            or not isinstance(tc.description, str)
            or not tc.description.strip()
            or not isinstance(tc.input_values, dict)
            or not tc.input_values
            or type(tc.expected_valid) is not bool
            or not set(tc.input_values).issubset(fields)
            for tc in test_cases
        ):
            raise ValueError("Each case requires inputs mapped to fields and boolean expected_valid")
    except (TypeError, ValueError) as exc:
        conn.close()
        return jsonify({"error": str(exc)}), 400

    fb_sel = feedback_selector or ""
    completion_marker_attr = (data.get("completion_marker_attr") or "").strip() or None
    completion_marker_type = (data.get("completion_marker_type") or "").strip() or None
    completion_timestamp_attr = (data.get("completion_timestamp_attr") or "").strip() or None
    completion_mode = (data.get("completion_mode") or "").strip() or None

    contract = ValidationContract(
        contract_id=contract_id,
        rule_name=rule_name,
        trigger_control_id=target_control_id,
        field_selectors=fields,
        feedback_selector=fb_sel,
        test_cases=test_cases,
        target_url_pattern=target_url_pattern,
        trigger_name=accessible_name,
        trigger_selector=trigger_selector,
        completion_marker_attr=completion_marker_attr,
        completion_marker_type=completion_marker_type,
        completion_timestamp_attr=completion_timestamp_attr,
        completion_mode=completion_mode,
    )

    valid, error = contract.validate_completion_configuration()
    if not valid or not feedback_selector or not (target_control_id or accessible_name):
        conn.close()
        return jsonify({"error": error or "Feedback selector and trigger ID or name are required"}), 400

    if replace_row_id is not None:
        if type(replace_row_id) is not int:
            conn.close()
            return jsonify({"error": "replace_row_id must be an integer"}), 400
        old_row = c.execute(
            "SELECT id, contract_id FROM validation_contracts WHERE id = ? AND project_id = ?",
            (replace_row_id, project_id),
        ).fetchone()
        if not old_row:
            conn.close()
            return jsonify({"error": "Rule to replace was not found in this project"}), 404
        c.execute("DELETE FROM validation_contracts WHERE id = ? AND project_id = ?", (replace_row_id, project_id))

    config_json = json.dumps({
        "field_selectors": fields,
        "completion_marker_attr": completion_marker_attr,
        "completion_marker_type": completion_marker_type,
        "completion_timestamp_attr": completion_timestamp_attr,
        "completion_mode": completion_mode,
        "test_cases": [
            {
                "case_id": tc.case_id,
                "description": tc.description,
                "input_values": tc.input_values,
                "expected_valid": tc.expected_valid,
                "expected_feedback_substring": tc.expected_feedback_substring,
                "expected_feedback_absent": tc.expected_feedback_absent,
            }
            for tc in test_cases
        ],
    })

    c.execute(
        """
        INSERT INTO validation_contracts (
            project_id, contract_id, rule_name, target_url_pattern,
            target_control_id, trigger_selector, accessible_name,
            feedback_selector, config_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id, contract_id, rule_name, target_url_pattern,
            target_control_id, trigger_selector, accessible_name,
            fb_sel, config_json
        ),
    )
    contract_row_id = c.lastrowid
    conn.commit()
    conn.close()
    if replace_row_id is not None:
        unregister_validation_contract(old_row["contract_id"], project_id=project_id)
    register_validation_contract(
        contract=contract, target_url_pattern=target_url_pattern,
        target_control_id=target_control_id, trigger_selector=trigger_selector,
        accessible_name=accessible_name, feedback_selector=fb_sel, project_id=project_id,
    )

    return jsonify({
        "status": "success",
        "id": contract_row_id,
        "contract_id": contract_id,
        "rule_name": rule_name,
        "project_id": project_id,
        "replaced_id": replace_row_id,
        "completion_marker_attr": completion_marker_attr,
        "completion_marker_type": completion_marker_type,
        "completion_timestamp_attr": completion_timestamp_attr,
        "completion_mode": completion_mode,
    }), 201


@api_bp.route('/api/projects/<int:project_id>/contracts', methods=['GET'])
def get_project_contracts(project_id):
    conn = get_db_connection()
    c = conn.cursor()
    rows = c.execute(
        "SELECT * FROM validation_contracts WHERE project_id = ?", (project_id,)
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        item = dict(r)
        info = inspect_stored_contract_row(r)
        info.pop("vcontract", None)
        item.update(info)
        result.append(item)
    return jsonify(result), 200


@api_bp.route('/api/projects/<int:project_id>/contracts/<int:contract_id>', methods=['DELETE'])
def delete_project_contract(project_id, contract_id):
    conn = get_db_connection()
    c = conn.cursor()
    row = c.execute("SELECT contract_id FROM validation_contracts WHERE id = ? AND project_id = ?", (contract_id, project_id)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Contract not found"}), 404
    from app.qa.validation_contract import unregister_validation_contract
    unregister_validation_contract(row["contract_id"], project_id=project_id)
    c.execute("DELETE FROM validation_contracts WHERE id = ? AND project_id = ?", (contract_id, project_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "deleted_id": contract_id}), 200


@api_bp.route('/api/projects/<int:project_id>/pending-resources', methods=['GET'])
def get_project_pending_resources(project_id):
    from app.qa.scoped_mutation import ScopedMutationRegistry
    records = ScopedMutationRegistry.get_pending_records_for_display(project_id=project_id)
    return jsonify({"pending_resources": records, "count": len(records)}), 200


@api_bp.route('/api/runs/<int:run_id>/pending-resources', methods=['GET'])
def get_run_pending_resources(run_id):
    from app.qa.scoped_mutation import ScopedMutationRegistry
    records = ScopedMutationRegistry.get_pending_records_for_display(run_id=run_id)
    return jsonify({"pending_resources": records, "count": len(records)}), 200


@api_bp.route('/api/historical-resources', methods=['GET'])
def get_historical_resources():
    state = request.args.get('state', 'pending')
    if state not in ('pending', 'all'):
        return jsonify({"error": "state parameter must be 'pending' or 'all'"}), 400

    limit_str = request.args.get('limit', '100')
    try:
        limit = int(limit_str)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400

    before_id = None
    if 'before_id' in request.args:
        try:
            before_id = int(request.args['before_id'])
            if before_id <= 0:
                return jsonify({"error": "before_id must be a positive integer"}), 400
        except ValueError:
            return jsonify({"error": "before_id must be an integer"}), 400

    from app.qa.scoped_mutation import ScopedMutationRegistry
    try:
        records = ScopedMutationRegistry.get_historical_records(state=state, limit=limit, before_id=before_id)
    except Exception as e:
        return jsonify({"error": f"Failed to query historical resources: {e}"}), 500

    return jsonify({"historical_resources": records, "count": len(records)}), 200


@api_bp.route('/api/historical-resources/<int:record_id>/resolve', methods=['POST'])
def resolve_historical_resource(record_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "A JSON object is required"}), 400

    archive_key = data.get("archive_project_key")
    if not isinstance(archive_key, str) or not archive_key.strip():
        return jsonify({"error": "A non-empty string archive_project_key is required"}), 400
    archive_key = archive_key.strip()

    note = data.get("note")
    if not isinstance(note, str) or not note.strip():
        return jsonify({"error": "A non-empty string note is required"}), 400
    note = note.strip()

    conn = get_db_connection()
    c = conn.cursor()
    row = c.execute(
        "SELECT id, project_id, run_id, status, remote_status, details_json FROM mutation_ownership_records WHERE id = ?",
        (record_id,),
    ).fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "Resource record not found"}), 404

    if row["project_id"] > 0 and row["run_id"] > 0:
        return jsonify({"error": "Record belongs to an active project; use active project resolution endpoint"}), 400

    raw_details = row["details_json"] or "{}"
    try:
        d = json.loads(raw_details)
        if not isinstance(d, dict):
            return jsonify({"error": "Cannot resolve record with corrupt metadata"}), 409
    except Exception:
        return jsonify({"error": "Cannot resolve record with corrupt metadata"}), 409

    hd = d.get("historical_detachment")
    if isinstance(hd, dict) and hd.get("archive_project_key"):
        expected_key = hd["archive_project_key"]
    else:
        expected_key = f"legacy-record:{row['id']}"

    if archive_key != expected_key:
        return jsonify({"error": "archive_project_key mismatch"}), 403

    if row["status"] in ("DELETED", "RESOLVED_MANUAL") or row["remote_status"] in ("CONFIRMED_DELETED", "MANUALLY_RESOLVED"):
        return jsonify({"error": "Resource is already resolved or deleted"}), 409

    from app.qa.scoped_mutation import ScopedMutationRegistry
    updated = ScopedMutationRegistry.mark_record_resolved_manual(
        record_id,
        note=note,
        archive_project_key=archive_key
    )
    if not updated:
        return jsonify({"error": "Failed to update record state (possible conflict)"}), 409

    return jsonify({
        "status": "success",
        "record_id": record_id,
        "resolved": True,
        "server_deletion_confirmed": False,
    }), 200


@api_bp.route('/api/pending-resources/<int:record_id>/resolve', methods=['POST'])
@api_bp.route('/api/projects/<int:project_id>/pending-resources/<int:record_id>/resolve', methods=['POST'])
def resolve_pending_resource(record_id, project_id=None):
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("note", ""), str):
        return jsonify({"error": "A JSON object and text note are required"}), 400
    from app.qa.scoped_mutation import ScopedMutationRegistry
    conn = get_db_connection()
    c = conn.cursor()
    row = c.execute(
        "SELECT id, project_id, status, remote_status FROM mutation_ownership_records WHERE id = ?",
        (record_id,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Resource record not found"}), 404

    req_proj = project_id if project_id is not None else data.get("project_id")
    if type(req_proj) is not int or req_proj <= 0:
        return jsonify({"error": "A positive integer project_id is required"}), 400
    if row["project_id"] <= 0:
        return jsonify({"error": "Historical resource records cannot be resolved via active endpoint; use /api/historical-resources/<id>/resolve"}), 400
    if data.get("project_id", req_proj) != req_proj or req_proj != row["project_id"]:
        return jsonify({"error": "Resource does not belong to specified project"}), 403

    note = (data.get("note") or "").strip()
    if not note:
        return jsonify({"error": "A non-empty verification note is required"}), 400

    if row["status"] in ("DELETED", "RESOLVED_MANUAL") or row["remote_status"] in ("CONFIRMED_DELETED", "MANUALLY_RESOLVED"):
        return jsonify({"error": "Resource is already resolved or deleted and cannot be re-resolved"}), 400

    updated = ScopedMutationRegistry.mark_record_resolved_manual(record_id, note=note, project_id=req_proj)
    if not updated:
        return jsonify({"error": "Failed to update record state"}), 409
    return jsonify({
        "status": "success",
        "record_id": record_id,
        "resolved": True,
        "server_deletion_confirmed": False,
    }), 200


