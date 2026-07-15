import json
import os
import sys
import urllib.request
import urllib.parse
from pathlib import Path
from jinja2 import Template

from speedhive.workflows.track_records import curation as track_records


def _send_resend_notification(org_id_int: int, candidates: list, resend_api_key: str, from_email: str, to_emails: list) -> dict:
    new_records = sum(1 for c in candidates if c.get("type") == "new_record")
    unmapped = sum(1 for c in candidates if c.get("type") == "unmapped")
    total_candidates = len(candidates)

    # Read and render template
    from app import UI_PASSWORD
    template_path = Path(__file__).resolve().parent.parent / "templates" / "emails" / "track_records_review.html"
    template_content = template_path.read_text(encoding="utf-8")
    template = Template(template_content)
    email_html = template.render(
        org_id_int=org_id_int,
        new_records=new_records,
        unmapped=unmapped,
        total_candidates=total_candidates,
        UI_PASSWORD=UI_PASSWORD
    )

    payload = {
        "from": from_email,
        "to": to_emails,
        "subject": f"WHRRI Track Records: Review Required ({total_candidates} new candidates)",
        "html": email_html
    }

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {resend_api_key}",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _auto_notify_for_org(org_id: int) -> None:
    from app.routes.organizations import TRACK_RECORDS_ROOT
    try:
        p = track_records.paths_for_org(TRACK_RECORDS_ROOT, org_id)
        config_file = p["dir"] / "config.json"
        if not config_file.exists():
            print(f"[Notifier] Org {org_id} config.json missing. Skipping auto-notification.")
            return

        with open(config_file) as f:
            config = json.load(f)

        notif_config = config.get("notifications", {})
        if not notif_config.get("enabled", True):
            print(f"[Notifier] Notifications disabled for Org {org_id}. Skipping.")
            return

        resend_api_key = notif_config.get("resend_api_key") or os.environ.get("RESEND_API_KEY")
        from_email = notif_config.get("from_email") or os.environ.get("NOTIFICATION_FROM_EMAIL")
        to_emails = notif_config.get("to_emails")
        if not to_emails:
            env_to = os.environ.get("NOTIFICATION_TO_EMAILS")
            if env_to:
                if env_to.strip().startswith("["):
                    try:
                        to_emails = json.loads(env_to)
                    except Exception:
                        to_emails = [email.strip() for email in env_to.split(",") if email.strip()]
                else:
                    to_emails = [email.strip() for email in env_to.split(",") if email.strip()]

        if isinstance(to_emails, str):
            to_emails = [email.strip() for email in to_emails.split(",") if email.strip()]

        if not resend_api_key or not from_email or not to_emails:
            print(f"[Notifier] Missing configuration key(s) for Org {org_id}. Skipping email.")
            return

        candidates_data = track_records.load_candidates(p)
        candidates = candidates_data.get("candidates", [])
        if not candidates:
            return

        # De-duplication check: compute fingerprint
        fingerprint_list = sorted([
            f"{c.get('type')}:{c.get('proposed', {}).get('classAbbreviation')}:{c.get('proposed', {}).get('lapTime')}:{c.get('proposed', {}).get('date')}"
            for c in candidates
        ])
        fingerprint = ",".join(fingerprint_list)

        last_notified = candidates_data.get("last_notified_fingerprint")
        if not notif_config.get("de_duplicate", True) or last_notified != fingerprint:
            # Send email
            print(f"[Notifier] Sending review notification for Org {org_id} to {to_emails}...")
            _send_resend_notification(org_id, candidates, resend_api_key, from_email, to_emails)

            # Update last_notified_fingerprint on disk
            candidates_data["last_notified_fingerprint"] = fingerprint
            track_records.save_candidates(p, candidates_data)
            print(f"[Notifier] Notification sent successfully for Org {org_id}.")
        else:
            print(f"[Notifier] Pending candidates for Org {org_id} have not changed. Skipping duplicate email.")

    except Exception as exc:
        print(f"[Notifier] Error executing auto-notification for Org {org_id}: {str(exc)}", file=sys.stderr)
