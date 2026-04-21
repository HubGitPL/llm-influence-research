"""GitHub OSS data collector — collects commits, PRs, and issues to CSV."""
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


# --- Config ---

def load_repos(path: Path = Path("repos.json")) -> list:
    with open(path, encoding="utf-8") as f:
        repos = json.load(f)
    for r in repos:
        for key in ("owner", "repo", "since", "until"):
            if key not in r:
                print(f"repos.json: brakuje pola '{key}' w {r}", file=sys.stderr)
                sys.exit(1)
    return repos


# --- Bot and AI detection ---

KNOWN_BOT_LOGINS: frozenset = frozenset({
    "allcontributors",
    "dependabot[bot]",
    "renovate[bot]",
    "github-actions[bot]",
    "stale[bot]",
    "imgbot[bot]",
    "allcontributors[bot]",
    "greenkeeper[bot]",
    "semantic-release-bot",
    "codecov",
    "snyk-bot",
    "pytorchmergebot",
})

_AI_KEYWORDS = [
    "claude", "cursor", "copilot", "chatgpt",
    "ai-generated", "gpt-4", "gemini", 
]
_AI_PATTERN = re.compile(
    "|".join(re.escape(kw) for kw in _AI_KEYWORDS),
    re.IGNORECASE,
)


def detect_bot(login: str, author_type: str) -> bool:
    """True jeśli login/author_type wskazuje na bota."""
    if author_type == "Bot":
        return True
    if login.endswith("[bot]"):
        return True
    if login in KNOWN_BOT_LOGINS:
        return True
    return False


def detect_ai_mention(text: str) -> str:
    """Zwraca trafione AI keywordy oddzielone ';', pusty string jeśli brak."""
    if not text:
        return ""
    found = []
    seen = set()
    for m in _AI_PATTERN.finditer(text):
        kw = m.group(0).lower()
        if kw not in seen:
            seen.add(kw)
            found.append(kw)
    return ";".join(found)


# --- Status tracking ---
# Jeden plik _status.json sluzacy jako checkpoint dla kazdego zbioru danych

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


# --- CSV ---

def write_csv(filepath: Path, rows: list, fieldnames: list):
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# --- Issue filtering helper ---

def _filter_issues(items: list) -> list:
    """Odfiltruj pull requesty z listy issues."""
    return [item for item in items if not item.get("pull_request")]


# --- Commit row extraction ---

def extract_commit_row(commit: dict) -> dict:
    author_user = commit.get("author") or {}  # może być null
    git_author = commit["commit"]["author"]
    login = author_user.get("login", "")
    author_type = author_user.get("type", "")
    return {
        "sha": commit["sha"],
        "author_name": git_author.get("name", ""),
        "author_email": git_author.get("email", ""),
        "author_login": login,
        "author_type": author_type,
        "author_date": git_author["date"],
        "is_bot": detect_bot(login, author_type),
    }


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
    """Parsuj stdout gh api — obsługuje concatenated JSON arrays z --paginate."""
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
            return obj  # single object response (np. dla /commits/{sha})
        idx = end_idx
    return items


# --- gh API wrappers ---

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
    """Wywołaj gh api z retry dla 403/502/504."""
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
                sleep_secs = 3600  # fallback: czekaj godzinę jeśli API rate_limit niedostępne
            print(f"  403 rate limit, czekam {sleep_secs}s")
            time.sleep(sleep_secs)
            # nie inkrementuj attempt — rate limit nie liczy się jako błąd
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


def run_gh_paginate(url: str) -> list:
    """Wywołaj gh api --paginate i zwróć spłaszczoną listę obiektów."""
    return run_gh_with_retry(url, paginate=True)


# --- Commit collection ---
COMMIT_FIELDS = ["sha", "author_name", "author_email", "author_login", "author_type", "author_date", "is_bot"]

# #Nie uzywana na ten moment, byla w pierwotnym planie
# def get_commit_stats(owner: str, repo: str, sha: str) -> dict:
#     """Pobierz additions/deletions dla jednego commita — wymaga osobnego API call."""
#     data = run_gh_with_retry(f"/repos/{owner}/{repo}/commits/{sha}")
#     if isinstance(data, dict):
#         return data.get("stats", {"additions": 0, "deletions": 0})
#     return {"additions": 0, "deletions": 0}


def collect_commits(owner: str, repo: str, since: str, until: str, out_dir: Path):
    """Pobierz wszystkie commity strona po stronie i zapisuj na bieżąco.

    Odporne na przerwanie: wznawia od ostatniego zapisanego SHA.
    Nie używa --paginate — każda strona (100 commitów) jest zapisywana od razu.
    """
    if checkpoint_exists(out_dir, "commits"):
        print(f"  [{owner}/{repo}] commits: pominięto (sentinel)")
        return

    csv_path = out_dir / "commits.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    # Wczytaj już przetworzone SHAs z ewentualnego częściowego pliku
    done_shas: set = set()
    if csv_path.exists():
        with open(csv_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done_shas.add(row["sha"])
        print(f"  [{owner}/{repo}] commits: wznowienie, już pobrano {len(done_shas)}")

    write_header = not csv_path.exists()
    new_count = 0
    page = 1

    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COMMIT_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()

        while True:
            url = (f"/repos/{owner}/{repo}/commits"
                   f"?since={since}T00:00:00Z&until={until}T23:59:59Z"
                   f"&per_page=100&page={page}")
            page_commits = run_gh_with_retry(url)
            if not page_commits:
                break

            for commit in page_commits:
                sha = commit["sha"]
                if sha in done_shas:
                    continue
                writer.writerow(extract_commit_row(commit))
                new_count += 1

            f.flush()
            total_so_far = len(done_shas) + new_count
            print(f"  [{owner}/{repo}] commits: strona {page}, łącznie {total_so_far}")
            page += 1

    mark_done(out_dir, "commits")
    print(f"  [{owner}/{repo}] commits: zapisano {len(done_shas) + new_count} wierszy")


# --- PR collection ---

PR_FIELDS = ["number", "state", "created_at", "closed_at", "merged_at", "author_login", "author_type", "is_bot"]


def collect_prs(owner: str, repo: str, since: str, until: str, out_dir: Path):
    """Pobierz PR-y w zakresie since–until i zapisuj na bieżąco do prs.csv.

    Odporne na przerwanie: wznawia od ostatniego zapisanego numeru PR.
    Paginacja malejąco po dacie — przerywa gdy created_at < since.
    """
    from datetime import datetime
    since_dt = datetime.fromisoformat(since + "T00:00:00+00:00")
    until_dt = datetime.fromisoformat(until + "T23:59:59+00:00")

    if checkpoint_exists(out_dir, "prs"):
        print(f"  [{owner}/{repo}] prs: pominięto (sentinel)")
        return

    csv_path = out_dir / "prs.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    done_numbers: set = set()
    if csv_path.exists():
        with open(csv_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done_numbers.add(row["number"])
        print(f"  [{owner}/{repo}] prs: wznowienie, już pobrano {len(done_numbers)}")

    # Wczytaj ostatnią stronę z pliku progress ( żeby nie skanować od początku)
    progress_path = out_dir / "prs.progress"
    page = 1
    if progress_path.exists():
        try:
            page = max(1, int(progress_path.read_text().strip()) - 2)
            print(f"  [{owner}/{repo}] prs: wznawianie od strony {page}")
        except ValueError:
            page = 1

    print(f"  [{owner}/{repo}] prs: pobieranie (od {since} do {until})...")
    write_header = not csv_path.exists()
    new_count = 0
    skipped_new = 0

    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PR_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()

        while True:
            url = (f"/repos/{owner}/{repo}/pulls"
                   f"?state=all&sort=created&direction=desc&per_page=100&page={page}")
            batch = run_gh_with_retry(url, paginate=False)
            if isinstance(batch, dict):
                batch = [batch]
            if not batch:
                break

            stop = False
            for pr in batch:
                created = datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00"))
                if created < since_dt:
                    stop = True
                    break
                if created > until_dt:
                    skipped_new += 1
                    continue
                if str(pr["number"]) in done_numbers:
                    continue
                pr_author = (pr.get("user") or {})
                pr_login = pr_author.get("login", "")
                pr_author_type = pr_author.get("type", "")
                writer.writerow({
                    "number": pr["number"],
                    "state": pr.get("state", ""),
                    "created_at": pr.get("created_at", ""),
                    "closed_at": pr.get("closed_at") or "",
                    "merged_at": pr.get("merged_at") or "",
                    "author_login": pr_login,
                    "author_type": pr_author_type,
                    "is_bot": detect_bot(pr_login, pr_author_type),
                })
                new_count += 1

            f.flush()
            progress_path.write_text(str(page))
            print(f"  [{owner}/{repo}] prs: strona {page}, zapisano {len(done_numbers) + new_count}, pominięto za nowych: {skipped_new}")
            if stop:
                break
            page += 1

    progress_path.unlink(missing_ok=True)
    mark_done(out_dir, "prs")
    print(f"  [{owner}/{repo}] prs: zapisano {len(done_numbers) + new_count} wierszy")


# --- Issue collection ---

ISSUE_FIELDS = ["number", "state", "created_at", "closed_at", "author_login", "author_type", "is_bot"]


def collect_issues(owner: str, repo: str, since: str, until: str, out_dir: Path):
    """Pobierz issues w zakresie since–until i zapisuj na bieżąco do issues.csv.

    Odporne na przerwanie: wznawia od ostatniego zapisanego numeru issue.
    """
    from datetime import datetime
    since_dt = datetime.fromisoformat(since + "T00:00:00+00:00")
    until_dt = datetime.fromisoformat(until + "T23:59:59+00:00")

    if checkpoint_exists(out_dir, "issues"):
        print(f"  [{owner}/{repo}] issues: pominięto (sentinel)")
        return

    csv_path = out_dir / "issues.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    done_numbers: set = set()
    if csv_path.exists():
        with open(csv_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done_numbers.add(row["number"])
        print(f"  [{owner}/{repo}] issues: wznowienie, już pobrano {len(done_numbers)}")

    print(f"  [{owner}/{repo}] issues: pobieranie (od {since} do {until})...")
    write_header = not csv_path.exists()
    new_count = 0
    page = 1

    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ISSUE_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()

        while True:
            url = (f"/repos/{owner}/{repo}/issues"
                   f"?state=all&sort=created&direction=desc&per_page=100&page={page}")
            batch = run_gh_with_retry(url, paginate=False)
            if isinstance(batch, dict):
                batch = [batch]
            if not batch:
                break

            stop = False
            for item in batch:
                created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
                if created < since_dt:
                    stop = True
                    break
                if item.get("pull_request"):
                    continue  # odfiltrowuj PR-y zwracane przez /issues
                if created > until_dt:
                    continue
                if str(item["number"]) in done_numbers:
                    continue
                issue_author = (item.get("user") or {})
                issue_login = issue_author.get("login", "")
                issue_author_type = issue_author.get("type", "")
                writer.writerow({
                    "number": item["number"],
                    "state": item.get("state", ""),
                    "created_at": item.get("created_at", ""),
                    "closed_at": item.get("closed_at") or "",
                    "author_login": issue_login,
                    "author_type": issue_author_type,
                    "is_bot": detect_bot(issue_login, issue_author_type),
                })
                new_count += 1

            f.flush()
            print(f"  [{owner}/{repo}] issues: strona {page}, łącznie {len(done_numbers) + new_count}")
            if stop:
                break
            page += 1

    mark_done(out_dir, "issues")
    print(f"  [{owner}/{repo}] issues: zapisano {len(done_numbers) + new_count} wierszy")


# --- Contributor collection ---

CONTRIBUTOR_FIELDS = ["login", "type", "contributions", "is_bot"]


def collect_contributors(owner: str, repo: str, since: str, until: str, out_dir: Path):
    """Pobierz wszystkich kontrybutorów repo i zapisz do contributors.csv.

    Endpoint /contributors nie obsługuje filtra dat — zbiera całkowite contribution counts.
    Używa --paginate bo wynik może być dłuższy niż jedna strona.
    """
    if checkpoint_exists(out_dir, "contributors"):
        print(f"  [{owner}/{repo}] contributors: pominięto (sentinel)")
        return

    csv_path = out_dir / "contributors.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    done_logins: set = set()
    if csv_path.exists():
        with open(csv_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done_logins.add(row["login"])
        print(f"  [{owner}/{repo}] contributors: wznowienie, już pobrano {len(done_logins)}")

    print(f"  [{owner}/{repo}] contributors: pobieranie...")
    write_header = not csv_path.exists()
    new_count = 0

    url = f"/repos/{owner}/{repo}/contributors?per_page=100&anon=0"
    items = run_gh_paginate(url)

    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CONTRIBUTOR_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        for item in items:
            login = item.get("login", "")
            if login in done_logins:
                continue
            author_type = item.get("type", "")
            writer.writerow({
                "login": login,
                "type": author_type,
                "contributions": item.get("contributions", 0),
                "is_bot": detect_bot(login, author_type),
            })
            new_count += 1
        f.flush()

    mark_done(out_dir, "contributors")
    print(f"  [{owner}/{repo}] contributors: zapisano {len(done_logins) + new_count} wierszy")


# --- Release collection ---

RELEASE_FIELDS = ["tag_name", "published_at", "prerelease", "is_draft", "author_login", "author_type", "is_bot"]


def collect_releases(owner: str, repo: str, since: str, until: str, out_dir: Path):
    """Pobierz release'y repo i zapisz do releases.csv.

    Ręczna paginacja — endpoint nie obsługuje sort+direction, więc nie możemy
    używać break przy dacie. Zbieramy wszystkie release'y (zwykle <kilkaset).
    """
    if checkpoint_exists(out_dir, "releases"):
        print(f"  [{owner}/{repo}] releases: pominięto (sentinel)")
        return

    csv_path = out_dir / "releases.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    done_tags: set = set()
    if csv_path.exists():
        with open(csv_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done_tags.add(row["tag_name"])
        print(f"  [{owner}/{repo}] releases: wznowienie, już pobrano {len(done_tags)}")

    print(f"  [{owner}/{repo}] releases: pobieranie...")
    write_header = not csv_path.exists()
    new_count = 0
    page = 1

    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RELEASE_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()

        while True:
            url = f"/repos/{owner}/{repo}/releases?per_page=100&page={page}"
            batch = run_gh_with_retry(url, paginate=False)
            if isinstance(batch, dict):
                batch = [batch]
            if not batch:
                break

            for release in batch:
                tag = release.get("tag_name", "")
                if tag in done_tags:
                    continue
                rel_author = (release.get("author") or {})
                author_login = rel_author.get("login", "")
                author_type = rel_author.get("type", "")
                writer.writerow({
                    "tag_name": tag,
                    "published_at": release.get("published_at") or "",
                    "prerelease": release.get("prerelease", False),
                    "is_draft": release.get("draft", False),
                    "author_login": author_login,
                    "author_type": author_type,
                    "is_bot": detect_bot(author_login, author_type),
                })
                new_count += 1

            f.flush()
            print(f"  [{owner}/{repo}] releases: strona {page}, łącznie {len(done_tags) + new_count}")
            page += 1

    mark_done(out_dir, "releases")
    print(f"  [{owner}/{repo}] releases: zapisano {len(done_tags) + new_count} wierszy")


# --- Main entrypoint ---

def main():
    print("POCZATEK! \nRozpoczynam zbieranie danych")
    repos = load_repos()
    print(f"Kolekcja danych: {len(repos)} repo(s)")

    for entry in repos:
        owner = entry["owner"]
        repo = entry["repo"]
        since = entry["since"]
        until = entry["until"]

        out_dir = Path("data") / "raw" / f"{owner}_{repo}"
        out_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n[{owner}/{repo}] start (since={since}, until={until})")
        collect_commits(owner, repo, since, until, out_dir)
        collect_prs(owner, repo, since, until, out_dir)
        collect_issues(owner, repo, since, until, out_dir)
        collect_contributors(owner, repo, since, until, out_dir)
        collect_releases(owner, repo, since, until, out_dir)
        print(f"[{owner}/{repo}] gotowe")

    print("\nKONIEC! \nKolekcja zakończona.")


if __name__ == "__main__":
    main()
