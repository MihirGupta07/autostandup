from datetime import datetime, timezone


def normalize_github(event_type: str, payload: dict) -> list[dict]:
    """Convert a GitHub webhook payload into a list of canonical activity events."""
    events = []
    now = datetime.now(timezone.utc)

    if event_type == "push":
        actor = payload.get("pusher", {}).get("name", "")
        repo = payload.get("repository", {}).get("name", "")
        branch = payload.get("ref", "").replace("refs/heads/", "")
        for commit in payload.get("commits", []):
            msg = commit.get("message", "").split("\n")[0][:200]
            events.append({
                "actor": actor,
                "event_type": "commit",
                "title": msg,
                "url": commit.get("url"),
                "branch": branch,
                "repo": repo,
                "occurred_at": now,
            })

    elif event_type == "pull_request":
        action = payload.get("action", "")
        pr = payload.get("pull_request", {})
        actor = pr.get("user", {}).get("login", "")
        repo = payload.get("repository", {}).get("name", "")

        type_map = {
            "opened": "pr_opened",
            "closed": "pr_merged" if pr.get("merged") else "pr_closed",
            "review_requested": "pr_review_requested",
        }
        mapped = type_map.get(action)
        if mapped:
            events.append({
                "actor": actor,
                "event_type": mapped,
                "title": pr.get("title", ""),
                "url": pr.get("html_url"),
                "branch": pr.get("head", {}).get("ref"),
                "repo": repo,
                "occurred_at": now,
            })

    elif event_type == "pull_request_review":
        if payload.get("action") == "submitted":
            review = payload.get("review", {})
            pr = payload.get("pull_request", {})
            actor = payload.get("sender", {}).get("login", "")
            repo = payload.get("repository", {}).get("name", "")
            state = review.get("state", "commented")
            events.append({
                "actor": actor,
                "event_type": f"pr_review_{state}",
                "title": f"Reviewed: {pr.get('title', '')}",
                "url": pr.get("html_url"),
                "branch": pr.get("head", {}).get("ref"),
                "repo": repo,
                "occurred_at": now,
            })

    return events


def normalize_gitlab(payload: dict) -> list[dict]:
    """Convert a GitLab webhook payload into a list of canonical activity events."""
    events = []
    now = datetime.now(timezone.utc)
    event_name = payload.get("event_name") or payload.get("event_type", "")

    if event_name == "push":
        actor = payload.get("user_username", "")
        repo = payload.get("project", {}).get("name", "")
        branch = payload.get("ref", "").replace("refs/heads/", "")
        for commit in payload.get("commits", []):
            msg = commit.get("message", "").split("\n")[0][:200]
            events.append({
                "actor": actor,
                "event_type": "commit",
                "title": msg,
                "url": commit.get("url"),
                "branch": branch,
                "repo": repo,
                "occurred_at": now,
            })

    elif event_name == "merge_request":
        obj = payload.get("object_attributes", {})
        actor = payload.get("user", {}).get("username", "")
        repo = payload.get("project", {}).get("name", "")
        action = obj.get("action", "")

        type_map = {
            "open": "pr_opened",
            "merge": "pr_merged",
            "close": "pr_closed",
        }
        mapped = type_map.get(action)
        if mapped:
            events.append({
                "actor": actor,
                "event_type": mapped,
                "title": obj.get("title", ""),
                "url": obj.get("url"),
                "branch": obj.get("source_branch"),
                "repo": repo,
                "occurred_at": now,
            })

    return events
