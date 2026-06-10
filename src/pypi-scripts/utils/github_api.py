from typing import Any
# from aiohttp_client_cache.session import CachedSession
from utils import globals

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"


async def execute_github_graphql(
    token: str, 
    query: str, 
    variables: dict[str, Any]
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
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


