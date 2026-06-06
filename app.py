"""Web app providing API endpoints for Speedhive data."""
import sys
from flask import Flask, jsonify, request
from speedhive_tools import (
    get_event_info,
    get_session_list,
    get_session_results,
    get_driver_details,
    get_lap_times,
)

app = Flask(__name__)

@app.route('/')
def index():
    return jsonify({"message": "Speedhive API wrapper. Use /event/<event_id> etc."})

@app.route('/event/<event_id>')
def event_info(event_id):
    try:
        data = get_event_info(event_id)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/event/<event_id>/sessions')
def session_list(event_id):
    try:
        data = get_session_list(event_id)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/session/<session_id>/results')
def session_results(session_id):
    try:
        data = get_session_results(session_id)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/driver/<driver_id>')
def driver_details(driver_id):
    try:
        data = get_driver_details(driver_id)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/session/<session_id>/driver/<driver_id>/laps')
def lap_times(session_id, driver_id):
    try:
        data = get_lap_times(session_id, driver_id)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
