"""
Module for interacting with the Speedhive race timing service.
Provides functions to retrieve event, session, driver, and lap data.
All functions return dicts/lists derived from the Speedhive JSON API.
"""

import httpx
from typing import List, Dict, Any

BASE_URL = "https://speedhive.mylaps.com/api/v1"

def get_event_info(event_id: str) -> Dict[str, Any]:
    """
    Fetch metadata for a race event.
    :param event_id: Speedhive event identifier (e.g., "E123456")
    :return: dict with keys: name, date, venue, country, etc.
    """
    url = f"{BASE_URL}/events/{event_id}"
    resp = httpx.get(url)
    resp.raise_for_status()
    return resp.json()["data"]

def get_session_list(event_id: str) -> List[Dict[str, Any]]:
    """
    Return all sessions belonging to an event.
    Each session dict contains: id, name, type (race / practice / qualifying), start_time, etc.
    """
    url = f"{BASE_URL}/events/{event_id}/sessions"
    resp = httpx.get(url)
    resp.raise_for_status()
    return resp.json()["data"]

def get_session_results(session_id: str) -> List[Dict[str, Any]]:
    """
    Return the classification results for a session.
    Each result dict: position, driver_name, car, total_time, best_lap_time, laps, interval, etc.
    """
    url = f"{BASE_URL}/sessions/{session_id}/results"
    resp = httpx.get(url)
    resp.raise_for_status()
    return resp.json()["data"]

def get_driver_details(driver_id: str) -> Dict[str, Any]:
    """
    Fetch personal details of a driver (name, nationality, date of birth, etc.)
    """
    url = f"{BASE_URL}/drivers/{driver_id}"
    resp = httpx.get(url)
    resp.raise_for_status()
    return resp.json()["data"]

def get_lap_times(session_id: str, driver_id: str) -> List[Dict[str, Any]]:
    """
    Retrieve every recorded lap for a driver in a session.
    Each lap dict: lap_number, time (string), time_ms (int), sector_times (list).
    """
    url = f"{BASE_URL}/sessions/{session_id}/drivers/{driver_id}/laps"
    resp = httpx.get(url)
    resp.raise_for_status()
    return resp.json()["data"]

if __name__ == "__main__":
    # Example usage
    event_id = "E123456"
    info = get_event_info(event_id)
    print(f"Event: {info['name']} at {info['venue']} ({info['date']})")
