#!/usr/bin/env python3

# import os
import asyncio
import repo_sampling
from utils import globals
from utils import github_api
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
) -> RepoMetrics | None:
    
    owner = package.repo_info.repo_owner
    repo_name = package.repo_info.repo_name
    args = (owner, repo_name, start, end)
    try: 
        commits = await github_metrics.get_commit_count(*args)
        issues = await github_metrics.get_issue_count(*args)
        contributors = await github_metrics.get_active_contributors_count(*args)
    except Exception as e:
        print(f"Error processing {package.package_name}: {e}")
        return None

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


    try:
        top_15k_packages = pypi.get_top_packages()
        sampled_packages = repo_sampling.sample_representative_packages(top_15k_packages, n_total=200)
        print(f"Sampled {len(sampled_packages)} packages with GitHub repos.")
        packages = pypi.extract_package_info(sampled_packages)
        print(f"Extracted GitHub data for {len(packages)} packages.")
        
        tasks = [
            github_api.is_repo_accessible(pkg.repo_info.repo_owner, pkg.repo_info.repo_name)
            for pkg in packages
        ]
        results = await asyncio.gather(*tasks)
        packages = [pkg for pkg, exists in zip(packages, results) if exists]
        print(f"{len(packages)} repositories exist and are accessible.")
        





        start_date = datetime(2020, 1, 1)
        end_date = datetime.now()
        interval = timedelta(days=2*90)  # 6 months
        
        time_series: List[TimeSeriesPoint] = []
        for start, end in datetime_range(start_date, end_date, interval):
            print(start, "->", end)
            window_dataset: List[RepoMetrics] = []
            tasks: List[Any] = []
            for package in packages:
                tasks.append(process_repo(package, start, end))
            tmp_dataset = await asyncio.gather(*tasks)
            window_dataset = [repo for repo in tmp_dataset if repo is not None]
            
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
    finally:
        await globals.SESSION.close()
    
    
if __name__ == "__main__":
    asyncio.run(main())