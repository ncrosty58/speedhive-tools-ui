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
        os.environ["SPEEDHIVE_UI_PASSWORD"] = "test-password"
        yield
        # Cleanup is handled by TemporaryDirectory

@pytest.fixture
def client():
    """A logged-in client (the whole UI sits behind the site password)."""
    from app import app, UI_PASSWORD
    app.config["TESTING"] = True
    with app.test_client() as client:
        client.post("/login", data={"password": UI_PASSWORD})
        yield client

@pytest.fixture
def anon_client():
    from app import app
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client

def test_login_required_for_ui(anon_client):
    """Anonymous UI requests are sent to the login page."""
    resp = anon_client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]

def test_public_track_records_feed_stays_open(anon_client):
    """The curated feed and CI sync endpoints must not require a login."""
    resp = anon_client.get("/org/123/track-records/curated.json")
    assert resp.status_code == 200
    resp = anon_client.get("/org/123/track-records/update/status")
    assert resp.status_code in (200, 502)  # may fail upstream, but not a login redirect

def test_login_wrong_password(anon_client):
    resp = anon_client.post("/login", data={"password": "nope"})
    assert resp.status_code == 200
    assert b"Incorrect password" in resp.data

def test_login_and_logout(anon_client):
    from app import UI_PASSWORD
    resp = anon_client.post("/login", data={"password": UI_PASSWORD})
    assert resp.status_code == 302
    resp = anon_client.get("/")
    assert resp.status_code == 200
    resp = anon_client.post("/logout")
    assert resp.status_code == 302
    resp = anon_client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]

def test_add_organization_requires_login(anon_client):
    resp = anon_client.get("/organizations/add")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]

def test_add_organization_page_and_validation(client):
    resp = client.get("/organizations/add")
    assert resp.status_code == 200
    assert b"Add organization" in resp.data
    # non-numeric input is rejected before any Speedhive lookup happens
    resp = client.post("/organizations/add", data={"org_id": "not-a-number"})
    assert resp.status_code == 200
    assert b"numeric" in resp.data

def test_track_records_ndjson_import_export_roundtrip(client):
    import io
    import json as jsonlib
    rec1 = {"classAbbreviation": "FP", "lapTime": "1:13.325", "driverName": "Jerry Morlewski", "date": "2026-05-24", "marque": "Triumph"}
    rec2 = {"classAbbreviation": "GT1", "lapTime": "1:08.001", "driverName": "Test Driver", "date": "2020-08-01"}
    ndjson = (jsonlib.dumps(rec1) + "\n" + jsonlib.dumps(rec2) + "\n").encode()

    resp = client.post("/org/555/track-records/curated/import",
                       data={"file": (io.BytesIO(ndjson), "records.ndjson"), "mode": "merge"},
                       content_type="multipart/form-data", follow_redirects=False)
    assert resp.status_code == 302
    assert "Imported+2" in resp.headers["Location"] or "Imported%202" in resp.headers["Location"]

    # re-import: both are duplicates now
    resp = client.post("/org/555/track-records/curated/import",
                       data={"file": (io.BytesIO(ndjson), "records.ndjson"), "mode": "merge"},
                       content_type="multipart/form-data")
    assert "skipped" in resp.headers["Location"]

    resp = client.get("/org/555/track-records/curated.ndjson")
    assert resp.status_code == 200
    lines = [jsonlib.loads(line) for line in resp.get_data(as_text=True).strip().splitlines()]
    assert len(lines) == 2
    assert lines[0]["classAbbreviation"] == "FP"

    # replace mode shrinks the list to the file contents
    resp = client.post("/org/555/track-records/curated/import",
                       data={"file": (io.BytesIO((jsonlib.dumps(rec2) + "\n").encode()), "r.ndjson"), "mode": "replace"},
                       content_type="multipart/form-data")
    resp = client.get("/org/555/track-records/curated.ndjson")
    lines = resp.get_data(as_text=True).strip().splitlines()
    assert len(lines) == 1

def test_track_records_ndjson_import_rejects_bad_lines(client):
    import io
    bad = b'{"classAbbreviation": "FP", "lapTime": "not-a-time", "driverName": "X", "date": "2026-01-01"}\n'
    resp = client.post("/org/556/track-records/curated/import",
                       data={"file": (io.BytesIO(bad), "bad.ndjson"), "mode": "merge"})
    assert "error=" in resp.headers["Location"]
    # nothing was written
    resp = client.get("/org/556/track-records/curated.ndjson")
    assert resp.get_data(as_text=True).strip() == ""


def test_operations_lists_multiple_dump_snapshots(client, monkeypatch):
    import json as jsonlib
    import app as app_module

    saved_at_values = iter([
        "2026-07-15T21:30:00Z",
        "2026-07-16T21:30:00Z",
    ])

    def fake_export_db_dump(storage, org_id, output_dir, max_events=None):
        saved_at = next(saved_at_values)
        output_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "org_id": org_id,
            "saved_at": saved_at,
            "events_count": 1,
            "sessions_count": 2,
            "laps_records_count": 3,
        }
        (output_dir / "manifest.json").write_text(jsonlib.dumps(manifest), encoding="utf-8")
        (output_dir / "events.ndjson").write_text("{}", encoding="utf-8")
        return {"path": str(output_dir), **manifest}

    monkeypatch.setattr(app_module, "export_db_dump", fake_export_db_dump)

    first = client.post("/org/777/dumps", data={"max_events": "25"}, follow_redirects=False)
    assert first.status_code == 302
    second = client.post("/org/777/dumps", data={"max_events": "25"}, follow_redirects=False)
    assert second.status_code == 302

    latest_manifest = app_module.DUMPS_ROOT / "777" / "manifest.json"
    archive_manifest = app_module.DUMPS_ROOT / "777" / "history" / "20260715T213000Z" / "manifest.json"
    assert latest_manifest.exists()
    assert archive_manifest.exists()

    ops = client.get("/org/777/operations")
    assert ops.status_code == 200
    assert ops.data.count(b"Download ZIP") == 2
    assert ops.data.count(b"Delete Dump") == 2
    assert b"Current" in ops.data

    latest_zip = client.get("/org/777/dumps/latest.zip")
    assert latest_zip.status_code == 200
    assert latest_zip.mimetype == "application/zip"

    archive_zip = client.get("/org/777/dumps/20260715T213000Z.zip")
    assert archive_zip.status_code == 200
    assert archive_zip.mimetype == "application/zip"

    delete_archive = client.post("/org/777/dumps/20260715T213000Z/delete", follow_redirects=False)
    assert delete_archive.status_code == 302
    assert not archive_manifest.exists()

    ops_after_archive_delete = client.get("/org/777/operations")
    assert ops_after_archive_delete.status_code == 200
    assert ops_after_archive_delete.data.count(b"Download ZIP") == 1
    assert ops_after_archive_delete.data.count(b"Delete Dump") == 1

    delete_latest = client.post("/org/777/dumps/delete", follow_redirects=False)
    assert delete_latest.status_code == 302
    assert not latest_manifest.exists()

    ops_after_latest_delete = client.get("/org/777/operations")
    assert ops_after_latest_delete.status_code == 200
    assert b"No offline export generated yet." in ops_after_latest_delete.data

def test_app_home_route(client):
    """Test that the home page (dashboard) renders successfully."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Speedhive" in resp.data
    # Check that search interface/manual ID input is present
    assert b"Organization ID" in resp.data

def test_track_records_redirect(client):
    """Test that track-records route redirects to the Lap Records page with proper parameters."""
    resp = client.get("/track-records?org_id=123&classification=Kart")
    assert resp.status_code == 302
    assert "/org/123/lap-records" in resp.headers["Location"]
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

def test_upload_local_dump_success(client, monkeypatch):
    """Test importing an offline ZIP dump successfully."""
    import zipfile
    import io
    
    # Mock import_dump_to_storage
    mock_summary = {"events": 5, "sessions": 10, "results": 20, "laps": 100, "announcements": 2}
    import speedhive.workflows.import_sqlite_dump as import_module
    monkeypatch.setattr(
        import_module,
        "import_dump_to_storage",
        lambda org, dump_dir, storage: mock_summary
    )

    # Create dummy ZIP in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("777/events.ndjson", '{"id": 1, "name": "Event 1"}\n')
        zf.writestr("777/sessions.ndjson", '{"id": 10}\n')

    zip_buffer.seek(0)
    
    # POST file upload
    resp = client.post(
        "/org/777/dumps/import",
        data={"file": (zip_buffer, "test_dump.zip")},
        content_type="multipart/form-data",
        follow_redirects=False
    )
    assert resp.status_code == 302
    assert "notice=" in resp.headers["Location"]
    import urllib.parse
    decoded_location = urllib.parse.unquote_plus(resp.headers["Location"])
    assert "imported offline dump" in decoded_location
