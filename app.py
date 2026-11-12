"""St. Jerome Library Assistant - Modular Monolith Entrypoint."""

import os
from flask import Flask

from core.auth import get_or_create_secret_key, register_security_hooks
from core.features import get_feature_config, parse_bool
from core.migrations import run_startup_migrations
from modules import register_all_modules
from pos_routes import pos_bp


def create_app():
    """Application factory initializing migrations, security hooks, and all domain modules."""
    application = Flask(__name__)

    # Ensure schema and columns exist without mutating production data
    run_startup_migrations()
    feature_cfg = get_feature_config()

    # Security & session configuration
    application.secret_key = get_or_create_secret_key()
    application.config['SESSION_COOKIE_HTTPONLY'] = True
    application.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    # Offline portable runs over local HTTP (localhost/LAN), where Secure cookies fail to stick.
    secure_env = os.environ.get('STJEROME_SESSION_COOKIE_SECURE')
    if secure_env is not None:
        application.config['SESSION_COOKIE_SECURE'] = parse_bool(secure_env, default=True)
    else:
        application.config['SESSION_COOKIE_SECURE'] = (feature_cfg.get('APP_MODE') != 'offline_portable')
    application.config['PERMANENT_SESSION_LIFETIME'] = 86400 * 7

    # Register RBAC, context processors, teardown DB cleanup, and 500 alerts
    register_security_hooks(application)

    # Register POS blueprint & all domain modules (preserving flat url_for endpoints)
    application.register_blueprint(pos_bp)
    register_all_modules(application)

    return application


app = create_app()

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5005)
