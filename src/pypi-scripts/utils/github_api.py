import requests
from typing import Any

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"

def execute_github_graphql(token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """Wykonuje zapytanie GraphQL do API GitHub i dba o podstawową walidację."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    
    response = requests.post(
        GITHUB_GRAPHQL_URL,
        headers=headers,
        json={"query": query, "variables": variables},
        timeout=30,
    )
    response.raise_for_status()
    
    data: dict[str, Any] = response.json()
    
    if "errors" in data:
        raise RuntimeError(f"GitHub API Error: {data['errors']}")
        
    return data


