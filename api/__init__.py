from flask import Flask, jsonify

from config import Config

from .routes import api_bp


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    app.register_blueprint(api_bp)

    @app.get("/")
    def index():
        return jsonify(
            {
                "service": "flask_modules",
                "message": "Flask server is running.",
                "api_base": "/api",
            }
        )

    return app
