# rfid_attendance_backend/__init__.py

import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from celery import Celery

# Initialize extensions globally
db = SQLAlchemy()
login_manager = LoginManager()
migrate = Migrate()
celery_app = Celery(__name__, broker='redis://redis:6379/0')

def create_app():
    app = Flask(__name__)

    # Configuration
    basedir = os.path.abspath(os.path.dirname(__file__))
    app.config['SECRET_KEY'] = 'your_super_secret_key'
    # Use PostgreSQL configuration for consistency with Docker setup
    app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://user:password@db:5432/mydatabase'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # Configure Celery
    app.config.update(
        CELERY_BROKER_URL='redis://redis:6379/0',
        CELERY_RESULT_BACKEND='redis://redis:6379/0'
    )
    celery_app.conf.update(app.config)

    # Initialize extensions with the app instance
    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)

    # Configure login manager
    login_manager.login_view = 'auth.login'
    login_manager.login_message_category = 'info'

    # Import and register blueprints
    from .auth import auth_bp
    from .staff_management import staff_management_bp
    from .reports import reports_bp
    from .api import api_bp
    from .main import main_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(staff_management_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(api_bp, url_prefix='/api')
    app.register_blueprint(main_bp)

    # It's important to import your models and tasks here so they are
    # discovered and registered by Flask and Celery.
    from . import models
    from . import tasks

    return app