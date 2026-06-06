"""Web app for Speedhive data with HTML frontend."""
import sys
from flask import Flask, render_template, request, jsonify
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
    return render_template('index.html')

@app.route('/event/<event_id>')
def event_info(event_id):
    try:
        data = get_event_info(event_id)
        return render_template('event.html', event=data, event_id=event_id)
    except Exception as e:
        return render_template('event.html', error=str(e), event_id=event_id), 500

@app.route('/event/<event_id>/sessions')
def session_list(event_id):
    try:
        data = get_session_list(event_id)
        return render_template('sessions.html', sessions=data, event_id=event_id)
    except Exception as e:
        return render_template('sessions.html', error=str(e), event_id=event_id), 500

@app.route('/session/<session_id>/results')
def session_results(session_id):
    try:
        data = get_session_results(session_id)
        return render_template('results.html', results=data, session_id=session_id)
    except Exception as e:
        return render_template('results.html', error=str(e), session_id=session_id), 500

@app.route('/driver/<driver_id>')
def driver_details(driver_id):
    try:
        data = get_driver_details(driver_id)
        return render_template('driver.html', driver=data)
    except Exception as e:
        return render_template('driver.html', error=str(e)), 500

@app.route('/session/<session_id>/driver/<driver_id>/laps')
def lap_times(session_id, driver_id):
    try:
        data = get_lap_times(session_id, driver_id)
        return render_template('lap_times.html', laps=data, session_id=session_id, driver_id=driver_id)
    except Exception as e:
        return render_template('lap_times.html', error=str(e), session_id=session_id, driver_id=driver_id), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8854, debug=True)
