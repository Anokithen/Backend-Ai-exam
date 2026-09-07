from flask import jsonify


def success_response(data=None, message="", status=200):
    return jsonify({"success": True, "data": data if data is not None else {}, "message": message}), status


def error_response(message, code="ERROR", status=400, details=None):
    error = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return jsonify({"success": False, "error": error}), status
