"""Shared helpers for all collectors (GitHub API, CSV, checkpoints)."""
import csv
import json
import re
import subprocess
import sys
import time
from pathlib import Path


# --- Exceptions ---

class GHAPIError(Exception):
    def __init__(self, http_code: int, message: str):
        self.http_code = http_code
        self.message = message
        super().__init__(f"HTTP {http_code}: {message}")


class BQAPIError(Exception):
    def __init__(self, http_code: int, reason: str, message: str):
        self.http_code = http_code
        self.reason = reason
        self.message = message
        super().__init__(f"HTTP {http_code} [{reason}]: {message}")


# --- JSON parsing helpers ---

def _parse_http_code(stderr: str) -> int:
    m = re.search(r"HTTP (\d+)", stderr)
    return int(m.group(1)) if m else 0


def _parse_error_msg(stdout: str) -> str:
    try:
        return json.loads(stdout).get("message", stdout)
    except Exception:
        return stdout


def _parse_json_output(stdout: str):
    """Parse stdout from gh api — handles concatenated JSON arrays from --paginate."""
    raw = stdout.strip()
    if not raw:
        return []
    decoder = json.JSONDecoder()
    items = []
    idx = 0
    while idx < len(raw):
        while idx < len(raw) and raw[idx] in " \t\n\r":
            idx += 1
        if idx >= len(raw):
            break
        obj, end_idx = decoder.raw_decode(raw, idx)
        if isinstance(obj, list):
            items.extend(obj)
        elif isinstance(obj, dict):
            return obj
        idx = end_idx
    return items


# --- GitHub rate limit + retry ---

_api_call_count = 0


def check_rate_limit(warn_if_below: int = 100, auto_sleep: bool = True):
    result = subprocess.run(
        ["gh", "api", "/rate_limit"], capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  check_rate_limit: nie udało się sprawdzić ({result.stderr.strip()})", file=sys.stderr)
        return
    try:
        core = json.loads(result.stdout)["resources"]["core"]
    except (json.JSONDecodeError, KeyError):
        print("  check_rate_limit: nieoczekiwana odpowiedź API", file=sys.stderr)
        return
    if core["remaining"] < warn_if_below and auto_sleep:
        sleep_secs = max(core["reset"] - int(time.time()), 0) + 60
        print(f"  rate limit: {core['remaining']} pozostało, czekam {sleep_secs}s")
        time.sleep(sleep_secs)


def run_gh_with_retry(url: str, paginate: bool = False):
    """Call gh api with retry for 403/502/504."""
    global _api_call_count
    max_attempts = 6
    backoff = 10
    attempt = 0

    while attempt < max_attempts:
        _api_call_count += 1
        if _api_call_count % 200 == 0:
            check_rate_limit(warn_if_below=100, auto_sleep=True)

        cmd = ["gh", "api", url]
        if paginate:
            cmd.append("--paginate")
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0:
            return _parse_json_output(result.stdout)

        http_code = _parse_http_code(result.stderr)
        msg = _parse_error_msg(result.stdout)
        if not msg.strip() and result.stderr.strip():
            msg = result.stderr.strip()

        if http_code == 403 and "rate limit" in msg.lower():
            rl_result = subprocess.run(
                ["gh", "api", "/rate_limit"], capture_output=True, text=True
            )
            try:
                rl = json.loads(rl_result.stdout)["resources"]["core"]
                sleep_secs = max(rl["reset"] - int(time.time()), 0) + 60
            except (json.JSONDecodeError, KeyError):
                sleep_secs = 3600
            print(f"  403 rate limit, czekam {sleep_secs}s")
            time.sleep(sleep_secs)
        elif http_code in (0, 502, 504):
            attempt += 1
            if attempt >= max_attempts:
                raise GHAPIError(http_code, msg)
            sleep_secs = min(backoff * (2 ** (attempt - 1)), 600)
            label = "błąd sieciowy" if http_code == 0 else f"HTTP {http_code}"
            print(f"  {label}, backoff {sleep_secs}s (próba {attempt}/{max_attempts})")
            time.sleep(sleep_secs)
        else:
            raise GHAPIError(http_code, msg)

    raise GHAPIError(0, "max retry exceeded")


# --- CSV writer ---

def write_csv(filepath: Path, rows: list, fieldnames: list):
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# --- Checkpoint helpers (_status.json) ---

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
