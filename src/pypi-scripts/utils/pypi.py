from datetime import datetime, timedelta

import requests
from typing import Any, List
from urllib.parse import urlparse
from utils.schemas import ExtractedPackageInfo, PyPIReleaseStats

def get_top_packages() -> List[str]:
    url = r"https://hugovk.github.io/top-pypi-packages/top-pypi-packages-30-days.json"
    response = requests.get(url)
    response.raise_for_status()
    data = response.json()
    top_packages = data.get("rows", [])
    top_packages_names = [pkg["project"].strip() for pkg in top_packages]
    return top_packages_names


def get_pypi(name: str) -> Any:
    url = f"https://pypi.org/pypi/{name}/json"
    r = requests.get(url)
    if r.status_code != 200:
        return None
    return r.json()


def get_package_github_repo_url(pypi_info: Any) -> str | None:
    PRIORITY_KEYS = [
        "source",
        "repository",
        "code",
    ]
    
    info = pypi_info.get("info", {})
    
    
    project_urls: Any = info.get("project_urls") or {}
    if not isinstance(project_urls, dict):
        project_urls = {}


    repo_url: str | None = None
    for k, v in project_urls.items(): # type: ignore
        if any(x in k.lower() for x in PRIORITY_KEYS): # type: ignore
            if "github.com" in v.lower(): # type: ignore
                repo_url = str(v) # type: ignore
                break
            
    return repo_url
    


def calculate_pypi_release_stats(pypi_info: Any, since: datetime=datetime(2008, 1, 1), until: datetime=datetime.now()) -> PyPIReleaseStats:
    releases = pypi_info.get("releases", {})
    matching_dates: list[datetime] = []
    
    for _, files in releases.items():
        if not files:
            continue
        
        upload_time_str = files[0].get("upload_time_iso_8601")
        if not upload_time_str:
            continue
            
        upload_time = datetime.fromisoformat(upload_time_str.replace("Z", ""))
        
        if since <= upload_time <= until:
            matching_dates.append(upload_time)
            
    matching_dates.sort()
    
    release_count = len(matching_dates)
    avg_interval: timedelta | None = None
    
    if release_count > 1:
        intervals = [
            matching_dates[i] - matching_dates[i - 1] 
            for i in range(1, len(matching_dates))
        ]
        total_seconds = sum(interval.total_seconds() for interval in intervals)
        avg_interval = timedelta(seconds=total_seconds / len(intervals))
        
    return PyPIReleaseStats(
        release_count=release_count,
        average_interval=avg_interval
    )

    
def extract_github_repo_data(top_packages: List[str], limit: int = 25) -> List[ExtractedPackageInfo]:
   
    valid_limit = limit

    data: List[ExtractedPackageInfo] = []
    for name in top_packages:
        if valid_limit <= 0:
            break
        
        try:
            pypi_info = get_pypi(name)
        except Exception:
            continue
        if not pypi_info:
            continue
        
        repo_url = get_package_github_repo_url(pypi_info)
        if not repo_url:
            continue
        
        parsed = urlparse(repo_url)
        parts = parsed.path.strip("/").split("/")
        if len(parts) < 2:
            continue
        
        repo_owner, repo_name = parts[0], parts[1]
        
        print(".", end="", flush=True)
        
        
        data.append(ExtractedPackageInfo(
            package_name=name, 
            repo_url=repo_url, 
            repo_name=repo_name, 
            repo_owner=repo_owner,
            pypi_info=pypi_info
        ))
        valid_limit -= 1
        
    print()
        
    return data

