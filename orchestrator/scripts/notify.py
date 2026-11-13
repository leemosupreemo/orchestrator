#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import json
import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate
from pathlib import Path

# Add scripts dir to path
SCRIPTS_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = SCRIPTS_DIR.parents[1]
if str(PACKAGE_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT.parent))

from orchestrator.project_config import PROJECT_CONFIG

ROOT = PROJECT_CONFIG.root
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))

try:
    from common import read_json
except ImportError:
    # Fallback if common.py can't be imported
    def read_json(path: Path) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

def desktop_notifications_off() -> bool:
    settings_path = PROJECT_CONFIG.config_dir / "settings.json"
    try:
        return read_json(settings_path).get("desktop_notifications", True) is False
    except Exception:
        return False


def notify(title: str, message: str, job_id: str | None = None, summary: str | None = None) -> None:
    if os.environ.get("ORCHESTRATOR_DISABLE_NOTIFICATIONS") == "1":
        print(f"      - Notifications disabled for: {title}")
        return False

    print(f"      - Sending notifications for: {title}")
    
    # 1. Desktop Notification (macOS), unless turned off under Configuration > Alerts
    try:
        if desktop_notifications_off():
            raise RuntimeError("turned off in Configuration > Alerts")
        safe_title = title.replace('"', '\\"')
        safe_message = message.replace('"', '\\"')
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{safe_message}" with title "{safe_title}"'
            ],
            check=False,
            timeout=5 # Don't hang the whole script if osascript stalls
        )
    except Exception as e:
        print(f"      - Desktop notification skipped: {e}")

    # 2. Email Notification
    success = send_email_notification(title, message, job_id, summary)
    return success

def send_email_notification(title: str, message: str, job_id: str | None = None, summary: str | None = None) -> bool:
    print("      - Preparing email notification...")
    settings_path = PROJECT_CONFIG.config_dir / "settings.json"
    if not settings_path.exists():
        return False

    try:
        settings = read_json(settings_path)
    except:
        return False

    emails = settings.get("notification_emails", [])
    if not emails:
        print("      - Skipped email notification (no recipient emails configured in settings.json).")
        return False

    primary_provider = settings.get("notification_provider", "gmail").lower()
    
    # Try primary first, then fallback
    providers_to_try = [primary_provider]
    if primary_provider == "resend":
        providers_to_try.append("gmail")
    else:
        providers_to_try.append("resend")

    last_error = None
    
    for provider in providers_to_try:
        # Credentials
        sender_email = settings.get("smtp_email")
        app_password = settings.get("smtp_password")

        # Provider-specific configuration
        if provider == "resend":
            smtp_server = "smtp.resend.com"
            smtp_port = 587
            login_user = "resend"
            login_pass = settings.get("resend_api_key")
            display_name = settings.get("resend_display_name", PROJECT_CONFIG.notification_display_name)
            from_email = settings.get("resend_from_email") or sender_email
            
            if not login_pass: # Skip if no key
                continue
        else:
            smtp_server = "smtp.gmail.com"
            smtp_port = 587
            login_user = sender_email
            login_pass = app_password
            display_name = PROJECT_CONFIG.notification_display_name
            from_email = sender_email
            
            if not login_user or not login_pass: # Skip if no gmail setup
                continue

        if provider != primary_provider:
            print(f"      - Primary provider failed. Attempting failover via {provider}...")

        # Determine status color
        header_color = "#22d3ee" # Cyan for info/unknown
        if "SUCCESS" in title.upper():
            header_color = "#22c55e" # Green
        elif "FAILED" in title.upper() or "FAILURE" in title.upper() or "ERROR" in title.upper():
            header_color = "#ef4444" # Red
        elif "PAUSED" in title.upper() or "CLARIFICATION" in title.upper():
            header_color = "#f59e0b" # Orange

        subject = f"[AI-WORKFLOW] {title}"
        if job_id:
            subject += f" ({job_id})"

        # Format the message (handling newlines for HTML)
        html_message = message.replace("\n", "<br>")
        
        summary_section = ""
        if summary:
            html_summary = summary.replace("\n", "<br>")
            summary_section = f"""
                    <div class="section">
                        <div class="section-title">Accomplishment</div>
                        <div class="metadata" style="border-left-color: #22c55e;">
                            <div class="message" style="font-size: 14px; font-style: italic;">{html_summary}</div>
                        </div>
                    </div>
            """

        # Modern HTML Template
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #334155; margin: 0; padding: 0; }}
                .container {{ max-width: 600px; margin: 20px auto; border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden; }}
                .header {{ background-color: {header_color}; color: white; padding: 20px; text-align: center; }}
                .header h1 {{ margin: 0; font-size: 20px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }}
                .content {{ padding: 30px; background-color: #ffffff; }}
                .section {{ margin-bottom: 25px; }}
                .section-title {{ font-size: 12px; font-weight: 700; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 8px; }}
                .message {{ font-size: 16px; color: #1e293b; }}
                .metadata {{ background-color: #f8fafc; border-radius: 6px; padding: 15px; border-left: 4px solid #cbd5e1; }}
                .metadata-item {{ font-size: 13px; margin-bottom: 5px; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
                .metadata-label {{ font-weight: 600; color: #64748b; min-width: 80px; display: inline-block; }}
                .footer {{ padding: 15px; text-align: center; font-size: 12px; color: #94a3b8; background-color: #f8fafc; border-top: 1px solid #e2e8f0; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>{title}</h1>
                </div>
                <div class="content">
                    {summary_section}
                    <div class="section">
                        <div class="section-title">Message</div>
                        <div class="message">{html_message}</div>
                    </div>
                    
                    <div class="section">
                        <div class="section-title">Job Details</div>
                        <div class="metadata">
                            <div class="metadata-item"><span class="metadata-label">ID:</span> {job_id or 'N/A'}</div>
                            <div class="metadata-item"><span class="metadata-label">Project:</span> {PROJECT_CONFIG.project_name}</div>
                            <div class="metadata-item"><span class="metadata-label">Root:</span> {ROOT}</div>
                        </div>
                    </div>
                </div>
                <div class="footer">
                    Automated notification from {PROJECT_CONFIG.notification_display_name}
                </div>
            </div>
        </body>
        </html>
        """

        # Plain text fallback
        plain_body = f"{title}\n\n"
        if summary:
            plain_body += f"Accomplishment:\n{summary}\n\n"
        plain_body += f"{message}\n\n"
        if job_id:
            plain_body += f"Job ID: {job_id}\n"
        plain_body += f"Project: {PROJECT_CONFIG.project_name}\nRoot: {ROOT}\n"

        try:
            # Connect to SMTP
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=10)
            server.starttls()
            server.login(login_user, login_pass)

            for recipient in emails:
                msg = MIMEMultipart("alternative")
                msg["From"] = f"{display_name} <{from_email}>"
                msg["To"] = recipient
                msg["Subject"] = subject
                msg["Date"] = formatdate(localtime=True)
                
                msg.attach(MIMEText(plain_body, "plain"))
                msg.attach(MIMEText(html_body, "html"))
                
                print(f"      - Sending email to {recipient} via {provider}...", end="", flush=True)
                server.send_message(msg)
                print(" SENT.", flush=True)

            server.quit()
            print(f"      - SMTP connection to {provider} closed successfully.")
            return True
        except Exception as e:
            print(f"      - Failed to send email via {provider}: {e}")
            last_error = e
            continue
            
    if last_error:
        print(f"      - Email notification failed: {last_error}")
    else:
        print("      - Skipped email notification (no SMTP or API provider credentials configured in settings.json).")
    return False

def main() -> None:
    title = sys.argv[1] if len(sys.argv) > 1 else "AI workflow"
    message = sys.argv[2] if len(sys.argv) > 2 else "Notification"
    job_id = sys.argv[3] if len(sys.argv) > 3 else None
    summary = sys.argv[4] if len(sys.argv) > 4 else None
    success = notify(title, message, job_id, summary)
    if not success:
        sys.exit(1)

if __name__ == "__main__":
    main()
