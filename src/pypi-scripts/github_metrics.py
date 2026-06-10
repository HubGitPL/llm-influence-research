
from typing import Any
from pydantic import BaseModel
from datetime import datetime, timedelta
from queries.graphql import (
    COMMIT_COUNT_QUERY, 
    ACTIVE_CONTRIBUTORS_QUERY, 
    ISSUE_COUNT_QUERY, ISSUE_SEARCH_TEMPLATE, 
    CLOSED_ISSUES_QUERY, CLOSED_ISSUES_TEMPLATE
)
from utils.github_api import execute_github_graphql


class RepoMetrics(BaseModel):
    package_name: str
    commits: int
    issues: int
    contributors: int
    avg_resolution_hours: float | None


async def get_commit_count(
    owner: str,
    repo: str,
    since: datetime,
    until: datetime,
) -> int:
    """
    Returns the total number of commits made by contributors to the specified repository
    between the given start and end dates.
    """

    variables = {
        "owner": owner,
        "repo": repo,
        "since": f"{since.isoformat()}Z",
        "until": f"{until.isoformat()}Z",
    }

    data = await execute_github_graphql(COMMIT_COUNT_QUERY, variables)
    repository: Any = data.get("data", {}).get("repository") or {}
    branch_ref = repository.get("defaultBranchRef")
    if not branch_ref:
        return 0
    return int(branch_ref["target"]["history"]["totalCount"])



#TODO 100 limit for contributors, pagination for more
async def get_active_contributors_count(
    owner: str,
    repo: str,
    since: datetime,
    until: datetime,
) -> int:
    """
    Returns the number of unique contributors who have pushed at least one commit in the given time range.
    """
    variables = {
        "owner": owner,
        "repo": repo,
        "since": f"{since.isoformat()}Z",
        "until": f"{until.isoformat()}Z",
    }

    data = await execute_github_graphql(ACTIVE_CONTRIBUTORS_QUERY, variables)

    repository: Any = data.get("data", {}).get("repository") or {}
    branch_ref = repository.get("defaultBranchRef")

    if not branch_ref:
        return 0

    commits: list[Any] = branch_ref["target"]["history"].get("nodes") or []

    # unique emails of authors who made commits in the given time range
    unique_authors: set[str] = set()
    for commit in commits:
        author_info = commit.get("author")
        if author_info and author_info.get("email"):
            unique_authors.add(author_info["email"])

    return len(unique_authors)



async def get_issue_count(
    owner: str,
    repo: str,
    since: datetime,
    until: datetime,
) -> int:
    """
    Returns the number of issues created in the specified time range.
    The logic for building the query has been delegated to the queries module.
    """
    since_iso = f"{since.isoformat()}Z"
    until_iso = f"{until.isoformat()}Z"

    search_query = ISSUE_SEARCH_TEMPLATE.format(
        owner=owner,
        repo=repo,
        since=since_iso,
        until=until_iso
    )

    variables = {"searchQuery": search_query}
    data = await execute_github_graphql(ISSUE_COUNT_QUERY, variables)
    
    search_results: Any = data.get("data", {}).get("search") or {}
    return int(search_results.get("issueCount", 0))




async def get_average_issue_resolution_time(
    owner: str,
    repo: str,
    since: datetime,
    until: datetime,
) -> timedelta | None:
    """
    Calculates the average time it takes for issues to be resolved (closed) in the specified repository
    between the given start and end dates. Only considers issues that were both created and closed within the time range.
    Returns the average resolution time as a timedelta object, or None if there are no closed issues in the specified period.
    """
    since_iso = f"{since.isoformat()}Z"
    until_iso = f"{until.isoformat()}Z"

    search_query = CLOSED_ISSUES_TEMPLATE.format(
        owner=owner,
        repo=repo,
        since=since_iso,
        until=until_iso
    )

    total_duration = timedelta()
    issue_count = 0
    has_next_page = True
    after_cursor = None

    while has_next_page:
        variables: dict[str, Any] = {
            "searchQuery": search_query,
            "after": after_cursor
        }

        data = await execute_github_graphql(CLOSED_ISSUES_QUERY, variables)
        search_data: Any = data.get("data", {}).get("search") or {}
        issues: list[Any] = search_data.get("nodes") or []

        for issue in issues:
            if not issue.get("createdAt") or not issue.get("closedAt"):
                continue
                
            created_at = datetime.fromisoformat(issue["createdAt"].replace("Z", "+00:00"))
            closed_at = datetime.fromisoformat(issue["closedAt"].replace("Z", "+00:00"))

            duration = closed_at - created_at
            total_duration += duration
            issue_count += 1

        page_info: Any = search_data.get("pageInfo") or {}
        has_next_page = page_info.get("hasNextPage", False)
        after_cursor = page_info.get("endCursor")

    if issue_count == 0:
        return None

    return total_duration / issue_count