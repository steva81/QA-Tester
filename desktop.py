import webview
import threading
import sys
import os
import ctypes

import signal
import time

def show_error_dialog(message):
    if os.environ.get("QA_TESTER_SUPPRESS_ERROR_DIALOG") == "1":
        if sys.stdout is not None:
            print(message)
        return
    try:
        # 0x10 is MB_ICONERROR
        ctypes.windll.user32.MessageBoxW(0, message, "QA Tester Error", 0x10)
    except:
        if sys.stdout is not None:
            print(message)


def check_webview2_runtime():
    """Verify that Microsoft Edge WebView2 Runtime is installed on Windows."""
    if os.environ.get("QA_TESTER_SIMULATE_MISSING_WEBVIEW2") == "1" or "--simulate-missing-webview2" in sys.argv:
        return False, None, "Simulated missing Microsoft Edge WebView2 Runtime."
    if os.name != "nt":
        return True, "non-windows", None
    import winreg
    webview2_client_guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    reg_targets = [
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{webview2_client_guid}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{webview2_client_guid}"),
        (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{webview2_client_guid}"),
    ]
    for hive, subkey in reg_targets:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                pv, _ = winreg.QueryValueEx(key, "pv")
                if pv and str(pv).strip() and str(pv).strip() != "0.0.0.0":
                    return True, str(pv).strip(), None
        except OSError:
            continue
    return (
        False,
        None,
        "Microsoft Edge WebView2 Runtime is required to run the QA Tester desktop interface. "
        "Please install the Microsoft Edge WebView2 Evergreen Runtime from "
        "https://developer.microsoft.com/microsoft-edge/webview2/ and try again.",
    )

if __name__ == '__main__':
    # Safeguard for console=False where stdout/stderr might be None
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w')

    try:
        from app.paths import is_packaged, get_logs_dir, logger, get_base_dir
        
        if is_packaged():
            # Setup Playwright path for packaged mode
            os.environ['PLAYWRIGHT_BROWSERS_PATH'] = os.path.join(get_base_dir(), 'playwright_browsers')
            
        # Set environment variables for Flask
        os.environ['FLASK_ENV'] = 'production'
        
        # Port discovery
        port = 5000
        for i, arg in enumerate(sys.argv):
            if arg == '--port' and i + 1 < len(sys.argv):
                try:
                    port = int(sys.argv[i + 1])
                except ValueError:
                    pass
        if os.environ.get('QA_TESTER_PORT'):
            try:
                port = int(os.environ['QA_TESTER_PORT'])
            except ValueError:
                pass

        # Shutdown token discovery (endpoint disabled by default unless configured)
        shutdown_token = None
        for i, arg in enumerate(sys.argv):
            if arg == '--shutdown-token' and i + 1 < len(sys.argv):
                shutdown_token = sys.argv[i + 1]
        if not shutdown_token:
            shutdown_token = os.environ.get('QA_TESTER_SHUTDOWN_TOKEN')

        from server import app
        from app.local_server import make_desktop_server
        from flask import request, jsonify
        
        # Controlled Flask Server
        server = make_desktop_server(port, app)
        
        # Start the Flask server in a separate thread
        t = threading.Thread(target=server.serve_forever)
        t.daemon = True
        t.start()
        
        shutdown_lock = threading.Lock()
        shutdown_requested = threading.Event()
        shutdown_in_progress = threading.Event()
        shutdown_completed = threading.Event()
        window = None

        def perform_shutdown(reason="unknown"):
            with shutdown_lock:
                if shutdown_completed.is_set():
                    logger.info(f"Shutdown already completed. Ignoring duplicate request (reason: {reason}).")
                    return
                if shutdown_in_progress.is_set():
                    logger.info(f"Shutdown currently in progress. Awaiting completion (reason: {reason}).")
            
            if shutdown_in_progress.is_set() and not shutdown_completed.is_set():
                shutdown_completed.wait(timeout=30)
                return

            with shutdown_lock:
                if shutdown_completed.is_set() or shutdown_in_progress.is_set():
                    return
                shutdown_requested.set()
                shutdown_in_progress.set()

            logger.info(f"Initiating graceful application shutdown (reason: {reason})...")
            try:
                from app.routes.api import shutdown_qa_workers
                shutdown_qa_workers()
                logger.info("QA workers shut down successfully.")
            except Exception as e:
                logger.error(f"Error stopping QA engines: {e}")

            try:
                server.shutdown()
                logger.info("Flask server shut down successfully.")
            except Exception as e:
                logger.error(f"Error shutting down Flask server: {e}")

            if window and reason not in ("window closed event", "webview exit"):
                try:
                    window.destroy()
                except Exception as e:
                    logger.debug(f"Window destroy: {e}")

            shutdown_completed.set()
            logger.info(f"Graceful application shutdown completed (reason: {reason}).")

        # Controlled automation shutdown endpoint (protected by per-launch secret)
        @app.route('/api/system/shutdown', methods=['POST'])
        def system_shutdown():
            if not shutdown_token:
                logger.warning("Rejected /api/system/shutdown: endpoint disabled in this session.")
                return jsonify({"error": "Shutdown endpoint is disabled in this session."}), 403

            auth_header = request.headers.get('X-Shutdown-Token')
            body = request.get_json(silent=True) or {}
            req_token = auth_header or body.get('shutdown_token') or body.get('token')

            if not req_token or req_token != shutdown_token:
                logger.warning("Rejected /api/system/shutdown: invalid or missing shutdown token.")
                return jsonify({"error": "Unauthorized: invalid or missing shutdown token."}), 401

            logger.info("Authorized shutdown requested via /api/system/shutdown")
            shutdown_requested.set()

            def do_shutdown_async():
                time.sleep(0.1)
                perform_shutdown(reason="api /system/shutdown")

            threading.Thread(target=do_shutdown_async, daemon=True).start()
            return jsonify({"status": "shutting_down"}), 200

        server_only = '--server-only' in sys.argv or os.environ.get('QA_TESTER_SERVER_ONLY') == '1'
        if server_only:
            logger.info(f"Starting QA Tester in server-only mode on http://127.0.0.1:{port}...")
            def sig_handler(sig, frame):
                logger.info(f"Signal {sig} received, requesting shutdown...")
                shutdown_requested.set()
            try:
                signal.signal(signal.SIGINT, sig_handler)
                signal.signal(signal.SIGTERM, sig_handler)
            except Exception:
                pass
            try:
                while not shutdown_requested.is_set():
                    shutdown_requested.wait(timeout=0.5)
            except KeyboardInterrupt:
                shutdown_requested.set()
            finally:
                perform_shutdown(reason="server_only main exit")
                shutdown_completed.wait(timeout=30)
            sys.exit(0)

        wv_ok, wv_version, wv_err = check_webview2_runtime()
        if not wv_ok:
            logger.critical(f"WebView2 Runtime prerequisite check failed: {wv_err}")
            perform_shutdown(reason="missing_webview2_runtime")
            show_error_dialog(f"QA Tester requires Microsoft Edge WebView2 Runtime.\n\n{wv_err}")
            sys.exit(2)
        logger.info(f"Verified Microsoft Edge WebView2 Runtime version: {wv_version}")

        # Create the PyWebView window
        window = webview.create_window(
            'QA Tester',
            f'http://127.0.0.1:{port}',
            width=1200,
            height=800,
            resizable=True,
            min_size=(800, 600)
        )
        
        def on_window_closed():
            logger.info("Desktop window closed event received.")
            def _handle_window_closed():
                perform_shutdown(reason="window closed event")
                try:
                    import System.Windows.Forms as WinForms
                    WinForms.Application.Exit()
                except Exception:
                    pass
            threading.Thread(target=_handle_window_closed, daemon=True).start()

        window.events.closed += on_window_closed
        
        logger.info("Starting QA Tester UI...")
        webview.start()
        logger.info("webview.start() returned, ensuring shutdown completed...")
        perform_shutdown(reason="webview exit")
        shutdown_completed.wait(timeout=30)
        
    except Exception as e:
        import traceback
        error_msg = f"QA Tester failed to start.\n\nError: {str(e)}"
        try:
            from app.paths import get_logs_dir, logger
            logger.critical(f"Startup failed:\n{traceback.format_exc()}")
            log_path = os.path.join(get_logs_dir(), 'qa-tester.log')
            error_msg += f"\n\nPlease check the log file at:\n{log_path}"
        except:
            pass
        show_error_dialog(error_msg)
        sys.exit(1)
