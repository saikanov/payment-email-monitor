"""Email Payment Monitor — checks IMAP inbox for payment emails and notifies Discord."""

import io
import logging
import os
import re
import sys
import time
from datetime import date, timedelta

import imgkit
import requests
from dotenv import load_dotenv
from imap_tools import MailBox, AND
from imap_tools.errors import MailboxLoginError

from storage import is_processed, mark_processed

load_dotenv()

# --- Logging ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)-7s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("email-monitor")

# --- Config ---
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.gmail.com")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes")
wise_account_number = os.getenv("WISE_ACCOUNT_NUMBER", "")

# --- Amount regex: matches numbers like 1,234.56 or 100 or 0.99 ---
AMOUNT_RE = re.compile(r"([\d,]+\.?\d*)")

# --- Currency symbols / codes ---
CURRENCY_RE = re.compile(r"(\$|€|£|¥|USD|EUR|GBP|JPY|BTC|ETH|USDT|USDC)", re.IGNORECASE)


def validate_config() -> bool:
    """Validate required configuration. Returns True if valid."""
    valid = True
    if not EMAIL_ADDRESS:
        logger.error("EMAIL_ADDRESS is not set in .env")
        valid = False
    if not EMAIL_PASSWORD:
        logger.error("EMAIL_PASSWORD is not set in .env")
        valid = False
    if not IMAP_SERVER:
        logger.error("IMAP_SERVER is not set in .env")
        valid = False
    if not DISCORD_WEBHOOK and not DRY_RUN:
        logger.warning(
            "DISCORD_WEBHOOK is not set — notifications will fail (use DRY_RUN=true to test)"
        )
    return valid


def detect_provider(sender: str, subject: str, body: str) -> str | None:
    """Detect payment provider from sender and subject (case-insensitive).

    Supports both English and Indonesian (Bahasa) email subjects.
    """
    sender_lower = sender.lower()
    subject_lower = subject.lower()

    # PayPal: sender contains "paypal" AND subject indicates received payment
    if "paypal" in sender_lower:
        return "PayPal"

    # Wise: sender contains "wise" AND subject indicates received money
    if "jago" in sender_lower and wise_account_number in body:
        return "Wise"

    # Crypto: subject contains deposit + confirmed
    if "binance" in subject_lower:
        return "Crypto"

    return None


def parse_amount(text: str) -> str:
    """Extract the first amount-like number from text."""
    match = AMOUNT_RE.search(text)
    return match.group(1) if match else "N/A"


def parse_currency(text: str) -> str:
    """Extract currency symbol or code from text."""
    match = CURRENCY_RE.search(text)
    return match.group(1).upper() if match else "N/A"


def parse_payment(
    sender: str, subject: str, body: str, message_id: str, provider: str
) -> dict:
    """Extract payment details into a simple dict."""
    combined = f"{subject} {body}"
    payment = {
        "provider": provider,
        "amount": parse_amount(combined),
        "currency": parse_currency(combined),
        "subject": subject,
        "sender": sender,
        "message_id": message_id,
    }
    logger.debug(
        "Parsed payment: provider=%s amount=%s %s",
        provider,
        payment["amount"],
        payment["currency"],
    )
    return payment


def html_to_image(html_body: str) -> bytes | None:
    """Convert an HTML string to PNG image bytes using imgkit."""
    try:
        options = {
            "format": "png",
            "width": "600",
            "quality": "100",
            "encoding": "UTF-8",
            "enable-local-file-access": "",
            "no-stop-slow-scripts": "",
            "load-error-handling": "ignore",
            "load-media-error-handling": "ignore",
            "javascript-delay": "0",
        }
        img_bytes = imgkit.from_string(html_body, False, options=options)
        logger.debug("HTML converted to image (%d bytes)", len(img_bytes))
        return img_bytes
    except Exception as e:
        logger.error("Failed to convert HTML to image: %s", e)
        return None


def send_discord_notification(payment: dict, html_body: str = "") -> None:
    """Send payment notification to Discord webhook with email screenshot."""
    message = (
        "💰 **PAYMENT RECEIVED**\n\n"
        f"**Provider:** {payment['provider']}\n"
        f"**From:** {payment['sender']}\n"
        f"**Subject:** {payment['subject']}"
    )

    if DRY_RUN:
        logger.info("[DRY RUN] Would send to Discord:\n%s", message)
        return

    if not DISCORD_WEBHOOK:
        logger.error("Cannot send notification — DISCORD_WEBHOOK is not set")
        return

    # Convert HTML body to image if available
    image_bytes = None
    if html_body:
        logger.debug("Converting email HTML to image...")
        image_bytes = html_to_image(html_body)

    logger.debug("Sending Discord webhook to %s...", DISCORD_WEBHOOK[:50])
    try:
        if image_bytes:
            # Send as multipart with image attachment
            payload = {"content": message}
            files = {
                "file": ("email.png", io.BytesIO(image_bytes), "image/png"),
                "payload_json": (
                    None,
                    __import__("json").dumps(payload),
                    "application/json",
                ),
            }
            response = requests.post(DISCORD_WEBHOOK, files=files, timeout=15)
        else:
            # Fallback: text-only
            response = requests.post(
                DISCORD_WEBHOOK, json={"content": message}, timeout=10
            )

        if response.status_code in (200, 204):
            logger.info("Discord notification sent for %s payment", payment["provider"])
        else:
            logger.warning(
                "Discord returned unexpected status %d: %s",
                response.status_code,
                response.text[:200],
            )
    except requests.ConnectionError:
        logger.error("Failed to connect to Discord — check your internet connection")
    except requests.Timeout:
        logger.error("Discord webhook request timed out")
    except requests.RequestException as e:
        logger.error("Discord webhook request failed: %s", e)


def check_emails() -> None:
    """Connect to IMAP, check UNSEEN emails, detect payments, and notify."""
    logger.info("Connecting to %s as %s ...", IMAP_SERVER, EMAIL_ADDRESS)

    try:
        with MailBox(IMAP_SERVER).login(EMAIL_ADDRESS, EMAIL_PASSWORD) as mailbox:
            logger.info("Login successful")
            since_date = date.today() - timedelta(days=7)
            logger.debug("Fetching UNSEEN emails since %s...", since_date)

            count = 0
            detected = 0
            for msg in mailbox.fetch(AND(seen=False, date_gte=since_date)):
                count += 1
                sender = msg.from_ or ""
                subject = msg.subject or ""
                body = msg.text or msg.html or ""
                html_body = msg.html or ""
                message_id = (
                    msg.headers.get("message-id", [""])[0] if msg.headers else ""
                )

                logger.debug(
                    "Email #%d: from=%s subject='%s' id=%s",
                    count,
                    sender,
                    subject[:60],
                    message_id[:40],
                )

                if not message_id:
                    logger.warning(
                        "Skipping email with no message-id: subject='%s'", subject
                    )
                    continue

                if is_processed(message_id):
                    logger.debug("Already processed, skipping: %s", message_id[:40])
                    continue

                provider = detect_provider(sender, subject, body)
                if provider is None:
                    logger.debug(
                        "Not a payment email, skipping: subject='%s'", subject[:60]
                    )
                    continue

                detected += 1
                logger.info(
                    "DETECTED %s payment from %s — subject='%s'",
                    provider,
                    sender,
                    subject,
                )
                payment = parse_payment(sender, subject, body, message_id, provider)
                send_discord_notification(payment, html_body)
                mark_processed(message_id)
                logger.info("Marked as processed: %s", message_id[:40])

            logger.info(
                "Done — %d email(s) checked, %d payment(s) detected", count, detected
            )

    except MailboxLoginError as e:
        logger.error(
            "IMAP login failed for %s on %s — %s",
            EMAIL_ADDRESS,
            IMAP_SERVER,
            e,
        )
        logger.error(
            "HINT: If using Gmail, you need an App Password. "
            "Go to https://myaccount.google.com/apppasswords to generate one. "
            "Regular passwords won't work if you have 2FA enabled."
        )
    except ConnectionRefusedError:
        logger.error(
            "Connection refused by %s — is the IMAP server address correct?",
            IMAP_SERVER,
        )
    except TimeoutError:
        logger.error(
            "Connection to %s timed out — check your network or firewall", IMAP_SERVER
        )
    except OSError as e:
        logger.error("Network error connecting to %s: %s", IMAP_SERVER, e)
    except Exception as e:
        logger.error("Unexpected error: %s: %s", type(e).__name__, e, exc_info=True)


def main() -> None:
    """Main worker loop."""
    logger.info("=" * 50)
    logger.info("  Email Payment Monitor")
    logger.info("=" * 50)
    logger.info("  IMAP Server  : %s", IMAP_SERVER)
    logger.info("  Email        : %s", EMAIL_ADDRESS)
    logger.info("  Poll Interval: %ds", POLL_INTERVAL)
    logger.info("  Dry Run      : %s", DRY_RUN)
    logger.info("  Discord      : %s", "SET" if DISCORD_WEBHOOK else "NOT SET")
    logger.info("=" * 50)

    if not validate_config():
        logger.critical("Configuration invalid — fix your .env file and restart")
        sys.exit(1)

    logger.info("Starting email monitor loop...")

    while True:
        check_emails()
        logger.info("Sleeping %ds until next check...", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
