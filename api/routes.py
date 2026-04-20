from flask import Blueprint, jsonify


api_bp = Blueprint("api", __name__, url_prefix="/api")


@api_bp.get("/health")
def health_check():
    return jsonify({"status": "ok"})


@api_bp.get("/ping")
def ping():
    return jsonify({"message": "pong"})
