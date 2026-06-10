from datetime import timedelta
from pydantic import BaseModel
from typing import Any

class PyPIReleaseStats(BaseModel):
    release_count: int
    average_interval: timedelta | None
    
class ExtractedPackageInfo(BaseModel):
    package_name: str
    repo_url: str
    repo_name: str
    repo_owner: str
    pypi_info: Any = None

class RepoMetrics(BaseModel):
    package_name: str
    commits: int
    issues: int
    contributors: int
    avg_resolution_hours: float | None