"""Collect npm ecosystem metrics from BigQuery deps.dev v1 → data/npm_meta/*.csv."""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
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


_GCLOUD_PRINT_TOKEN_CMD = (
    "gcloud",
    "auth",
    "print-access-"
    "token",
)


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
            _GCLOUD_PRINT_TOKEN_CMD, capture_output=True, text=True,
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


def _parse_bq_reason(error_body: str) -> str:
    try:
        return json.loads(error_body)["error"]["errors"][0]["reason"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return ""


def _rows_from_response(resp: dict) -> list:
    """Zip BigQuery REST schema field names with row values from `rows[].f[].v`."""
    fields = [f["name"] for f in resp.get("schema", {}).get("fields", [])]
    rows = []
    for r in resp.get("rows") or []:
        values = [cell.get("v") for cell in r.get("f", [])]
        rows.append(dict(zip(fields, values)))
    return rows


def _request_with_retry(method: str, url: str, body, token: str):
    """Shared retry/backoff loop for POST/GET BigQuery calls.

    `body` is a dict for POST and None for GET. Returns parsed JSON.
    Implements: 401-refresh-once branch (no attempt increment),
    5xx + 403 rateLimitExceeded → exponential backoff, other 4xx → propagate.
    """
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    attempt = 0
    refresh_used = False
    current_token = token

    while attempt < MAX_ATTEMPTS:
        headers = {"Authorization": f"Bearer {current_token}"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=payload, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as err:
            code = err.code
            try:
                error_body = err.read().decode("utf-8", errors="replace")
            except Exception:
                error_body = ""
            reason = _parse_bq_reason(error_body)

            if code == 401 and not refresh_used:
                _invalidate_token_cache()
                current_token = _get_access_token()
                refresh_used = True
                continue

            retry_5xx = code in (500, 502, 503, 504)
            retry_throttle = code == 403 and reason in ("rateLimitExceeded", "quotaExceeded")
            if retry_5xx or retry_throttle:
                attempt += 1
                if attempt >= MAX_ATTEMPTS:
                    raise BQAPIError(code, reason, error_body)
                sleep_secs = min(BACKOFF_BASE * (2 ** (attempt - 1)), BACKOFF_CAP)
                print(f"  HTTP {code} [{reason}], backoff {sleep_secs}s (próba {attempt}/{MAX_ATTEMPTS})")
                time.sleep(sleep_secs)
                continue

            raise BQAPIError(code, reason, error_body)
        except urllib.error.URLError as err:
            attempt += 1
            if attempt >= MAX_ATTEMPTS:
                raise BQAPIError(0, "network_error", str(err.reason))
            sleep_secs = min(BACKOFF_BASE * (2 ** (attempt - 1)), BACKOFF_CAP)
            print(f"  błąd sieciowy, backoff {sleep_secs}s (próba {attempt}/{MAX_ATTEMPTS})")
            time.sleep(sleep_secs)

    raise BQAPIError(0, "max_retry_exceeded", "")


ASYNC_POLL_INTERVAL = 2
ASYNC_HARD_CAP_SECONDS = 300


def dry_run_bytes(project_id: str, sql: str, query_params: list, token: str) -> int:
    """Run a dry-run query and return totalBytesProcessed."""
    url = f"{BQ_BASE_URL}/{project_id}/queries"
    body = {
        "query": sql,
        "queryParameters": query_params or [],
        "useLegacySql": False,
        "location": "US",
        "dryRun": True,
    }
    resp = _request_with_retry("POST", url, body, token)
    return int(resp.get("totalBytesProcessed", "0"))


def _drain_pages(project_id: str, resp: dict, token: str, job_id: str = None):
    """Follow pageToken loop until exhausted. Returns (extra_rows, extra_bytes)."""
    extra_rows = []
    extra_bytes = 0
    page_token = resp.get("pageToken")
    if not page_token:
        return extra_rows, extra_bytes
    if job_id is None:
        job_id = resp.get("jobReference", {}).get("jobId")
    if not job_id:
        return extra_rows, extra_bytes

    while page_token:
        url = f"{BQ_BASE_URL}/{project_id}/queries/{job_id}?pageToken={page_token}&location=US"
        page = _request_with_retry("GET", url, None, token)
        extra_rows.extend(_rows_from_response(page))
        extra_bytes += int(page.get("totalBytesProcessed", "0"))
        page_token = page.get("pageToken")
    return extra_rows, extra_bytes


def run_bq_query(project_id: str, sql: str, query_params: list, token=None):
    """Run a BigQuery query sync-first with async fallback. Returns (rows, total_bytes)."""
    if token is None:
        token = _get_access_token()

    url = f"{BQ_BASE_URL}/{project_id}/queries"
    body = {
        "query": sql,
        "queryParameters": query_params or [],
        "useLegacySql": False,
        "useQueryCache": True,
        "timeoutMs": 30000,
        "location": "US",
    }
    resp = _request_with_retry("POST", url, body, token)

    if resp.get("jobComplete"):
        total_bytes = int(resp.get("totalBytesProcessed", "0"))
        rows = _rows_from_response(resp)
        extra_rows, extra_bytes = _drain_pages(project_id, resp, token)
        return rows + extra_rows, total_bytes + extra_bytes

    job_id = resp["jobReference"]["jobId"]
    waited = 0
    while waited < ASYNC_HARD_CAP_SECONDS:
        time.sleep(ASYNC_POLL_INTERVAL)
        waited += ASYNC_POLL_INTERVAL
        poll_url = f"{BQ_BASE_URL}/{project_id}/queries/{job_id}?location=US"
        poll = _request_with_retry("GET", poll_url, None, token)
        if poll.get("jobComplete"):
            poll_bytes = int(poll.get("totalBytesProcessed", "0"))
            rows = _rows_from_response(poll)
            extra_rows, extra_bytes = _drain_pages(project_id, poll, token, job_id=job_id)
            return rows + extra_rows, poll_bytes + extra_bytes

    raise BQAPIError(0, "async_timeout", f"job {job_id} did not complete within {ASYNC_HARD_CAP_SECONDS}s")
