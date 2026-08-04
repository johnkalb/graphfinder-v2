import os
import smtplib
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

def send_email(to_email: str, subject: str, html_content: str, text_content: Optional[str] = None) -> dict:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
    except ValueError:
        port = 587
    user = os.environ.get("SMTP_USER")
    pwd = os.environ.get("SMTP_PASS")
    from_addr = os.environ.get("TESTER_INVITE_FROM", "tester-support@sixdegrees.net")

    message_id = f"<{uuid.uuid4()}@{host}>"

    if not user or not pwd:
        return {
            "success": False,
            "error": "SMTP credentials not configured (SMTP_USER/SMTP_PASS)",
            "message_id": message_id
        }

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_email
        msg["Message-ID"] = message_id

        if not text_content:
            text_content = "Please view this email in an HTML-compatible client."

        msg.attach(MIMEText(text_content, "plain", "utf-8"))
        msg.attach(MIMEText(html_content, "html", "utf-8"))

        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=10)
        else:
            server = smtplib.SMTP(host, port, timeout=10)
            if port == 587 or host == "smtp.gmail.com":
                server.starttls()
        
        server.login(user, pwd)
        server.sendmail(from_addr, [to_email], msg.as_string())
        server.quit()
        return {
            "success": True,
            "message_id": message_id
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message_id": message_id
        }
