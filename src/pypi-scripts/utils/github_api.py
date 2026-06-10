from typing import Any

from aiohttp import ClientResponseError
# from aiohttp_client_cache.session import CachedSession
from utils import globals

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"


async def execute_github_graphql(
    query: str, 
    variables: dict[str, Any]
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {globals.TOKEN}",
        "Content-Type": "application/json",
    }
    
    async with globals.SESSION.post(
        GITHUB_GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers=headers,
    ) as response:
        data: dict[str, Any] = await response.json()
    
    response.raise_for_status()
    
    if "errors" in data:
        raise RuntimeError(f"GitHub API Error: {data['errors']}")
        
    return data


async def execute_github_rest(endpoint: str) -> dict[str, Any]:
    url = f"https://api.github.com/{endpoint.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {globals.TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",  
    }
    
    async with globals.SESSION.get(url, headers=headers) as response:
        response.raise_for_status()
        return await response.json()


async def is_repo_accessible(owner: str, repo_name: str) -> bool:
    endpoint = f"repos/{owner}/{repo_name}"
    
    try:
        repo_data = await execute_github_rest(endpoint)
        # Alternatywnie: repo_data.get("visibility") == "public"
        return repo_data.get("private") is False
        
    except ClientResponseError as e:
        if e.status == 404:
            return False
            
        if e.status in (401, 403):
            print(f"GitHub REST API permissions or rate limit error (Status {e.status}): {e.message}")
            raise e
            
        print(f"Unexpected HTTP error while checking repository visibility: {e.status} - {e.message}")
        return False
        
    except Exception as e:
        print(f"Network or connection error while checking repository visibility: {e}")
        raise e
