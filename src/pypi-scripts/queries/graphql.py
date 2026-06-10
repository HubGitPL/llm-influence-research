COMMIT_COUNT_QUERY = """
query(
    $owner: String!,
    $repo: String!,
    $since: GitTimestamp!,
    $until: GitTimestamp!
) {
    repository(owner: $owner, name: $repo) {
        defaultBranchRef {
            target {
                ... on Commit {
                    history(since: $since, until: $until) {
                        totalCount
                    }
                }
            }
        }
    }
}
"""


ACTIVE_CONTRIBUTORS_QUERY = """
query(
    $owner: String!,
    $repo: String!,
    $since: GitTimestamp!,
    $until: GitTimestamp!
) {
    repository(owner: $owner, name: $repo) {
        defaultBranchRef {
            target {
                ... on Commit {
                    history(since: $since, until: $until, first: 100) {
                        nodes {
                            author {
                                email
                            }
                        }
                    }
                }
            }
        }
    }
}
"""


ISSUE_COUNT_QUERY = """
query($searchQuery: String!) {
    search(query: $searchQuery, type: ISSUE, first: 1) {
        issueCount
    }
}
"""


ISSUE_SEARCH_TEMPLATE = "repo:{owner}/{repo} type:issue created:{since}..{until}"





CLOSED_ISSUES_QUERY = """
query($searchQuery: String!, $after: String) {
    search(query: $searchQuery, type: ISSUE, first: 100, after: $after) {
        pageInfo {
            hasNextPage
            endCursor
        }
        nodes {
            ... on Issue {
                createdAt
                closedAt
            }
        }
    }
}
"""
CLOSED_ISSUES_TEMPLATE = "repo:{owner}/{repo} type:issue is:closed closed:{since}..{until}"