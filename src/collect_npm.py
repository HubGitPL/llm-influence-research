"""Collect npm ecosystem metrics from BigQuery deps.dev v1 → data/npm_meta/*.csv."""
import argparse
import csv
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
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
    "do not currently have an active account",
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
    token_str = result.stdout.strip()
    if not token_str:
        raise BQAPIError(0, "empty_token",
                         "gcloud auth print-access-token returned empty output — check gcloud session state")
    _token_cache["token"] = token_str
    _token_cache["fetched_at"] = time.time()
    return token_str


def _invalidate_token_cache() -> None:
    _token_cache["token"] = None
    _token_cache["fetched_at"] = 0.0


# --- checkpoint + CSV helpers (verbatim from collect_meta.py:53-78) ---------

def _load_status(out_dir: Path) -> dict:
    path = out_dir / "_status.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def checkpoint_exists(out_dir: Path, metric: str) -> bool:
    return _load_status(out_dir).get(metric) is True


def mark_done(out_dir: Path, metric: str):
    status = _load_status(out_dir)
    status[metric] = True
    (out_dir / "_status.json").write_text(json.dumps(status, indent=2))


def write_csv(filepath: Path, rows: list, fieldnames: list):
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# canonical on-disk vocabulary (NPMECO-06 / D-06)
STATUS_KEY = {"m1": "m1_new_packages", "m2": "m2_new_versions", "m3": "m3_cumulative"}
CSV_NAME = {
    "m1": "new_packages_per_year",
    "m2": "new_versions_per_year",
    "m3": "cumulative_packages_ever",
}
FIELDS = {
    "m1": ["year", "types", "other_scoped", "unscoped", "total"],
    "m2": ["year", "new_versions", "new_release_versions", "new_prerelease_versions"],
    "m3": ["year", "cumulative_packages_ever"],
}


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
    start_time = time.monotonic()
    while time.monotonic() - start_time < ASYNC_HARD_CAP_SECONDS:
        time.sleep(ASYNC_POLL_INTERVAL)
        poll_url = f"{BQ_BASE_URL}/{project_id}/queries/{job_id}?location=US"
        poll = _request_with_retry("GET", poll_url, None, token)
        if poll.get("jobComplete"):
            poll_bytes = int(poll.get("totalBytesProcessed", "0"))
            rows = _rows_from_response(poll)
            extra_rows, extra_bytes = _drain_pages(project_id, poll, token, job_id=job_id)
            return rows + extra_rows, poll_bytes + extra_bytes

    raise BQAPIError(0, "async_timeout", f"job {job_id} did not complete within {ASYNC_HARD_CAP_SECONDS}s")


_PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


def _validate_project_id(project_id: str) -> str:
    """Reject project-ids that don't match GCP rules (T-06-03)."""
    if not _PROJECT_ID_PATTERN.match(project_id):
        print(
            f"Error: invalid --project-id format: {project_id} "
            "(must match GCP project rules: lowercase, digits, hyphens, 6-30 chars)",
            file=sys.stderr,
        )
        sys.exit(2)
    return project_id


def _validate_snapshot_format(s: str) -> str:
    """Parse ISO-8601 before any SQL substitution (T-06-03)."""
    candidate = s[:-1] if s.endswith("Z") else s
    try:
        datetime.fromisoformat(candidate)
    except ValueError:
        print(f"Error: --snapshot-at must be ISO-8601 (got: {s})", file=sys.stderr)
        sys.exit(2)
    return s


def _bq_timestamp_to_iso(raw: str) -> str:
    """Normalize a BigQuery TIMESTAMP field to ISO-8601 UTC.

    BigQuery REST returns TIMESTAMP columns as float-seconds-since-epoch encoded
    as a string (e.g. "1730419200.000000"). Anything else (already ISO, datetime
    object) is also accepted so this helper is safe to apply at the boundary.
    """
    if isinstance(raw, datetime):
        dt = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    else:
        s = str(raw).strip()
        try:
            dt = datetime.fromtimestamp(float(s), tz=timezone.utc)
        except ValueError:
            candidate = s[:-1] + "+00:00" if s.endswith("Z") else s
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_snapshot(project_id: str, token: str, override=None) -> str:
    """Resolve a pinned SnapshotAt — auto-pick MAX, or validate user override."""
    if override:
        _validate_snapshot_format(override)
        # deps.dev Snapshots is a single-column TIMESTAMP table named "Time".
        probe_sql = (
            "SELECT 1 AS ok FROM `bigquery-public-data.deps_dev_v1.Snapshots` "
            "WHERE Time=@pin LIMIT 1"
        )
        params = [{
            "name": "pin",
            "parameterType": {"type": "TIMESTAMP"},
            "parameterValue": {"value": override},
        }]
        rows, _ = run_bq_query(project_id, probe_sql, params, token)
        if not rows:
            available_sql = (
                "SELECT Time AS s FROM `bigquery-public-data.deps_dev_v1.Snapshots` "
                "ORDER BY Time DESC LIMIT 10"
            )
            try:
                avail_rows, _ = run_bq_query(project_id, available_sql, [], token)
            except BQAPIError:
                avail_rows = []
            print(
                f"Error: --snapshot-at {override} not found in Snapshots table.",
                file=sys.stderr,
            )
            if avail_rows:
                print("Available snapshots (most recent first):", file=sys.stderr)
                for r in avail_rows:
                    print(f"  {r.get('s')}", file=sys.stderr)
            sys.exit(2)
        return override

    # deps.dev Snapshots is a single-column TIMESTAMP table named "Time".
    default_sql = (
        "SELECT MAX(Time) AS max_snapshot "
        "FROM `bigquery-public-data.deps_dev_v1.Snapshots`"
    )
    rows, _ = run_bq_query(project_id, default_sql, [], token)
    if not rows or rows[0].get("max_snapshot") is None:
        raise BQAPIError(0, "no_snapshots", "Snapshots table returned no rows")
    return _bq_timestamp_to_iso(rows[0]["max_snapshot"])


# --- M1: new packages per year, three-way scope split ----------------------
# D-01: query the raw PackageVersions table (not the "latest" view) and pin
# SnapshotAt=@pin so all M1/M2/M3 share one logical snapshot — research-reproducibility invariant.
# Pitfall 2: year bucket comes from UpstreamPublishedAt (publish time), NOT SnapshotAt.
# Pitfall 8: three-way split — @types/*, other @-scoped, unscoped — per NPMECO-03.
SQL_M1_NEW_PACKAGES = """
WITH first_seen AS (
  SELECT Name,
         MIN(UpstreamPublishedAt) AS first_publish,
         CASE
           WHEN STARTS_WITH(Name, '@types/') THEN 'types'
           WHEN STARTS_WITH(Name, '@')      THEN 'other_scoped'
           ELSE                                  'unscoped'
         END AS bucket
  FROM `bigquery-public-data.deps_dev_v1.PackageVersions`
  WHERE System = 'NPM' AND SnapshotAt = @pin AND UpstreamPublishedAt IS NOT NULL
  GROUP BY Name
)
SELECT EXTRACT(YEAR FROM first_publish) AS year,
       COUNTIF(bucket = 'types')        AS types,
       COUNTIF(bucket = 'other_scoped') AS other_scoped,
       COUNTIF(bucket = 'unscoped')     AS unscoped,
       COUNT(*)                         AS total
FROM first_seen
GROUP BY year
ORDER BY year
""".strip()


# Small companion query for NPMECO-07 audit field.
SQL_M1_NULL_COUNT = """
SELECT COUNT(*) AS null_count
FROM `bigquery-public-data.deps_dev_v1.PackageVersions`
WHERE System = 'NPM' AND SnapshotAt = @pin AND UpstreamPublishedAt IS NULL
""".strip()


def _pin_param(pinned_snapshot: str) -> list:
    """Build the @pin TIMESTAMP BigQuery parameter."""
    return [{
        "name": "pin",
        "parameterType": {"type": "TIMESTAMP"},
        "parameterValue": {"value": pinned_snapshot},
    }]


def collect_m1_new_packages(out_dir, project_id, token, pinned_snapshot,
                            force_cost_override, dry_run_only):
    """Run M1 end-to-end. Returns (bytes_processed, upstream_published_at_null_count)."""
    pin_param = _pin_param(pinned_snapshot)
    bytes_est = dry_run_bytes(project_id, SQL_M1_NEW_PACKAGES, pin_param, token)

    if dry_run_only:
        print(f"  m1: dry-run = {bytes_est / 1e9:.2f} GB")
        return (bytes_est, 0)

    assert_under_budget(bytes_est, force_cost_override)

    rows, bytes_used = run_bq_query(project_id, SQL_M1_NEW_PACKAGES, pin_param, token)
    null_rows, null_bytes = run_bq_query(project_id, SQL_M1_NULL_COUNT, pin_param, token)
    null_count = int(null_rows[0].get("null_count", 0)) if null_rows else 0

    write_csv(out_dir / f"{CSV_NAME['m1']}.csv", rows, FIELDS["m1"])
    mark_done(out_dir, STATUS_KEY["m1"])  # flip ONLY after CSV close (NPMECO-06)

    return (bytes_used + null_bytes, null_count)


# --- M2: new versions per year, release vs prerelease split ----------------
# FEATURES.md §Metric 2: COUNTIF over VersionInfo.IsRelease for the split.
# D-01: same SnapshotAt=@pin pin as M1 so all 3 CSVs share one logical snapshot.
# Pitfall 2: year bucket is UpstreamPublishedAt (publish time), not SnapshotAt.
SQL_M2_NEW_VERSIONS = """
SELECT EXTRACT(YEAR FROM UpstreamPublishedAt) AS year,
       COUNT(*)                               AS new_versions,
       COUNTIF(VersionInfo.IsRelease)         AS new_release_versions,
       COUNTIF(NOT VersionInfo.IsRelease)     AS new_prerelease_versions
FROM `bigquery-public-data.deps_dev_v1.PackageVersions`
WHERE System = 'NPM' AND SnapshotAt = @pin AND UpstreamPublishedAt IS NOT NULL
GROUP BY year
ORDER BY year
""".strip()


SQL_M2_NULL_COUNT = """
SELECT COUNT(*) AS null_count
FROM `bigquery-public-data.deps_dev_v1.PackageVersions`
WHERE System = 'NPM' AND SnapshotAt = @pin AND UpstreamPublishedAt IS NULL
""".strip()


def collect_m2_new_versions(out_dir, project_id, token, pinned_snapshot,
                            force_cost_override, dry_run_only):
    """Run M2 end-to-end. Returns (bytes_processed, upstream_published_at_null_count)."""
    pin_param = _pin_param(pinned_snapshot)
    bytes_est = dry_run_bytes(project_id, SQL_M2_NEW_VERSIONS, pin_param, token)

    if dry_run_only:
        print(f"  m2: dry-run = {bytes_est / 1e9:.2f} GB")
        return (bytes_est, 0)

    assert_under_budget(bytes_est, force_cost_override)

    rows, bytes_used = run_bq_query(project_id, SQL_M2_NEW_VERSIONS, pin_param, token)
    null_rows, null_bytes = run_bq_query(project_id, SQL_M2_NULL_COUNT, pin_param, token)
    null_count = int(null_rows[0].get("null_count", 0)) if null_rows else 0

    write_csv(out_dir / f"{CSV_NAME['m2']}.csv", rows, FIELDS["m2"])
    mark_done(out_dir, STATUS_KEY["m2"])

    return (bytes_used + null_bytes, null_count)


# --- M3: cumulative packages ever (lower bound, deps.dev unpublish gap) ----
# FEATURES.md §Metric 4: year × first_seen cross-join trick.
# @end_year is derived at runtime from the pinned snapshot's calendar year
# (Blocker #3) — keeps the time series alive across reruns in later years
# without exposing a user-facing year-range flag (NPMECO-08 surface unchanged).
# D-01: SnapshotAt=@pin inside first_seen CTE.
SQL_M3_CUMULATIVE_PACKAGES = """
WITH years AS (
  SELECT y FROM UNNEST(GENERATE_ARRAY(2010, @end_year)) AS y
),
first_seen AS (
  SELECT Name, MIN(UpstreamPublishedAt) AS first_publish
  FROM `bigquery-public-data.deps_dev_v1.PackageVersions`
  WHERE System = 'NPM' AND SnapshotAt = @pin AND UpstreamPublishedAt IS NOT NULL
  GROUP BY Name
)
SELECT y.y                                  AS year,
       COUNT(*)                             AS cumulative_packages_ever
FROM years y
JOIN first_seen f ON EXTRACT(YEAR FROM f.first_publish) <= y.y
GROUP BY y.y
ORDER BY y.y
""".strip()


SQL_M3_NULL_COUNT = """
SELECT COUNT(*) AS null_count
FROM `bigquery-public-data.deps_dev_v1.PackageVersions`
WHERE System = 'NPM' AND SnapshotAt = @pin AND UpstreamPublishedAt IS NULL
""".strip()


def _snapshot_year(pinned_snapshot: str) -> int:
    """Extract calendar year from an ISO-8601 snapshot timestamp."""
    candidate = pinned_snapshot
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    return datetime.fromisoformat(candidate).year


def collect_m3_cumulative_packages(out_dir, project_id, token, pinned_snapshot,
                                   force_cost_override, dry_run_only):
    """Run M3 end-to-end. Returns (bytes_processed, upstream_published_at_null_count)."""
    end_year = _snapshot_year(pinned_snapshot)
    pin_param = _pin_param(pinned_snapshot)
    end_year_param = {
        "name": "end_year",
        "parameterType": {"type": "INT64"},
        "parameterValue": {"value": str(end_year)},
    }
    main_params = pin_param + [end_year_param]

    bytes_est = dry_run_bytes(project_id, SQL_M3_CUMULATIVE_PACKAGES, main_params, token)

    if dry_run_only:
        print(f"  m3: dry-run = {bytes_est / 1e9:.2f} GB")
        return (bytes_est, 0)

    assert_under_budget(bytes_est, force_cost_override)

    rows, bytes_used = run_bq_query(
        project_id, SQL_M3_CUMULATIVE_PACKAGES, main_params, token,
    )
    null_rows, null_bytes = run_bq_query(
        project_id, SQL_M3_NULL_COUNT, pin_param, token,
    )
    null_count = int(null_rows[0].get("null_count", 0)) if null_rows else 0

    write_csv(out_dir / f"{CSV_NAME['m3']}.csv", rows, FIELDS["m3"])
    mark_done(out_dir, STATUS_KEY["m3"])

    return (bytes_used + null_bytes, null_count)


def _run_metric(metric, out_dir, project_id, token, pinned_snapshot,
                force_cost_override, dry_run_only):
    """Dispatch one metric run. Returns (bytes_used, null_count)."""
    if checkpoint_exists(out_dir, STATUS_KEY[metric]):
        print(f"  {metric}: skip (already done)")
        return (0, 0)
    if metric == "m1":
        return collect_m1_new_packages(
            out_dir, project_id, token, pinned_snapshot,
            force_cost_override, dry_run_only,
        )
    if metric == "m2":
        return collect_m2_new_versions(
            out_dir, project_id, token, pinned_snapshot,
            force_cost_override, dry_run_only,
        )
    if metric == "m3":
        return collect_m3_cumulative_packages(
            out_dir, project_id, token, pinned_snapshot,
            force_cost_override, dry_run_only,
        )
    raise ValueError(f"unknown metric: {metric}")


def assert_under_budget(bytes_estimate: int, force_override: bool) -> None:
    """50 GB dry-run cost gate (NPMECO-02 / T-06-02)."""
    if bytes_estimate <= DRY_RUN_BYTE_THRESHOLD:
        return
    gb = bytes_estimate / 1e9
    if force_override:
        print(
            f"  WARN: dry-run estimate {gb:.2f} GB exceeds 50 GB threshold — "
            "running because --force-cost-override is set",
            file=sys.stderr,
        )
        return
    print(
        f"Error: dry-run estimate {gb:.2f} GB exceeds 50 GB threshold. "
        "Re-run with --force-cost-override if intentional.",
        file=sys.stderr,
    )
    sys.exit(2)


def normalize_metric(metric):
    """Map user-facing alias to canonical m1/m2/m3, or None for 'run all'."""
    if metric is None or metric == "all":
        return None
    mapping = {"new-packages": "m1", "new-versions": "m2", "cumulative": "m3"}
    return mapping.get(metric, metric)


def parse_args(args=None):
    parser = argparse.ArgumentParser(
        description="Collect npm ecosystem metrics from BigQuery deps.dev."
    )
    parser.add_argument(
        "--project-id",
        required=True,
        type=_validate_project_id,
        help="GCP project id for BigQuery jobs.query (billing/quota target)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore _status.json and re-run all metrics",
    )
    parser.add_argument(
        "--metric",
        choices=["m1", "new-packages", "m2", "new-versions", "m3", "cumulative", "all"],
        default=None,
        help="Run only one metric (omit or 'all' = run all 3 in sequence)",
    )
    parser.add_argument(
        "--dry-run-only",
        action="store_true",
        help="Print byte-scan estimate per query without executing",
    )
    parser.add_argument(
        "--snapshot-at",
        default=None,
        help="Override pinned SnapshotAt (ISO-8601). Default: MAX(SnapshotAt) WHERE System='NPM'",
    )
    parser.add_argument(
        "--force-cost-override",
        action="store_true",
        help="Run queries whose dry-run estimate exceeds 50 GB (default: abort)",
    )
    return parser.parse_args(args)


def write_run_meta(out_dir: Path, pinned_snapshot: str, project_id: str,
                   bytes_per_metric: dict, null_counts_per_metric: dict) -> None:
    """Write the per-run audit trail to meta.json (NPMECO-07)."""
    payload = {
        "run_timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "pinned_snapshot_at": pinned_snapshot,
        "project_id": project_id,
        "total_bytes_processed": sum(bytes_per_metric.values()),
        "bytes_per_metric": bytes_per_metric,
        "upstream_published_at_null_count": null_counts_per_metric,
    }
    (out_dir / "meta.json").write_text(json.dumps(payload, indent=2))


def main() -> None:
    args = parse_args()
    out_dir = BASE_OUT
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.force:
        (out_dir / "_status.json").unlink(missing_ok=True)

    token = _get_access_token()
    pinned_snapshot = resolve_snapshot(args.project_id, token, args.snapshot_at)

    metric = normalize_metric(args.metric)
    targets = [metric] if metric else ["m1", "m2", "m3"]

    bytes_per_metric: dict = {}
    null_counts_per_metric: dict = {}

    for m in targets:
        try:
            bytes_used, null_count = _run_metric(
                m, out_dir, args.project_id, token, pinned_snapshot,
                args.force_cost_override, args.dry_run_only,
            )
        except BQAPIError as e:
            # D-07 #3: classified BigQuery error → human-friendly hint + exit 2.
            # D-08: unclassified errors propagate so the user sees the raw BQ error.
            hint = _classify_bq_error(e.http_code, e.reason, e.message, args.project_id)
            if hint is not None:
                print(f"Error: {hint}", file=sys.stderr)
                sys.exit(2)
            raise
        if not args.dry_run_only and bytes_used > 0:
            bytes_per_metric[m] = bytes_used
            null_counts_per_metric[m] = null_count

    # Warning #8: always rewrite meta.json on non-dry-run invocations, even no-ops,
    # so every run has an audit trail (NPMECO-07 "audit each run").
    if not args.dry_run_only:
        write_run_meta(
            out_dir, pinned_snapshot, args.project_id,
            bytes_per_metric, null_counts_per_metric,
        )

    print(
        f"Gotowe: {len(bytes_per_metric)} metryk wykonanych w tym uruchomieniu → {out_dir}/"
    )


if __name__ == "__main__":
    main()
