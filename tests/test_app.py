import os
import sys
import tempfile
from pathlib import Path
import pytest

# Add parent directory of tests folder to Python path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# We set the environment variables before importing app to isolate the database
@pytest.fixture(scope="module", autouse=True)
def setup_test_env():
    # Create a temporary directory for web_data
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_speedhive.db"
        os.environ["SPEEDHIVE_DB_PATH"] = str(db_path)
        os.environ["SPEEDHIVE_WEB_DATA_DIR"] = tmpdir
        yield
        # Cleanup is handled by TemporaryDirectory

@pytest.fixture
def client():
    from app import app
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client

def test_app_home_route(client):
    """Test that the home page (dashboard) renders successfully."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Speedhive" in resp.data
    # Check that search interface/manual ID input is present
    assert b"Organization ID" in resp.data

def test_track_records_redirect(client):
    """Test that track-records route redirects to index with proper parameters."""
    resp = client.get("/track-records?org_id=123&classification=Kart")
    assert resp.status_code == 302
    assert "org_id=123" in resp.headers["Location"]
    assert "classification=Kart" in resp.headers["Location"]

def test_org_search_redirect(client):
    """Test that org-search route redirects to index."""
    resp = client.get("/org-search?org_id=456")
    assert resp.status_code == 302
    assert "org_id=456" in resp.headers["Location"]

    resp_empty = client.get("/org-search")
    assert resp_empty.status_code == 302
    assert resp_empty.headers["Location"] == "/" or resp_empty.headers["Location"].endswith("/")

def test_driver_search_redirect(client):
    """Test driver-search redirecting back to dashboard with search queries."""
    resp = client.get("/driver-search?org_id=789&q=John")
    assert resp.status_code == 302
    assert "org_id=789" in resp.headers["Location"]
    assert "q=John" in resp.headers["Location"]

def test_org_stats_invalid(client):
    """Test that stats page with an invalid org ID redirects to index with an error."""
    resp = client.get("/org/invalid_id/stats")
    assert resp.status_code == 302
    assert "error=" in resp.headers["Location"]

def test_org_operations_invalid(client):
    """Test that operations page with an invalid org ID redirects to index with an error."""
    resp = client.get("/org/invalid_id/operations")
    assert resp.status_code == 302
    assert "error=" in resp.headers["Location"]

def test_event_page_missing(client):
    """Test that requesting a missing event returns 404."""
    resp = client.get("/event/999999")
    assert resp.status_code == 404
    assert b"not found" in resp.data

def test_session_page_missing(client):
    """Test that requesting a missing session returns 404."""
    resp = client.get("/session/999999")
    assert resp.status_code == 404
    assert b"not found" in resp.data

def test_driver_laps_page_missing(client):
    """Test that requesting a missing driver laps returns 404."""
    resp = client.get("/session/999999/driver/123/laps")
    assert resp.status_code == 404
    assert b"not found" in resp.data
