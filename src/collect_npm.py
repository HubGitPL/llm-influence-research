"""Collect npm ecosystem metrics from BigQuery deps.dev v1 → data/npm_meta/*.csv."""
import json
import subprocess
import sys
import time
from pathlib import Path


class BQAPIError(Exception):
    def __init__(self, http_code: int, reason: str, message: str):
        self.http_code = http_code
        self.reason = reason
        self.message = message
        super().__init__(f"HTTP {http_code} [{reason}]: {message}")


BQ_BASE_URL = "https://bigquery.googleapis.com/bigquery/v2/projects"
BASE_OUT = Path("data/npm_meta")
DRY_RUN_BYTE_THRESHOLD = 50 * 1024 ** 3
TOKEN_TTL_SECONDS = 3000
MAX_ATTEMPTS = 6
BACKOFF_BASE = 10
BACKOFF_CAP = 600


_token_cache = {"token": None, "fetched_at": 0.0}


_AUTH_ERROR_HINTS = (
    "no currently active account",
    "reauthentication required",
    "no credentials",
    "not logged in",
)


def _get_access_token() -> str:
    """Mint or reuse a cached gcloud OAuth bearer token (50 min TTL)."""
    if _token_cache["token"] and time.time() - _token_cache["fetched_at"] < TOKEN_TTL_SECONDS:
        return _token_cache["token"]
    try:
        result = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        print(
            "Error: gcloud SDK not installed. Install: https://cloud.google.com/sdk/install",
            file=sys.stderr,
        )
        sys.exit(2)
    if result.returncode != 0:
        stderr_lower = result.stderr.lower()
        if any(s in stderr_lower for s in _AUTH_ERROR_HINTS):
            print("Error: not authenticated. Run: gcloud auth login", file=sys.stderr)
            sys.exit(2)
        raise BQAPIError(0, "gcloud_failed", result.stderr.strip())
    _token_cache["token"] = result.stdout.strip()
    _token_cache["fetched_at"] = time.time()
    return _token_cache["token"]


def _invalidate_token_cache() -> None:
    _token_cache["token"] = None
    _token_cache["fetched_at"] = 0.0


def _classify_bq_error(http_code: int, reason: str, body: str, project_id: str):
    """Return an actionable hint for the three D-07 classified branches, or None."""
    if http_code == 403 and reason == "accessDenied" and "BigQuery API has not been used" in body:
        return (
            f"BigQuery API not enabled for project {project_id}. "
            f"Enable: gcloud services enable bigquery.googleapis.com --project={project_id}"
        )
    return None
