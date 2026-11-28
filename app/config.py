import os

from dotenv import load_dotenv

load_dotenv()


def _db_url():
    url = os.getenv("DATABASE_URL", "sqlite:///eliberia_dev.db")
    # Render / Neon give postgres:// or postgresql://; force the psycopg2 driver we install.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg2://" + url[len(prefix):]
            break
    return url


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    # key for encrypting personal data at rest; falls back to SECRET_KEY. Never change it once data exists.
    DATA_KEY = os.getenv("DATA_KEY") or None
    SQLALCHEMY_DATABASE_URI = _db_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # The live site origins are in the default, so a missing CORS_ORIGINS
    # value never locks the live site out. A value set in the Render
    # dashboard still wins, so keep it in sync there too.
    CORS_ORIGINS = [o.strip() for o in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:3000,https://portagov.giin.co.rw,https://portagov-frontend.vercel.app").split(",")]
    UPLOAD_DIR = os.getenv(
        "UPLOAD_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads"))
    MAX_CONTENT_LENGTH = 8 * 1024 * 1024
    TOKEN_HOURS = int(os.getenv("TOKEN_HOURS", "12"))
    # When True, OTP codes are returned in API responses (development only!)
    EXPOSE_OTP = os.getenv("EXPOSE_OTP", "True").lower() == "true"
    # staff, admins and agents confirm sign-in with a one-time code
    STAFF_2FA = os.getenv("STAFF_2FA", "True").lower() == "true"
    # shared secret the USSD gateway appends as ?key=...; without it the caller's number is not trusted
    USSD_SECRET = os.getenv("USSD_SECRET") or None
    PAYMENT_PROVIDER = os.getenv("PAYMENT_PROVIDER", "mock")  # mock | live
    PAYMENT_WEBHOOK_SECRET = os.getenv("PAYMENT_WEBHOOK_SECRET") or None
    MTN_MOMO_SUBSCRIPTION_KEY = os.getenv("MTN_MOMO_SUBSCRIPTION_KEY") or None
    MTN_MOMO_API_USER = os.getenv("MTN_MOMO_API_USER") or None
    MTN_MOMO_API_KEY = os.getenv("MTN_MOMO_API_KEY") or None
    MTN_MOMO_ENV = os.getenv("MTN_MOMO_ENV", "sandbox")
    MTN_MOMO_TARGET = os.getenv("MTN_MOMO_TARGET", "sandbox")
    MTN_MOMO_CURRENCY = os.getenv("MTN_MOMO_CURRENCY", "EUR")
    MTN_MOMO_CALLBACK_URL = os.getenv("MTN_MOMO_CALLBACK_URL") or None
    # SMS: log | africastalking | twilio  (see services/sms.py)
    SMS_PROVIDER = os.getenv("SMS_PROVIDER", "log")
    SMS_USERNAME = os.getenv("SMS_USERNAME") or None
    SMS_API_KEY = os.getenv("SMS_API_KEY") or None
    SMS_SENDER_ID = os.getenv("SMS_SENDER_ID") or None
    SMS_ACCOUNT_SID = os.getenv("SMS_ACCOUNT_SID") or None
    SMS_AUTH_TOKEN = os.getenv("SMS_AUTH_TOKEN") or None
    SMS_FROM = os.getenv("SMS_FROM") or None
    SMS_UPDATES = os.getenv("SMS_UPDATES", "True").lower() == "true"  # also text status updates
    # email: one-time codes go to the user's email address as well as by SMS.
    # Outbound mail goes through the Resend HTTPS API (free tier) when
    # RESEND_API_KEY is set, because outbound SMTP from Render free to the
    # StackMail mailbox times out. SMTP below stays as the fallback when no
    # Resend key is configured. The sender is MAIL_FROM (portagov@giin.co.rw);
    # Resend only sends from it once giin.co.rw is verified in Resend with
    # the DNS records Resend shows for the domain. Values are set in Render,
    # never in code.
    RESEND_API_KEY = os.getenv("RESEND_API_KEY") or None
    SMTP_HOST = os.getenv("SMTP_HOST") or None
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER = os.getenv("SMTP_USER") or None
    SMTP_PASS = os.getenv("SMTP_PASS") or None
    MAIL_FROM = os.getenv("MAIL_FROM", "portagov@giin.co.rw")
    # email inbox: the platform mailbox on ServerByt (StackMail). Values are set in Render,
    # never in code. Until IMAP_USER and IMAP_PASS are set, the inbox reports not connected.
    IMAP_HOST = os.getenv("IMAP_HOST", "imap.stackmail.com")
    IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
    IMAP_USER = os.getenv("IMAP_USER") or None
    IMAP_PASS = os.getenv("IMAP_PASS") or None
    # files: local | s3  (see services/storage.py)
    STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local")
    STORAGE_S3_BUCKET = os.getenv("STORAGE_S3_BUCKET") or None
    STORAGE_S3_ENDPOINT = os.getenv("STORAGE_S3_ENDPOINT") or None
    STORAGE_S3_ACCESS_KEY = os.getenv("STORAGE_S3_ACCESS_KEY") or None
    STORAGE_S3_SECRET_KEY = os.getenv("STORAGE_S3_SECRET_KEY") or None
    STORAGE_S3_REGION = os.getenv("STORAGE_S3_REGION", "auto")
    # demo accounts and first administrator
    DEMO_MODE = os.getenv("DEMO_MODE", os.getenv("EXPOSE_OTP", "True")).lower() == "true"
    BOOTSTRAP_ADMIN_PHONE = os.getenv("BOOTSTRAP_ADMIN_PHONE") or None
    BOOTSTRAP_ADMIN_NAME = os.getenv("BOOTSTRAP_ADMIN_NAME", "Platform Administrator")
    BOOTSTRAP_ADMIN_PASSWORD = os.getenv("BOOTSTRAP_ADMIN_PASSWORD") or None
    FORCE_PASSWORD_CHANGE = os.getenv("FORCE_PASSWORD_CHANGE", "True").lower() == "true"
    # legal and retention
    TERMS_VERSION = os.getenv("TERMS_VERSION", "2026-11-draft")
    DRAFT_RETENTION_DAYS = int(os.getenv("DRAFT_RETENTION_DAYS", "90"))
    NOTIFICATION_RETENTION_DAYS = int(os.getenv("NOTIFICATION_RETENTION_DAYS", "365"))
    TASKS_SECRET = os.getenv("TASKS_SECRET") or None
    # citizen services
    CENTER_SLOT_CAPACITY = int(os.getenv("CENTER_SLOT_CAPACITY", "5"))
    COURIER_FEE_USD = float(os.getenv("COURIER_FEE_USD", "5"))
    BIOMETRIC_PROVIDER = os.getenv("BIOMETRIC_PROVIDER", "mock")
