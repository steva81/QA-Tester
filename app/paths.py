import os
import sys
import logging

APP_VERSION = '1.0.0'

def is_packaged():
    return getattr(sys, 'frozen', False)

def get_base_dir():
    if is_packaged():
        return sys._MEIPASS
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

def get_writable_dir():
    custom = os.environ.get('QA_TESTER_WRITABLE_DIR')
    if custom:
        return custom
    if is_packaged():
        local_app_data = os.environ.get('LOCALAPPDATA')
        if not local_app_data:
            local_app_data = os.path.expanduser('~\\AppData\\Local')
        return os.path.join(local_app_data, 'QATester')
    return get_base_dir()

if is_packaged():
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', os.path.join(get_base_dir(), 'playwright_browsers'))

def get_data_dir():
    return os.path.join(get_writable_dir(), 'data')

def get_screenshots_dir():
    return os.path.join(get_writable_dir(), 'screenshots')

def get_reports_dir():
    return os.path.join(get_writable_dir(), 'reports')

def get_exports_dir():
    return os.path.join(get_writable_dir(), 'exports')

def get_logs_dir():
    return os.path.join(get_writable_dir(), 'logs')

def get_settings_dir():
    return os.path.join(get_writable_dir(), 'settings')

def ensure_dirs():
    os.makedirs(get_data_dir(), exist_ok=True)
    os.makedirs(get_screenshots_dir(), exist_ok=True)
    os.makedirs(get_reports_dir(), exist_ok=True)
    os.makedirs(get_exports_dir(), exist_ok=True)
    os.makedirs(get_logs_dir(), exist_ok=True)
    os.makedirs(get_settings_dir(), exist_ok=True)

def chromium_launch_args(headless=False):
    """Explicit child-process log destination; Chromium can ignore CHROME_LOG_FILE alone.

    Legacy headless child processes (Playwright's default ``--headless``) ignore
    ``--log-file`` on Windows and fall back to ``debug.log`` beside the bundled
    chrome.exe, i.e. inside the installation directory. Headless launches
    therefore use the regular Chrome headless mode, whose children honor the
    writable log destination.
    """
    if not is_packaged():
        return []
    os.makedirs(get_logs_dir(), exist_ok=True)
    args = ['--log-file=' + os.path.abspath(os.path.join(get_logs_dir(), 'chromium_debug.log'))]
    if headless:
        args.append('--headless=new')
    return args

def setup_logging():
    ensure_dirs()
    if is_packaged():
        os.environ['CHROME_LOG_FILE'] = os.path.abspath(os.path.join(get_logs_dir(), 'chromium_debug.log'))
    log_file = os.path.join(get_logs_dir(), 'qa-tester.log')
    
    logger = logging.getLogger('qa-tester')
    logger.setLevel(logging.INFO)
    
    if not logger.handlers:
        if is_packaged():
            from logging.handlers import RotatingFileHandler
            handler = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5)
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            
            # Also redirect werkzeug logs
            w_logger = logging.getLogger('werkzeug')
            if not w_logger.handlers:
                w_logger.addHandler(handler)
                w_logger.setLevel(logging.INFO)
        else:
            handler = logging.StreamHandler()
            formatter = logging.Formatter('%(levelname)s: %(message)s')
            handler.setFormatter(formatter)
            logger.addHandler(handler)

    return logger

logger = setup_logging()
