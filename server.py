import os
import secrets
from flask import Flask, render_template, request, redirect, url_for, jsonify
from app.models.database import init_db

from app.paths import ensure_dirs, get_base_dir, logger

template_dir = os.path.join(get_base_dir(), 'templates')
static_dir = os.path.join(get_base_dir(), 'static')

app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)

# Ensure data directories exist
ensure_dirs()

from app.utils import format_local_time, display_severity_risk, display_review_status

@app.context_processor
def utility_processor():
    return dict(
        format_local_time=format_local_time,
        display_severity_risk=display_severity_risk,
        display_review_status=display_review_status
    )

# Initialize database
init_db()

# Initialize security guards (DNS rebinding and cross-site request protection)
from app.security import init_security
init_security(app)

# Initialize mutation request lifecycle lock
from app.data_lifecycle import init_data_lifecycle
init_data_lifecycle(app)

from app.routes.api import api_bp
from app.routes.views import views_bp
from app.routes.exports import exports_bp
app.register_blueprint(api_bp)
app.register_blueprint(views_bp)
app.register_blueprint(exports_bp)

if __name__ == '__main__':
    # Flask backend binds only to localhost
    app.run(host='127.0.0.1', port=5000, debug=True)

