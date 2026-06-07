"""Collect metadata (releases, commit counts) for OSS repos listed in data/repos.csv."""
import argparse
import csv
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from src._utils import (
    GHAPIError,
    _parse_http_code,
    _parse_error_msg,
    _parse_json_output,
    check_rate_limit,
    run_gh_with_retry,
    write_csv,
    _load_status,
    checkpoint_exists,
    mark_done,
)

_include_api_call_count = 0


def run_gh_with_include_retry(url: str) -> str:
    """Call gh api URL --include with retry for 403/502/504. Returns raw stdout (headers+body)."""
    global _include_api_call_count
    max_attempts = 6
    backoff = 10
    attempt = 0

    while attempt < max_attempts:
        _include_api_call_count += 1
        if _include_api_call_count % 200 == 0:
            check_rate_limit(warn_if_below=100, auto_sleep=True)

        cmd = ["gh", "api", url, "--include"]
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0:
            return result.stdout

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


def is_repo_complete(out_dir: Path) -> bool:
    status = _load_status(out_dir)
    return all(status.get(m) is True for m in ("releases", "total_commits", "commits_per_year"))


def load_repos_csv(path: Path) -> list:
    if not path.exists():
        print(f"Error: {path} does not exist", file=sys.stderr)
        sys.exit(1)
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_args(args=None):
    parser = argparse.ArgumentParser(description="Collect metadata for OSS repos from repos.csv.")
    parser.add_argument("--since", type=int, default=None, help="Start year for per-year commit counts. If omitted, derived from repos.meta.json or min(created_at) in repos.csv")
    parser.add_argument("--until", type=int, default=None, help="End year for per-year commit counts. If omitted, derived from repos.meta.json or defaults to 2025")
    parser.add_argument("--resume", type=int, default=None, help="Resume run N (reuse existing data/collect_N/, skip completed repos)")
    return parser.parse_args(args)


def derive_since_from_repos(repos: list) -> int:
    years = []
    for r in repos:
        ca = r.get("created_at", "")
        if len(ca) >= 4:
            try:
                years.append(int(ca[:4]))
            except ValueError:
                pass
    if not years:
        print("Error: nie udało się wyderywować --since — brak prawidłowych created_at w repos.csv", file=sys.stderr)
        sys.exit(1)
    return min(years)


def load_repos_meta(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def resolve_year_bounds(cli_since: int, cli_until: int, meta: dict, repos: list) -> tuple:
    """Return (since_year, until_year). CLI overrides meta; meta overrides derived defaults."""
    mode = meta.get("mode")
    meta_year = meta.get("year")

    if mode == "since" and meta_year is not None:
        since_year = cli_since if cli_since is not None else meta_year
        until_year = cli_until if cli_until is not None else datetime.now().year
    elif mode == "until" and meta_year is not None:
        since_year = cli_since if cli_since is not None else derive_since_from_repos(repos)
        until_year = cli_until if cli_until is not None else meta_year
    else:
        since_year = cli_since if cli_since is not None else derive_since_from_repos(repos)
        until_year = cli_until if cli_until is not None else datetime.now().year

    if since_year > until_year:
        print(f"Error: since_year ({since_year}) > until_year ({until_year})", file=sys.stderr)
        sys.exit(1)
    return since_year, until_year


RELEASE_META_FIELDS = ["tag_name", "published_at", "prerelease", "author_login"]


def collect_releases_meta(owner: str, repo: str, out_dir: Path, since_year: int, until_year: int = None):
    if until_year is None:
        until_year = datetime.now().year
    if checkpoint_exists(out_dir, "releases"):
        print(f"  [{owner}/{repo}] releases: skip (already done)")
        return

    rows = []
    page = 1
    while True:
        url = f"/repos/{owner}/{repo}/releases?per_page=100&page={page}"
        batch = run_gh_with_retry(url)
        if isinstance(batch, dict):
            batch = [batch]
        if not batch:
            break
        for r in batch:
            pub = r.get("published_at") or ""
            if len(pub) >= 4:
                try:
                    if int(pub[:4]) > until_year:
                        continue
                except ValueError:
                    pass
            author_login = (r.get("author") or {}).get("login", "")
            rows.append({
                "tag_name": r["tag_name"],
                "published_at": r["published_at"],
                "prerelease": r["prerelease"],
                "author_login": author_login,
            })
        page += 1

    write_csv(out_dir / "releases.csv", rows, RELEASE_META_FIELDS)

    counts = {y: 0 for y in range(since_year, until_year + 1)}
    for r in rows:
        pub = r.get("published_at")
        if not pub:
            continue
        try:
            year = int(pub[:4])
        except ValueError:
            continue
        if year in counts:
            counts[year] += 1
    per_year_rows = [{"year": y, "count": counts[y]} for y in sorted(counts)]
    write_csv(out_dir / "releases_per_year.csv", per_year_rows, ["year", "count"])

    mark_done(out_dir, "releases")


def get_total_commit_count(owner: str, repo: str, until_year: int = None) -> int:
    if until_year is None:
        until_year = datetime.now().year
    url = f"/repos/{owner}/{repo}/commits?per_page=1&until={until_year}-12-31T23:59:59Z"
    try:
        raw = run_gh_with_include_retry(url)
    except GHAPIError as e:
        if e.http_code == 409:
            return 0
        raise
    link_m = re.search(r'Link:.*?page=(\d+)>;\s*rel="last"', raw)
    if link_m:
        return int(link_m.group(1))
    body = raw.split("\n\n", 1)[-1].strip()
    try:
        items = json.loads(body)
        return len(items)
    except Exception:
        return 0


def collect_commits_per_year(owner: str, repo: str, since_year: int, until_year: int = None) -> list:
    if until_year is None:
        until_year = datetime.now().year
    rows = []
    for year in range(since_year, until_year + 1):
        url = (
            f"/repos/{owner}/{repo}/commits"
            f"?per_page=1&since={year}-01-01T00:00:00Z&until={year}-12-31T23:59:59Z"
        )
        try:
            raw = run_gh_with_include_retry(url)
            link_m = re.search(r'Link:.*?page=(\d+)>;\s*rel="last"', raw)
            if link_m:
                count = int(link_m.group(1))
            else:
                body = raw.split("\n\n", 1)[-1].strip()
                count = len(json.loads(body))
        except GHAPIError as e:
            if e.http_code == 409:
                count = 0
            else:
                raise
        except Exception as e:
            print(f"  [{owner}/{repo}] commits {year}: błąd zbierania — {e}", file=sys.stderr)
            count = None
        rows.append({"year": year, "count": count})
    return rows


def next_collect_dir(data_dir: Path) -> Path:
    n = 1
    while (data_dir / f"collect_{n}").exists():
        n += 1
    return data_dir / f"collect_{n}"


def main():
    args = parse_args()
    repos_done = Path("data") / "repos.done"
    if not repos_done.exists():
        print("Error: data/repos.done not found — run find_repos.py first", file=sys.stderr)
        sys.exit(1)
    repos = load_repos_csv(Path("data/repos.csv"))
    data_dir = Path("data")

    meta = load_repos_meta(data_dir / "repos.meta.json")
    since_year, until_year = resolve_year_bounds(args.since, args.until, meta, repos)
    src = []
    if meta.get("mode"):
        src.append(f"repos.meta.json (--{meta['mode']} {meta.get('year')})")
    if args.since is not None or args.until is not None:
        src.append("CLI override")
    src_label = ", ".join(src) if src else "derived from repos.csv"
    print(f"Zakres lat: {since_year}..{until_year} ({src_label})")

    if args.resume is not None:
        collect_dir = data_dir / f"collect_{args.resume}"
        if not collect_dir.exists():
            print(f"Error: {collect_dir} does not exist — nie ma czego wznawiać", file=sys.stderr)
            sys.exit(1)
        print(f"Wznawiam run {args.resume} dla {len(repos)} repo → {collect_dir}/")
    else:
        collect_dir = next_collect_dir(data_dir)
        collect_dir.mkdir(parents=True)
        print(f"Zbieram dane dla {len(repos)} repo → {collect_dir}/")

    for entry in repos:
        owner = entry["owner"]
        repo = entry["repo"]
        out_dir = collect_dir / f"{owner}_{repo}"
        out_dir.mkdir(parents=True, exist_ok=True)

        if is_repo_complete(out_dir):
            print(f"skip {owner}/{repo}")
            continue

        collect_releases_meta(owner, repo, out_dir, since_year, until_year)

        if not checkpoint_exists(out_dir, "total_commits"):
            total = get_total_commit_count(owner, repo, until_year)
            (out_dir / "meta.json").write_text(json.dumps({"total_commits": total}, indent=2))
            mark_done(out_dir, "total_commits")

        if not checkpoint_exists(out_dir, "commits_per_year"):
            per_year = collect_commits_per_year(owner, repo, since_year, until_year)
            write_csv(out_dir / "commits_per_year.csv", per_year, ["year", "count"])
            mark_done(out_dir, "commits_per_year")

        print(f"  [{owner}/{repo}] done")


if __name__ == "__main__":
    main()
