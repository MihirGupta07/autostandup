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
from scheduler import run_standups, start_scheduler

load_dotenv()

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="AutoStandup", redirect_slashes=False)
templates = Jinja2Templates(directory="templates")

GITLAB_WEBHOOK_SECRET = os.getenv("GITLAB_WEBHOOK_SECRET", "")
SLACK_CLIENT_ID = os.getenv("SLACK_CLIENT_ID", "")
SLACK_CLIENT_SECRET = os.getenv("SLACK_CLIENT_SECRET", "")
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
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
async def web_run_standup(db: Session = Depends(get_db)):
    await run_standups(db)
    return RedirectResponse("/", status_code=303)


@app.get("/setup")
def setup_page(request: Request, db: Session = Depends(get_db)):
    workspace = db.query(models.Workspace).first()
    members = db.query(models.Member).all() if workspace else []
    connected_repos = db.query(models.GitHubRepo).filter_by(workspace_id=workspace.id).all() if workspace else []

    channels = []
    slack_users = []
    available_repos = []

    if workspace:
        client = SlackClient(token=workspace.slack_bot_token)

        if not workspace.slack_channel_id:
            try:
                resp = client.conversations_list(types="public_channel", limit=200)
                channels = sorted(
                    [c for c in resp["channels"] if not c.get("is_archived")],
                    key=lambda c: c["name"],
                )
            except Exception as e:
                print(f"Slack channels error: {e}")

        elif not workspace.github_token:
            pass  # show GitHub connect button

        elif not connected_repos:
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
        "channels": channels,
        "slack_users": slack_users,
        "available_repos": available_repos,
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

    db.add(models.Member(
        workspace_id=workspace.id,
        git_username=git_username,
        slack_user_id=slack_user_id,
        display_name=display_name,
    ))
    db.commit()
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
    if GITLAB_WEBHOOK_SECRET and token != GITLAB_WEBHOOK_SECRET:
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
