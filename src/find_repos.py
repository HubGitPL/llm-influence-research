"""Find OSS repositories on GitHub matching given criteria."""
import argparse
import csv
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


class GHAPIError(Exception):
    def __init__(self, http_code: int, message: str):
        self.http_code = http_code
        self.message = message
        super().__init__(f"HTTP {http_code}: {message}")


STAR_BREAKPOINTS = [1000, 5000, 10000, 50000]
MAX_SEARCH_PAGE = 10
MAX_STARS_CAP = 500_000
REPO_FIELDS = ['owner', 'repo', 'stars', 'forks', 'language', 'created_at', 'contributors_count']
EARLIEST_YEAR = 2008


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


def check_search_rate_limit(auto_sleep: bool = True):
    result = subprocess.run(["gh", "api", "/rate_limit"], capture_output=True, text=True)
    if result.returncode != 0:
        return
    try:
        search = json.loads(result.stdout)["resources"]["search"]
    except (json.JSONDecodeError, KeyError):
        return
    if search["remaining"] < 5 and auto_sleep:
        sleep_secs = max(search["reset"] - int(time.time()), 0) + 5
        print(f"  search rate limit: {search['remaining']} pozostało, czekam {sleep_secs}s")
        time.sleep(sleep_secs)


_api_call_count = 0


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


def write_csv(filepath: Path, rows: list, fieldnames: list):
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_args(args=None):
    parser = argparse.ArgumentParser(description="Find OSS repositories on GitHub.")
    parser.add_argument('--language', required=True, help="Programming language to search for")
    parser.add_argument('--min-stars', type=int, required=True, help="Minimum number of stars")
    parser.add_argument('--min-contributors', type=int, required=True, help="Minimum number of contributors")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--since', type=int, help="lower bound for active year (last push >= YEAR; collect_meta uses commit data from YEAR)")
    group.add_argument('--until', type=int, help=f"End year (exclusive) for creation window (repos created from {EARLIEST_YEAR}-01-01 to (UNTIL-1)-12-31, so UNTIL is guaranteed to be a year the repo already existed)")
    parser.add_argument('--force', action='store_true', help="Ignore repos.done sentinel and re-run")
    return parser.parse_args(args)


def build_created_window(since: int = None, until: int = None) -> str:
    """Build GitHub search window qualifier.
    --since Y: repos with last push >= Y-01-01 (pushed: qualifier).
    --until Y: repo created strictly before Y-01-01 (created: qualifier)."""
    if since is not None:
        return f"pushed:>={since}-01-01"
    if until <= EARLIEST_YEAR:
        raise ValueError(f"--until must be > {EARLIEST_YEAR} (got {until})")
    return f"created:{EARLIEST_YEAR}-01-01..{until - 1}-12-31"


def get_star_bands(min_stars: int) -> list:
    """Return list of (lo, hi) star range tuples. hi=None means open-ended (>=lo)."""
    bands = []
    for i in range(len(STAR_BREAKPOINTS)):
        lo = STAR_BREAKPOINTS[i]
        hi = STAR_BREAKPOINTS[i + 1] - 1 if i + 1 < len(STAR_BREAKPOINTS) else None

        # skip band entirely if hi is below min_stars
        if hi is not None and hi < min_stars:
            continue

        # clip lo up to min_stars if needed
        if lo < min_stars:
            lo = min_stars

        bands.append((lo, hi))
    return bands


def probe_count(language: str, lo: int, hi, created_window: str) -> int:
    """Return total_count for a star band without fetching results (1 API call)."""
    star_q = f"stars:>={lo}" if hi is None else f"stars:{lo}..{hi}"
    query = f"language:{language}+{star_q}+{created_window}"
    check_search_rate_limit()
    result = subprocess.run(
        ["gh", "api", f"/search/repositories?q={query}&per_page=1"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return 0
    return json.loads(result.stdout).get("total_count", 0)


def get_star_bands_dynamic(language: str, min_stars: int, created_window: str) -> list:
    """Build star bands by probing total_count and bisecting until each band has <=1000 repos."""
    bands = []

    def bisect(lo, hi):
        count = probe_count(language, lo, hi, created_window)
        if count == 0:
            return
        if count <= 1000 or lo >= hi:
            bands.append((lo, hi))
            return
        mid = (lo + hi) // 2
        bisect(lo, mid)
        bisect(mid + 1, hi)

    bisect(min_stars, MAX_STARS_CAP)
    top = probe_count(language, MAX_STARS_CAP + 1, None, created_window)
    if top > 0:
        bands.append((MAX_STARS_CAP + 1, None))

    return bands


def search_repos_page(query: str, page: int, per_page: int = 100) -> tuple:
    """Return (items, total_count). Retries on 403 rate-limit (sleep to reset) and 502/504/network (exp backoff)."""
    url = f"/search/repositories?q={query}&per_page={per_page}&page={page}&sort=stars&order=asc"
    max_attempts = 6
    backoff = 10
    attempt = 0

    while attempt < max_attempts:
        result = subprocess.run(["gh", "api", url], capture_output=True, text=True)
        if result.returncode == 0:
            d = json.loads(result.stdout)
            return d.get("items", []), d.get("total_count", 0)

        http_code = _parse_http_code(result.stderr)
        msg = _parse_error_msg(result.stdout)
        if not msg.strip() and result.stderr.strip():
            msg = result.stderr.strip()

        if http_code == 403 and "rate limit" in msg.lower():
            rl_result = subprocess.run(["gh", "api", "/rate_limit"], capture_output=True, text=True)
            sleep_secs = 60
            try:
                search_rl = json.loads(rl_result.stdout)["resources"]["search"]
                sleep_secs = max(search_rl["reset"] - int(time.time()), 0) + 5
            except (json.JSONDecodeError, KeyError):
                pass
            print(f"  search 403 rate limit, czekam {sleep_secs}s")
            time.sleep(sleep_secs)
        elif http_code in (0, 502, 504):
            attempt += 1
            if attempt >= max_attempts:
                raise GHAPIError(http_code, msg)
            sleep_secs = min(backoff * (2 ** (attempt - 1)), 600)
            label = "błąd sieciowy" if http_code == 0 else f"HTTP {http_code}"
            print(f"  search {label}, backoff {sleep_secs}s (próba {attempt}/{max_attempts})")
            time.sleep(sleep_secs)
        else:
            raise GHAPIError(http_code, msg)

    raise GHAPIError(0, "max retry exceeded")


def collect_band(language: str, lo: int, hi, created_window: str, seen: set) -> list:
    """Fetch candidates from one star band. Returns list of repo dicts."""
    star_q = f"stars:>={lo}" if hi is None else f"stars:{lo}..{hi}"
    query = f"language:{language}+{star_q}+{created_window}"

    candidates = []
    for page in range(1, MAX_SEARCH_PAGE + 1):
        check_search_rate_limit()
        try:
            items, total = search_repos_page(query, page)
        except GHAPIError as e:
            print(f"  pasmo {lo}..{hi}: strona {page} przerwana — HTTP {e.http_code}: {e.message}")
            break
        if not items:
            break
        for item in items:
            fn = item["full_name"]
            if fn in seen:
                continue
            seen.add(fn)
            owner, repo = fn.split("/", 1)
            candidates.append({
                "owner": owner,
                "repo": repo,
                "stars": item["stargazers_count"],
                "forks": item["forks_count"],
                "language": item.get("language") or language,
                "created_at": item["created_at"],
            })
        print(f"  pasmo {lo}..{hi}: strona {page}, łącznie {len(candidates)}, total_count={total}")
        if len(items) < 100:
            break
    return candidates


def get_contributor_count(owner: str, repo: str) -> int:
    """Return approximate contributor count using Link header trick."""
    url = f"/repos/{owner}/{repo}/contributors?per_page=1&anon=0"
    result = subprocess.run(["gh", "api", url, "--include"], capture_output=True, text=True)
    if result.returncode != 0:
        http_code = _parse_http_code(result.stderr)
        if http_code == 404:
            return 0
        raise GHAPIError(http_code, result.stderr)
    link_m = re.search(r'Link:.*?page=(\d+)>;\s*rel="last"', result.stdout)
    if link_m:
        return int(link_m.group(1))
    try:
        body_part = result.stdout.split("\n\n", 1)[-1].strip()
        items = json.loads(body_part)
        return len(items) if isinstance(items, list) else 0
    except Exception:
        return 0


def _load_contributor_progress(repos_csv: Path, progress_file: Path) -> tuple:
    """Return (start_idx, done_names) from progress file and existing CSV."""
    start_idx = 0
    done_names: set = set()
    if repos_csv.exists() and progress_file.exists():
        try:
            start_idx = int(progress_file.read_text().strip())
        except ValueError:
            start_idx = 0
        try:
            with open(repos_csv) as f:
                for row in csv.DictReader(f):
                    done_names.add(f"{row['owner']}/{row['repo']}")
        except Exception:
            pass
        if start_idx > 0:
            print(f"Wznawianie od indeksu {start_idx}, już zapisano {len(done_names)} repo")
    return start_idx, done_names


def main():
    args = parse_args()
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)
    repos_csv = data_dir / "repos.csv"
    done_file = data_dir / "repos.done"
    progress_file = data_dir / "repos.progress"
    meta_file = data_dir / "repos.meta.json"
    bands_file = data_dir / "repos_bands.json"

    if done_file.exists() and not args.force:
        print("repos.csv już istnieje. Usuń data/repos.done lub użyj --force żeby powtórzyć.")
        sys.exit(0)
    if args.force:
        done_file.unlink(missing_ok=True)
        progress_file.unlink(missing_ok=True)
        repos_csv.unlink(missing_ok=True)
        meta_file.unlink(missing_ok=True)
        bands_file.unlink(missing_ok=True)

    if args.since is not None:
        mode, year = "since", args.since
    else:
        mode, year = "until", args.until
    created_window = build_created_window(since=args.since, until=args.until)
    meta_file.write_text(json.dumps({"mode": mode, "year": year, "created_window": created_window}, indent=2))
    print(f"Tryb: --{mode} {year} ({created_window})")

    # Stage 1: search across star bands
    bands_cache: dict = {}
    if bands_file.exists():
        try:
            bands_cache = json.loads(bands_file.read_text())
        except (json.JSONDecodeError, OSError):
            bands_cache = {}
        if bands_cache:
            print(f"Wznawiam Stage 1: znaleziono {len(bands_cache)} ukończonych pasm w {bands_file}")

    print("Wyznaczam pasma gwiazdek...")
    bands = get_star_bands_dynamic(args.language, args.min_stars, created_window)
    print(f"Wyznaczono {len(bands)} pasm")
    seen: set = set()
    candidates = []

    for band_key, band_results in bands_cache.items():
        for r in band_results:
            fn = f"{r['owner']}/{r['repo']}"
            seen.add(fn)
        candidates.extend(band_results)

    for lo, hi in bands:
        band_key = f"{lo}_{hi}"
        if band_key in bands_cache:
            print(f"  skipping completed band {lo}..{hi if hi else '*'} (cached)")
            continue
        print(f"Szukam w paśmie stars:{lo}..{hi if hi else '*'}")
        band_results = collect_band(args.language, lo, hi, created_window, seen)
        candidates.extend(band_results)
        bands_cache[band_key] = band_results
        bands_file.write_text(json.dumps(bands_cache, indent=2))

    print(f"Znaleziono {len(candidates)} unikalnych kandydatów")

    # Stage 2: contributor filter with progress tracking
    start_idx, done_names = _load_contributor_progress(repos_csv, progress_file)
    resuming = start_idx > 0
    mode = "a" if resuming else "w"
    accepted = 0

    with open(repos_csv, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=REPO_FIELDS, extrasaction="ignore")
        if not resuming:
            writer.writeheader()
        for i, cand in enumerate(candidates[start_idx:], start=start_idx):
            fn = f"{cand['owner']}/{cand['repo']}"
            if fn in done_names:
                continue
            try:
                count = get_contributor_count(cand["owner"], cand["repo"])
            except GHAPIError:
                count = 0
            if count >= args.min_contributors:
                writer.writerow({**cand, "contributors_count": count})
                f.flush()
                accepted += 1
            progress_file.write_text(str(i + 1))
            if (i + 1) % 50 == 0:
                print(f"  sprawdzono {i+1}/{len(candidates)}, zaakceptowano {accepted}")

    progress_file.unlink(missing_ok=True)
    done_file.touch()
    bands_file.unlink(missing_ok=True)
    print(f"Gotowe: {accepted} repo zapisano do {repos_csv}")


if __name__ == "__main__":
    main()
