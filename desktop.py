import webview
import threading
import sys
import os
import ctypes

def show_error_dialog(message):
    try:
        # 0x10 is MB_ICONERROR
        ctypes.windll.user32.MessageBoxW(0, message, "QA Tester Error", 0x10)
    except:
        print(message)

if __name__ == '__main__':
    try:
        from app.paths import is_packaged, get_logs_dir, logger, get_base_dir
        
        if is_packaged():
            # Setup Playwright path for packaged mode
            os.environ['PLAYWRIGHT_BROWSERS_PATH'] = os.path.join(get_base_dir(), 'playwright_browsers')
            
        # Set environment variables for Flask
        os.environ['FLASK_ENV'] = 'production'
        
        from server import app
        from werkzeug.serving import make_server
        
        # Controlled Flask Server
        server = make_server('127.0.0.1', 5000, app)
        
        # Start the Flask server in a separate thread
        t = threading.Thread(target=server.serve_forever)
        t.daemon = True
        t.start()
        
        def on_closed():
            logger.info("Window closed, shutting down server and QA engines...")
            # Stop any running engines gracefully
            try:
                from app.routes.api import running_engines
                for engine in list(running_engines.values()):
                    engine.stop()
            except Exception as e:
                logger.error(f"Error stopping engines: {e}")
                
            try:
                server.shutdown()
                logger.info("Flask server shut down successfully.")
            except Exception as e:
                logger.error(f"Error shutting down server: {e}")

        # Create the PyWebView window
        window = webview.create_window(
            'QA Tester',
            'http://127.0.0.1:5000',
            width=1200,
            height=800,
            resizable=True,
            min_size=(800, 600)
        )
        
        window.events.closed += on_closed
        
        logger.info("Starting QA Tester UI...")
        webview.start()
        
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
