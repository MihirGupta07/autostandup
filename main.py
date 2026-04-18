import hashlib
import hmac
import json
import os
import secrets
import urllib.parse
from itertools import groupby

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from slack_sdk import WebClient as SlackClient
from sqlalchemy.orm import Session

import models
from database import engine, get_db
from normalizer import normalize_github, normalize_gitlab
from scheduler import run_standups, start_scheduler, reschedule
from slack_bot import post_team_standup
from datetime import datetime, timedelta, timezone

load_dotenv()

models.Base.metadata.create_all(bind=engine)


def _run_migrations():
    """Add columns introduced after initial schema. Idempotent."""
    from sqlalchemy import text
    statements = [
        "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS gitlab_token VARCHAR",
        "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS gitlab_webhook_secret VARCHAR",
        "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS github_token VARCHAR",
        "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS github_webhook_secret VARCHAR",
    ]
    with engine.begin() as conn:
        for stmt in statements:
            try:
                conn.execute(text(stmt))
            except Exception as e:
                print(f"Migration skipped ({stmt}): {e}")


if engine.dialect.name == "postgresql":
    _run_migrations()

app = FastAPI(title="AutoStandup", redirect_slashes=False)
templates = Jinja2Templates(directory="templates")

SLACK_CLIENT_ID = os.getenv("SLACK_CLIENT_ID", "")
SLACK_CLIENT_SECRET = os.getenv("SLACK_CLIENT_SECRET", "")
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
GITLAB_CLIENT_ID = os.getenv("GITLAB_CLIENT_ID", "")
GITLAB_CLIENT_SECRET = os.getenv("GITLAB_CLIENT_SECRET", "")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")


# ── Startup ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    start_scheduler()


# ── GitHub API helpers ────────────────────────────────────────────────────────

def _gh_get(token: str, path: str, **params):
    resp = httpx.get(
        f"https://api.github.com{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        params=params,
    )
    return resp.json() if resp.status_code == 200 else []


def _gh_post(token: str, path: str, data: dict):
    resp = httpx.post(
        f"https://api.github.com{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json=data,
    )
    return resp.json()


# ── GitLab API helpers ────────────────────────────────────────────────────────

def _gl_get(token: str, path: str, **params):
    resp = httpx.get(
        f"https://gitlab.com/api/v4{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )
    return resp.json() if resp.status_code == 200 else []


def _gl_post(token: str, path: str, data: dict):
    resp = httpx.post(
        f"https://gitlab.com/api/v4{path}",
        headers={"Authorization": f"Bearer {token}"},
        json=data,
    )
    return resp.json()


# ── Backfill ──────────────────────────────────────────────────────────────────

def backfill_member(db: Session, workspace, member, days: int = 7):
    """Pull recent commits and PRs/MRs for a member so the first standup isn't empty."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_iso = since.isoformat()
    new_events = 0

    # GitHub
    if workspace.github_token:
        for repo in db.query(models.GitHubRepo).filter_by(workspace_id=workspace.id).all():
            owner, name = repo.full_name.split("/", 1)
            commits = _gh_get(
                workspace.github_token,
                f"/repos/{owner}/{name}/commits",
                author=member.git_username, since=since_iso, per_page=50,
            )
            for c in commits if isinstance(commits, list) else []:
                db.add(models.ActivityEvent(
                    workspace_id=workspace.id, member_id=member.id,
                    source="github", event_type="commit",
                    title=(c.get("commit") or {}).get("message", "").split("\n", 1)[0][:200],
                    url=c.get("html_url"), branch=None, repo=repo.full_name,
                    occurred_at=_parse_iso((c.get("commit") or {}).get("author", {}).get("date")),
                ))
                new_events += 1

            prs = _gh_get(
                workspace.github_token,
                f"/repos/{owner}/{name}/pulls",
                state="all", sort="updated", direction="desc", per_page=30,
            )
            for pr in prs if isinstance(prs, list) else []:
                if (pr.get("user") or {}).get("login") != member.git_username:
                    continue
                updated = _parse_iso(pr.get("updated_at"))
                if updated and updated < since:
                    continue
                etype = "pr_merged" if pr.get("merged_at") else "pr_opened"
                db.add(models.ActivityEvent(
                    workspace_id=workspace.id, member_id=member.id,
                    source="github", event_type=etype,
                    title=pr.get("title", "")[:200], url=pr.get("html_url"),
                    branch=(pr.get("head") or {}).get("ref"), repo=repo.full_name,
                    occurred_at=updated or datetime.now(timezone.utc),
                ))
                new_events += 1

    # GitLab
    if workspace.gitlab_token:
        for project in db.query(models.GitLabProject).filter_by(workspace_id=workspace.id).all():
            commits = _gl_get(
                workspace.gitlab_token,
                f"/projects/{project.project_id}/repository/commits",
                since=since_iso, author=member.git_username, per_page=50,
            )
            for c in commits if isinstance(commits, list) else []:
                db.add(models.ActivityEvent(
                    workspace_id=workspace.id, member_id=member.id,
                    source="gitlab", event_type="commit",
                    title=(c.get("title") or c.get("message") or "")[:200],
                    url=c.get("web_url"), branch=None, repo=project.path_with_namespace,
                    occurred_at=_parse_iso(c.get("committed_date") or c.get("created_at")),
                ))
                new_events += 1

            mrs = _gl_get(
                workspace.gitlab_token,
                f"/projects/{project.project_id}/merge_requests",
                author_username=member.git_username, updated_after=since_iso,
                state="all", per_page=30,
            )
            for mr in mrs if isinstance(mrs, list) else []:
                etype = "pr_merged" if mr.get("state") == "merged" else "pr_opened"
                db.add(models.ActivityEvent(
                    workspace_id=workspace.id, member_id=member.id,
                    source="gitlab", event_type=etype,
                    title=mr.get("title", "")[:200], url=mr.get("web_url"),
                    branch=mr.get("source_branch"), repo=project.path_with_namespace,
                    occurred_at=_parse_iso(mr.get("updated_at")),
                ))
                new_events += 1

    db.commit()
    print(f"Backfilled {new_events} events for {member.display_name or member.git_username}")
    return new_events


def _parse_iso(s):
    if not s:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)

def describe_cron(cron: str) -> str:
    parts = cron.strip().split()
    if len(parts) != 5:
        return cron

    minute, hour, day, month, dow = parts

    def format_time(hour_str: str, minute_str: str) -> str:
        try:
            h = int(hour_str)
            m = int(minute_str)
        except ValueError:
            return f"{hour_str}:{minute_str.zfill(2)}"
        am_pm = "AM" if h < 12 else "PM"
        h12 = h if 1 <= h <= 12 else (h - 12 if h > 12 else 12)
        return f"{h12}:{m:02d} {am_pm}"

    time_text = format_time(hour, minute)
    if day == "*" and month == "*" and dow == "1-5":
        return f"Every weekday at {time_text}"
    if day == "*" and month == "*" and dow == "*":
        return f"Every day at {time_text}"
    if day == "*" and month == "*" and dow.isdigit():
        days = {
            "0": "Sunday",
            "1": "Monday",
            "2": "Tuesday",
            "3": "Wednesday",
            "4": "Thursday",
            "5": "Friday",
            "6": "Saturday",
        }
        return f"Every {days.get(dow, dow)} at {time_text}"
    if day != "*" and month == "*" and dow == "*":
        return f"On day {day} of every month at {time_text}"
    return f"Cron schedule: {cron}"

# ── Web UI ────────────────────────────────────────────────────────────────────

@app.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    entries = (
        db.query(models.StandupEntry)
        .order_by(models.StandupEntry.ran_at.desc())
        .limit(200)
        .all()
    )
    entries_by_date = [
        (date_str, list(group))
        for date_str, group in groupby(
            entries, key=lambda e: e.ran_at.strftime("%B %d, %Y")
        )
    ]
    return templates.TemplateResponse(request, "dashboard.html", {
        "entries_by_date": entries_by_date,
    })


@app.post("/run-standup")
async def web_run_standup(
    hours: int = Form(24),
    custom_hours: str = Form(""),
    db: Session = Depends(get_db),
):
    # "custom" selection overrides the preset
    if custom_hours.strip():
        try:
            hours = max(1, min(int(custom_hours), 720))  # clamp 1h..30d
        except ValueError:
            pass
    await run_standups(db, hours=hours)
    return RedirectResponse("/", status_code=303)


@app.post("/repost-standup")
def web_repost_standup(date: str = Form(...), db: Session = Depends(get_db)):
    """Re-post all standup entries from a given date (format: "Month DD, YYYY") to Slack."""
    workspace = db.query(models.Workspace).first()
    if not workspace or not workspace.slack_channel_id:
        return RedirectResponse("/", status_code=303)

    try:
        target = datetime.strptime(date, "%B %d, %Y").date()
    except ValueError:
        return RedirectResponse("/", status_code=303)

    entries = (
        db.query(models.StandupEntry)
        .filter(models.StandupEntry.workspace_id == workspace.id)
        .all()
    )
    matching = [e for e in entries if e.ran_at.date() == target]
    if not matching:
        return RedirectResponse("/", status_code=303)

    standups = [
        {"member": e.member_name, "standup": f"*{e.member_name}*\n{e.content}", "flags": e.flags or []}
        for e in matching
    ]
    post_team_standup(workspace.slack_bot_token, workspace.slack_channel_id, standups)
    return RedirectResponse("/", status_code=303)


@app.get("/setup")
def setup_page(request: Request, db: Session = Depends(get_db)):
    workspace = db.query(models.Workspace).first()
    members = db.query(models.Member).all() if workspace else []
    connected_repos = db.query(models.GitHubRepo).filter_by(workspace_id=workspace.id).all() if workspace else []
    connected_projects = db.query(models.GitLabProject).filter_by(workspace_id=workspace.id).all() if workspace else []

    channels = []
    slack_users = []
    available_repos = []
    available_projects = []

    provider_connected = bool(workspace and (workspace.github_token or workspace.gitlab_token))
    has_any_source = bool(connected_repos or connected_projects)

    if workspace:
        client = SlackClient(token=workspace.slack_bot_token)

        try:
            resp = client.conversations_list(types="public_channel", limit=200)
            channels = sorted(
                [c for c in resp["channels"] if not c.get("is_archived")],
                key=lambda c: c["name"],
            )
        except Exception as e:
            print(f"Slack channels error: {e}")

        if not workspace.slack_channel_id:
            pass  # will show channel picker

        elif not provider_connected:
            pass  # show GitHub/GitLab connect buttons

        elif not has_any_source:
            if workspace.github_token:
                try:
                    connected_names = {r.full_name for r in connected_repos}
                    all_repos = _gh_get(
                        workspace.github_token, "/user/repos",
                        per_page=100, sort="updated",
                        affiliation="owner,organization_member",
                    )
                    available_repos = sorted(
                        [r for r in all_repos if r.get("full_name") not in connected_names],
                        key=lambda r: r["full_name"],
                    )
                except Exception as e:
                    print(f"GitHub repos error: {e}")
            if workspace.gitlab_token:
                try:
                    connected_ids = {p.project_id for p in connected_projects}
                    all_projects = _gl_get(
                        workspace.gitlab_token, "/projects",
                        membership=True, per_page=100, order_by="last_activity_at",
                    )
                    available_projects = sorted(
                        [p for p in all_projects if p.get("id") not in connected_ids],
                        key=lambda p: p["path_with_namespace"],
                    )
                except Exception as e:
                    print(f"GitLab projects error: {e}")

        else:
            try:
                existing_ids = {m.slack_user_id for m in members}
                resp = client.users_list()
                slack_users = sorted(
                    [
                        u for u in resp["members"]
                        if not u.get("is_bot")
                        and not u.get("deleted")
                        and u.get("id") != "USLACKBOT"
                        and u.get("id") not in existing_ids
                    ],
                    key=lambda u: u.get("real_name", ""),
                )
            except Exception as e:
                print(f"Slack users error: {e}")

    return templates.TemplateResponse(request, "setup.html", {
        "workspace": workspace,
        "members": members,
        "connected_repos": connected_repos,
        "connected_projects": connected_projects,
        "channels": channels,
        "slack_users": slack_users,
        "available_repos": available_repos,
        "available_projects": available_projects,
        "standup_schedule_human": describe_cron(workspace.standup_cron) if workspace else "Every weekday at 9:00 AM",
    })


# ── Slack OAuth ───────────────────────────────────────────────────────────────

@app.get("/oauth/slack")
def slack_oauth_start():
    params = urllib.parse.urlencode({
        "client_id": SLACK_CLIENT_ID,
        "scope": "chat:write,channels:read,channels:join,users:read",
        "redirect_uri": f"{APP_BASE_URL}/oauth/slack/callback",
    })
    return RedirectResponse(f"https://slack.com/oauth/v2/authorize?{params}")


@app.get("/oauth/slack/callback")
def slack_oauth_callback(code: str, db: Session = Depends(get_db)):
    r = httpx.post(
        "https://slack.com/api/oauth.v2.access",
        auth=(SLACK_CLIENT_ID, SLACK_CLIENT_SECRET),
        data={"code": code, "redirect_uri": f"{APP_BASE_URL}/oauth/slack/callback"},
    )
    resp = r.json()
    if not resp.get("ok"):
        print(f"Slack OAuth failed: {resp}")
        raise HTTPException(400, detail=resp.get("error", "OAuth failed"))

    db.query(models.GitHubRepo).delete()
    db.query(models.GitLabProject).delete()
    db.query(models.Member).delete()
    db.query(models.Workspace).delete()
    db.commit()
    workspace = models.Workspace(
        slack_bot_token=resp["access_token"],
        slack_channel_id="",
        standup_cron="0 9 * * 1-5",
    )
    db.add(workspace)
    db.commit()
    return RedirectResponse("/setup", status_code=303)


# ── GitHub OAuth ──────────────────────────────────────────────────────────────

@app.get("/oauth/github")
def github_oauth_start():
    params = urllib.parse.urlencode({
        "client_id": GITHUB_CLIENT_ID,
        "scope": "admin:repo_hook,read:org",
        "redirect_uri": f"{APP_BASE_URL}/oauth/github/callback",
    })
    return RedirectResponse(f"https://github.com/login/oauth/authorize?{params}")


@app.get("/oauth/github/callback")
def github_oauth_callback(code: str, db: Session = Depends(get_db)):
    resp = httpx.post(
        "https://github.com/login/oauth/access_token",
        data={
            "client_id": GITHUB_CLIENT_ID,
            "client_secret": GITHUB_CLIENT_SECRET,
            "code": code,
            "redirect_uri": f"{APP_BASE_URL}/oauth/github/callback",
        },
        headers={"Accept": "application/json"},
    )
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise HTTPException(400, detail=data.get("error_description", "GitHub OAuth failed"))

    workspace = db.query(models.Workspace).first()
    if workspace:
        workspace.github_token = token
        workspace.github_webhook_secret = secrets.token_hex(20)
        db.commit()
    return RedirectResponse("/setup", status_code=303)


# ── GitLab OAuth ──────────────────────────────────────────────────────────────

@app.get("/oauth/gitlab")
def gitlab_oauth_start():
    params = urllib.parse.urlencode({
        "client_id": GITLAB_CLIENT_ID,
        "redirect_uri": f"{APP_BASE_URL}/oauth/gitlab/callback",
        "response_type": "code",
        "scope": "api",
    })
    return RedirectResponse(f"https://gitlab.com/oauth/authorize?{params}")


@app.get("/oauth/gitlab/callback")
def gitlab_oauth_callback(code: str, db: Session = Depends(get_db)):
    resp = httpx.post(
        "https://gitlab.com/oauth/token",
        data={
            "client_id": GITLAB_CLIENT_ID,
            "client_secret": GITLAB_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": f"{APP_BASE_URL}/oauth/gitlab/callback",
        },
    )
    data = resp.json()
    token = data.get("access_token")
    if not token:
        print(f"GitLab OAuth failed: {data}")
        raise HTTPException(400, detail=data.get("error_description", "GitLab OAuth failed"))

    workspace = db.query(models.Workspace).first()
    if workspace:
        workspace.gitlab_token = token
        workspace.gitlab_webhook_secret = secrets.token_hex(20)
        db.commit()
    return RedirectResponse("/setup", status_code=303)


# ── Setup actions ─────────────────────────────────────────────────────────────

@app.post("/setup/channel")
def save_channel(
    channel_id: str = Form(...),
    standup_cron: str = Form("0 9 * * 1-5"),
    db: Session = Depends(get_db),
):
    workspace = db.query(models.Workspace).first()
    if workspace:
        workspace.slack_channel_id = channel_id
        workspace.standup_cron = standup_cron
        db.commit()
        try:
            SlackClient(token=workspace.slack_bot_token).conversations_join(channel=channel_id)
        except Exception:
            pass
        try:
            reschedule(standup_cron)
        except Exception as e:
            print(f"Reschedule failed: {e}")
    return RedirectResponse("/setup", status_code=303)


@app.post("/setup/settings")
def save_settings(
    channel_id: str = Form(...),
    standup_cron: str = Form(...),
    db: Session = Depends(get_db),
):
    """Edit schedule and/or channel after initial setup."""
    workspace = db.query(models.Workspace).first()
    if not workspace:
        return RedirectResponse("/setup", status_code=303)

    channel_changed = channel_id != workspace.slack_channel_id
    workspace.slack_channel_id = channel_id
    workspace.standup_cron = standup_cron
    db.commit()

    if channel_changed:
        try:
            SlackClient(token=workspace.slack_bot_token).conversations_join(channel=channel_id)
        except Exception:
            pass
    try:
        reschedule(standup_cron)
    except Exception as e:
        print(f"Reschedule failed: {e}")
    return RedirectResponse("/setup", status_code=303)


@app.post("/setup/repos")
async def save_repos(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    selected = form.getlist("repos")

    workspace = db.query(models.Workspace).first()
    if not workspace or not selected:
        return RedirectResponse("/setup", status_code=303)

    for full_name in selected:
        owner, repo = full_name.split("/", 1)
        result = _gh_post(
            workspace.github_token,
            f"/repos/{owner}/{repo}/hooks",
            {
                "name": "web",
                "active": True,
                "events": ["push", "pull_request", "pull_request_review"],
                "config": {
                    "url": f"{APP_BASE_URL}/webhooks/github",
                    "content_type": "json",
                    "secret": workspace.github_webhook_secret,
                },
            },
        )
        db.add(models.GitHubRepo(
            workspace_id=workspace.id,
            full_name=full_name,
            hook_id=result.get("id"),
        ))

    db.commit()
    return RedirectResponse("/setup", status_code=303)


@app.post("/setup/repos/{repo_id}/delete")
def delete_repo(repo_id: str, db: Session = Depends(get_db)):
    repo = db.query(models.GitHubRepo).filter_by(id=repo_id).first()
    if repo:
        workspace = db.query(models.Workspace).first()
        if workspace and repo.hook_id:
            owner, name = repo.full_name.split("/", 1)
            try:
                httpx.delete(
                    f"https://api.github.com/repos/{owner}/{name}/hooks/{repo.hook_id}",
                    headers={"Authorization": f"Bearer {workspace.github_token}"},
                )
            except Exception:
                pass
        db.delete(repo)
        db.commit()
    return RedirectResponse("/setup", status_code=303)


@app.post("/setup/projects")
async def save_projects(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    selected = form.getlist("projects")  # values are GitLab numeric project IDs

    workspace = db.query(models.Workspace).first()
    if not workspace or not selected:
        return RedirectResponse("/setup", status_code=303)

    for pid_str in selected:
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        project = _gl_get(workspace.gitlab_token, f"/projects/{pid}")
        if not project or not project.get("path_with_namespace"):
            continue

        result = _gl_post(
            workspace.gitlab_token,
            f"/projects/{pid}/hooks",
            {
                "url": f"{APP_BASE_URL}/webhooks/gitlab",
                "token": workspace.gitlab_webhook_secret,
                "push_events": True,
                "merge_requests_events": True,
                "enable_ssl_verification": True,
            },
        )
        db.add(models.GitLabProject(
            workspace_id=workspace.id,
            project_id=pid,
            path_with_namespace=project["path_with_namespace"],
            hook_id=result.get("id"),
        ))

    db.commit()
    return RedirectResponse("/setup", status_code=303)


@app.post("/setup/projects/{project_row_id}/delete")
def delete_project(project_row_id: str, db: Session = Depends(get_db)):
    project = db.query(models.GitLabProject).filter_by(id=project_row_id).first()
    if project:
        workspace = db.query(models.Workspace).first()
        if workspace and project.hook_id:
            try:
                httpx.delete(
                    f"https://gitlab.com/api/v4/projects/{project.project_id}/hooks/{project.hook_id}",
                    headers={"Authorization": f"Bearer {workspace.gitlab_token}"},
                )
            except Exception:
                pass
        db.delete(project)
        db.commit()
    return RedirectResponse("/setup", status_code=303)


@app.post("/setup/members")
def add_member_ui(
    slack_user_id: str = Form(...),
    git_username: str = Form(...),
    db: Session = Depends(get_db),
):
    workspace = db.query(models.Workspace).first()
    if not workspace:
        return RedirectResponse("/setup", status_code=303)

    display_name = slack_user_id
    try:
        info = SlackClient(token=workspace.slack_bot_token).users_info(user=slack_user_id)
        display_name = (
            info["user"].get("real_name")
            or info["user"]["profile"].get("display_name")
            or slack_user_id
        )
    except Exception:
        pass

    member = models.Member(
        workspace_id=workspace.id,
        git_username=git_username,
        slack_user_id=slack_user_id,
        display_name=display_name,
    )
    db.add(member)
    db.commit()
    db.refresh(member)

    try:
        backfill_member(db, workspace, member, days=7)
    except Exception as e:
        print(f"Backfill failed for {git_username}: {e}")

    return RedirectResponse("/setup", status_code=303)


@app.post("/setup/members/{member_id}/delete")
def delete_member_ui(member_id: str, db: Session = Depends(get_db)):
    member = db.query(models.Member).filter_by(id=member_id).first()
    if member:
        db.delete(member)
        db.commit()
    return RedirectResponse("/setup", status_code=303)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


# ── Webhook helpers ───────────────────────────────────────────────────────────

def _verify_github(payload: bytes, signature: str, secret: str) -> bool:
    if not secret:
        return True
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _save_events(events: list[dict], source: str, raw: dict, db: Session):
    for event in events:
        member = (
            db.query(models.Member)
            .filter_by(git_username=event["actor"])
            .first()
        )
        db.add(
            models.ActivityEvent(
                workspace_id=member.workspace_id if member else None,
                member_id=member.id if member else None,
                source=source,
                event_type=event["event_type"],
                title=event["title"],
                url=event.get("url"),
                branch=event.get("branch"),
                repo=event.get("repo"),
                occurred_at=event["occurred_at"],
                raw=raw,
            )
        )
    db.commit()


# ── Webhooks ──────────────────────────────────────────────────────────────────

@app.post("/webhooks/github")
async def github_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    workspace = db.query(models.Workspace).first()
    secret = (workspace.github_webhook_secret if workspace else "") or ""
    if not _verify_github(payload, sig, secret):
        raise HTTPException(status_code=401, detail="Invalid signature")

    event_type = request.headers.get("X-GitHub-Event", "")
    data = json.loads(payload)
    events = normalize_github(event_type, data)
    _save_events(events, "github", data, db)
    return {"received": len(events)}


@app.post("/webhooks/gitlab")
async def gitlab_webhook(request: Request, db: Session = Depends(get_db)):
    token = request.headers.get("X-Gitlab-Token", "")
    workspace = db.query(models.Workspace).first()
    secret = (workspace.gitlab_webhook_secret if workspace else "") or ""
    if secret and token != secret:
        raise HTTPException(status_code=401, detail="Invalid token")

    data = await request.json()
    events = normalize_gitlab(data)
    _save_events(events, "gitlab", data, db)
    return {"received": len(events)}


# ── Admin API ─────────────────────────────────────────────────────────────────

@app.post("/admin/trigger-standup")
async def trigger_standup(db: Session = Depends(get_db)):
    await run_standups(db)
    return {"status": "triggered"}
