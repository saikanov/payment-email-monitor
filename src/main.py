"""Email Payment Monitor — checks IMAP inbox for payment emails and notifies Discord."""

import io
import json
import logging
import os
import re
import signal
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
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes")
wise_account_number = os.getenv("WISE_ACCOUNT_NUMBER", "")

# --- Graceful shutdown ---
shutdown = False


def handle_signal(sig, frame):
    """Handle termination signals for graceful shutdown."""
    global shutdown
    logger.info("Shutdown signal received (sig=%s), finishing current cycle...", sig)
    shutdown = True


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


def get_accounts() -> list[dict]:
    """Retrieve list of email accounts from environment variables."""
    accounts = []

    # Primary
    primary_email = os.getenv("EMAIL_ADDRESS", "")
    primary_pass = os.getenv("EMAIL_PASSWORD", "")
    if primary_email and primary_pass:
        accounts.append(
            {
                "email": primary_email,
                "password": primary_pass,
                "server": os.getenv("IMAP_SERVER", "imap.gmail.com"),
            }
        )

    # Additional
    for i in range(2, 20):
        email = os.getenv(f"EMAIL_ADDRESS_{i}", "")
        password = os.getenv(f"EMAIL_PASSWORD_{i}", "")
        server = os.getenv(f"IMAP_SERVER_{i}", "imap.gmail.com")
        if email and password:
            accounts.append({"email": email, "password": password, "server": server})

    return accounts


# --- Amount regex: matches numbers like 1,234.56 or 100 or 0.99 ---
AMOUNT_RE = re.compile(r"([\d,]+\.?\d*)")

# --- Currency symbols / codes ---
CURRENCY_RE = re.compile(r"(\$|€|£|¥|USD|EUR|GBP|JPY|BTC|ETH|USDT|USDC)", re.IGNORECASE)


class AccountConnection:
    """Manages a persistent IMAP connection for one email account."""

    def __init__(self, account: dict):
        self.account = account
        self.mailbox: MailBox | None = None

    @property
    def email(self) -> str:
        return self.account["email"]

    @property
    def server(self) -> str:
        return self.account["server"]

    def connect(self) -> MailBox:
        """Return existing connection or create a new one. Auto-reconnects on failure."""
        if self.mailbox is not None:
            try:
                self.mailbox.client.noop()  # health check
                return self.mailbox
            except Exception:
                logger.warning("Connection stale for %s, reconnecting...", self.email)
                self._close()

        logger.info("Connecting to %s as %s ...", self.server, self.email)
        self.mailbox = MailBox(self.server).login(
            self.account["email"], self.account["password"]
        )
        logger.info("Login successful for %s", self.email)
        return self.mailbox

    def _close(self):
        """Safely close the IMAP connection."""
        try:
            if self.mailbox:
                self.mailbox.logout()
        except Exception:
            pass
        self.mailbox = None

    def close(self):
        """Public close method for clean shutdown."""
        self._close()
        logger.debug("Connection closed for %s", self.email)


def validate_config() -> bool:
    """Validate required configuration. Returns True if valid."""
    valid = True
    accounts = get_accounts()
    if not accounts:
        logger.error(
            "No valid email accounts configured in .env (need at least EMAIL_ADDRESS and EMAIL_PASSWORD)"
        )
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

    # PayPal: sender contains "paypal" AND subject indicates a payment
    if "paypal" in sender_lower and any(
        kw in subject_lower
        for kw in ["received", "payment", "sent you", "diterima", "pembayaran"]
    ):
        return "PayPal"

    # Bank Jago (Wise transfer destination): sender is Jago AND body contains the account number
    if "jago" in sender_lower and wise_account_number and wise_account_number in body:
        return "Wise"

    # Crypto: Binance deposit/transfer notifications
    if "binance" in sender_lower and any(
        kw in subject_lower for kw in ["deposit", "transfer", "received", "confirmed"]
    ):
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
        f"💰 **Email From {payment['provider']}**\n\n"
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
                    json.dumps(payload),
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


def check_emails(conn: AccountConnection) -> None:
    """Check UNSEEN emails via persistent connection, detect payments, and notify."""
    try:
        mailbox = conn.connect()
    except MailboxLoginError as e:
        logger.error(
            "IMAP login failed for %s on %s — %s",
            conn.email,
            conn.server,
            e,
        )
        logger.error(
            "HINT: If using Gmail, you need an App Password. "
            "Go to https://myaccount.google.com/apppasswords to generate one. "
            "Regular passwords won't work if you have 2FA enabled."
        )
        return
    except ConnectionRefusedError:
        logger.error(
            "Connection refused by %s — is the IMAP server address correct?",
            conn.server,
        )
        return
    except TimeoutError:
        logger.error(
            "Connection to %s timed out — check your network or firewall", conn.server
        )
        return
    except OSError as e:
        logger.error("Network error connecting to %s: %s", conn.server, e)
        return
    except Exception as e:
        logger.error("Unexpected error connecting to %s: %s: %s", conn.server, type(e).__name__, e, exc_info=True)
        return

    try:
        since_date = date.today() - timedelta(days=7)
        logger.debug("Fetching UNSEEN emails since %s for %s...", since_date, conn.email)

        count = 0
        detected = 0
        for msg in mailbox.fetch(AND(seen=False, date_gte=since_date), mark_seen=False):
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

            # Mark as seen on the server only after successful processing
            try:
                mailbox.seen(msg.uid)
            except Exception as e:
                logger.warning("Failed to mark email as seen (uid=%s): %s", msg.uid, e)

            logger.info("Marked as processed: %s", message_id[:40])

        logger.info(
            "Done — %d email(s) checked, %d payment(s) detected for %s",
            count,
            detected,
            conn.email,
        )

    except Exception as e:
        logger.error(
            "Error fetching emails for %s: %s: %s",
            conn.email,
            type(e).__name__,
            e,
            exc_info=True,
        )
        # Force reconnect on next cycle
        conn._close()


def main() -> None:
    """Main worker loop with persistent connections and graceful shutdown."""
    logger.info("=" * 50)
    logger.info("  Email Payment Monitor")
    logger.info("=" * 50)
    accounts = get_accounts()
    logger.info("  Configured Accounts : %d", len(accounts))
    for i, acc in enumerate(accounts, start=1):
        logger.info("  Account %d          : %s (%s)", i, acc["email"], acc["server"])
    logger.info("  Poll Interval       : %ds", POLL_INTERVAL)
    logger.info("  Dry Run             : %s", DRY_RUN)
    logger.info("  Discord             : %s", "SET" if DISCORD_WEBHOOK else "NOT SET")
    logger.info("=" * 50)

    if not validate_config():
        logger.critical("Configuration invalid — fix your .env file and restart")
        sys.exit(1)

    # Create persistent connections (loaded once, not every loop)
    connections = [AccountConnection(acc) for acc in accounts]

    logger.info("Starting email monitor loop...")

    try:
        while not shutdown:
            for conn in connections:
                if shutdown:
                    break
                check_emails(conn)
            if not shutdown:
                logger.info("Sleeping %ds until next check...", POLL_INTERVAL)
                time.sleep(POLL_INTERVAL)
    finally:
        logger.info("Shutting down — closing all connections...")
        for conn in connections:
            conn.close()
        logger.info("All connections closed. Goodbye!")


if __name__ == "__main__":
    main()
