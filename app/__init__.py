from flask import Flask
from dotenv import load_dotenv
import os

load_dotenv()

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key')
    
    # Import database to ensure initialization
    from app.services import database
    
    # Register routes AFTER database initialization
    from app.routes.api import bp as api_bp
    app.register_blueprint(api_bp)
    
    return app