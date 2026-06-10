#!/usr/bin/env python3

# import os
import asyncio
import repo_sampling
from utils import globals
from typing import Any, List
import github_metrics 
import utils.pypi as pypi
from pydantic import BaseModel
# from dotenv import load_dotenv
# import matplotlib.pyplot as plt
from github_metrics import RepoMetrics
from datetime import datetime, timedelta
from aiohttp_client_cache.session import CachedSession

from utils.time_intervals import datetime_range




class AggregatedMetrics(BaseModel):
    # total_commits: int
    # total_issues: int
    # total_contributors: int
    # avg_resolution_hours: float
    
    avg_commits_per_repo: float 
    avg_issues_per_repo: float
    avg_contributors_per_repo: float



class TimeSeriesPoint(BaseModel):
    start: datetime
    end: datetime
    metrics: AggregatedMetrics
    


async def process_repo(
    package: pypi.ExtractedPackageInfo, 
    start: datetime, 
    end: datetime
) -> RepoMetrics:
    
    owner = package.repo_info.repo_owner
    repo_name = package.repo_info.repo_name
    args = (globals.TOKEN, owner, repo_name, start, end)
    commits = await github_metrics.get_commit_count(*args)
    issues = await github_metrics.get_issue_count(*args)
    contributors = await github_metrics.get_active_contributors_count(*args)

    return RepoMetrics(
        package_name=package.package_name,
        commits=commits,
        issues=issues,
        contributors=contributors,
        avg_resolution_hours=0#avg_hours
    )
   



# time series per quarter
async def main() -> None:
    globals.SESSION = CachedSession(
        cache=globals.CACHE,
        expire_after=3600
    )

    top_15k_packages = pypi.get_top_packages()
    sampled_packages = repo_sampling.sample_representative_packages(top_15k_packages, n_total=100)
    print(f"Sampled {len(sampled_packages)} packages with GitHub repos.")
    github_repos = pypi.extract_github_repo_data(sampled_packages, limit=20)
    print(f"Extracted GitHub data for {len(github_repos)} packages.")





    start_date = datetime(2020, 1, 1)
    end_date = datetime.now()
    interval = timedelta(days=2*90)  # 6 months
    
    time_series: List[TimeSeriesPoint] = []
    for start, end in datetime_range(start_date, end_date, interval):
        print(start, "->", end)
        window_dataset: List[RepoMetrics] = []
        tasks: List[Any] = []
        for package in github_repos:
            tasks.append(process_repo(package, start, end))
        window_dataset = await asyncio.gather(*tasks)
            # try:
            #     commits = github_metrics.get_commit_count(*args)

            #     issues = github_metrics.get_issue_count(*args)

            #     contributors = github_metrics.get_active_contributors_count(*args)

            #     avg_issue_time = github_metrics.get_average_issue_resolution_time(*args)

            #     avg_hours = (
            #         avg_issue_time.total_seconds() / 3600
            #         if avg_issue_time else None
            #     )

            #     window_dataset.append(
            #         RepoMetrics(
            #             package_name=item.package_name,
            #             commits=commits,
            #             issues=issues,
            #             contributors=contributors,
            #             avg_resolution_hours=avg_hours
            #         )
            #     )

            # except Exception as e:
            #     print(f"Error for {item.package_name}: {e}")
            #     continue

        
        
        
        # Aggregate metrics for the time window
        if len(window_dataset) == 0:
            continue
        total_commits = sum(repo.commits for repo in window_dataset)
        total_issues = sum(repo.issues for repo in window_dataset)
        total_contributors = sum(repo.contributors for repo in window_dataset)
        # avg_resolution_hours = (
        #     sum(repo.avg_resolution_hours for repo in window_dataset if repo.avg_resolution_hours is not None) /
        #     len([repo for repo in window_dataset if repo.avg_resolution_hours is not None])
        #     if any(repo.avg_resolution_hours is not None for repo in window_dataset) else None
        # )
        avg_commits_per_repo = total_commits / len(window_dataset)
        avg_issues_per_repo = total_issues / len(window_dataset)
        avg_contributors_per_repo = total_contributors / len(window_dataset)

        aggregated_metrics = AggregatedMetrics(
            # total_commits=total_commits,
            # total_issues=total_issues,
            # total_contributors=total_contributors,
            # avg_resolution_hours=avg_resolution_hours or 0.0,
            avg_commits_per_repo=avg_commits_per_repo,
            avg_issues_per_repo=avg_issues_per_repo,
            avg_contributors_per_repo=avg_contributors_per_repo
        )

        time_series.append(TimeSeriesPoint(start=start, end=end, metrics=aggregated_metrics))

    # plot_metrics(dataset)
    print("Done. Time series data points:")
    for point in time_series:
        print(f"{point.start.date()} - {point.end.date()}: {point.metrics}")
    
    
if __name__ == "__main__":
    asyncio.run(main())