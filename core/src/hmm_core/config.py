import os


def public_url() -> str:
    return os.environ.get("PUBLIC_URL", "http://localhost:8000").rstrip("/")
