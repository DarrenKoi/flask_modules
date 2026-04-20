import os


class Config:
    """Base Flask configuration for local and cloud execution."""

    DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"
    TESTING = os.getenv("FLASK_TESTING", "0") == "1"
    JSON_SORT_KEYS = False
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me")
    SERVER_NAME = os.getenv("FLASK_SERVER_NAME") or None
