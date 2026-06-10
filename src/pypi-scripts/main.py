#!/usr/bin/env python3

import os
import utils.pypi as pypi
from dotenv import load_dotenv
import github_metrics as github_metrics
from datetime import datetime, timedelta
import matplotlib.pyplot as plt
from typing import List

from utils.schemas import RepoMetrics



load_dotenv()
TOKEN = os.getenv("GITHUB_TOKEN", "")


def example_run() -> None:
    # Example usage of the get_commit_count function
    owner = "certifi"
    repo = "python-certifi"
    since = datetime(2020, 2, 25) # Commits on Feb 25, 2026
    until = datetime.now() # now
    count = github_metrics.get_commit_count(
        token=TOKEN,
        owner=owner,
        repo=repo,
        since=since,
        until=until
    )
    active_contributors = github_metrics.get_active_contributors_count(
        token=TOKEN,
        owner=owner,
        repo=repo,
        since=since,
        until=until
    )
    issue_count = github_metrics.get_issue_count(
        token=TOKEN,
        owner=owner,
        repo=repo,
        since=since,
        until=until
    )
    average_issue_resolution_time = github_metrics.get_average_issue_resolution_time(
        token=TOKEN,
        owner=owner,
        repo=repo,
        since=since,
        until=until
    )
    package_info = pypi.get_pypi("certifi")
    stats = pypi.calculate_pypi_release_stats(package_info)
    
    
    
    
    print(f"Commits: {count}")
    print(f"Issues created: {issue_count}")
    print(f"Active contributors: {active_contributors}")
    print(f"Average issue resolution time: {average_issue_resolution_time}")
    print(f"Package: certifi, Release count: {stats.release_count}, Average interval between releases: {stats.average_interval}")
    




def plot_metrics(data: List[RepoMetrics]) -> None:
    names = [d.package_name for d in data]

    commits = [d.commits for d in data]
    issues = [d.issues for d in data]
    contributors = [d.contributors for d in data]

    # --- 1. Commits ---
    plt.figure(figsize=(12, 5))
    plt.bar(names, commits)
    plt.xticks(rotation=90)
    plt.title("Commits per PyPI repository (last 12 months)")
    plt.ylabel("Commits")
    plt.tight_layout()
    plt.show()

    # --- 2. Issues ---
    plt.figure(figsize=(12, 5))
    plt.bar(names, issues)
    plt.xticks(rotation=90)
    plt.title("Issues per PyPI repository (last 12 months)")
    plt.ylabel("Issues")
    plt.tight_layout()
    plt.show()

    # --- 3. Contributors ---
    plt.figure(figsize=(12, 5))
    plt.bar(names, contributors)
    plt.xticks(rotation=90)
    plt.title("Active contributors per repository")
    plt.ylabel("Contributors")
    plt.tight_layout()
    plt.show()

    # --- 4. Scatter: commits vs issues ---
    plt.figure(figsize=(6, 6))
    plt.scatter(commits, issues)

    for i, name in enumerate(names):
        plt.annotate(name, (commits[i], issues[i]), fontsize=7)

    plt.title("Commits vs Issues correlation")
    plt.xlabel("Commits")
    plt.ylabel("Issues")
    plt.tight_layout()
    plt.show()

    # --- 5. Issue resolution time ---
    resolution = [
        d.avg_resolution_hours if d.avg_resolution_hours else 0
        for d in data
    ]

    plt.figure(figsize=(12, 5))
    plt.bar(names, resolution)
    plt.xticks(rotation=90)
    plt.title("Average issue resolution time (hours)")
    plt.ylabel("Hours")
    plt.tight_layout()
    plt.show()
    

def main() -> None:
    load_dotenv()

    results = pypi.get_top_packages()
    github_repos = pypi.extract_github_repo_data(results, limit=20)

    dataset: List[RepoMetrics] = []

    since = datetime.now() - timedelta(days=365)
    until = datetime.now()

    for i, item in enumerate(github_repos, start=1):
        print(f"[{i}/20] {item.package_name}")

        try:
            commits = github_metrics.get_commit_count(
                TOKEN,
                item.repo_owner,
                item.repo_name,
                since,
                until
            )

            issues = github_metrics.get_issue_count(
                TOKEN,
                item.repo_owner,
                item.repo_name,
                since,
                until
            )

            contributors = github_metrics.get_active_contributors_count(
                TOKEN,
                item.repo_owner,
                item.repo_name,
                since,
                until
            )

            avg_issue_time = github_metrics.get_average_issue_resolution_time(
                TOKEN,
                item.repo_owner,
                item.repo_name,
                since,
                until
            )

            avg_hours = (
                avg_issue_time.total_seconds() / 3600
                if avg_issue_time else None
            )

            dataset.append(
                RepoMetrics(
                    package_name=item.package_name,
                    commits=commits,
                    issues=issues,
                    contributors=contributors,
                    avg_resolution_hours=avg_hours
                )
            )

        except Exception as e:
            print(f"Error for {item.package_name}: {e}")
            continue

    plot_metrics(dataset)
    
    
if __name__ == "__main__":
    example_run()
    main()