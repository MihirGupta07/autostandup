import os
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from database import SessionLocal
import models
from summarizer import generate_standup, detect_flags
from slack_bot import post_team_standup

_scheduler = AsyncIOScheduler()


async def run_standups(db=None, hours: int = 24):
    """Generate and post standups for all workspaces, covering the last `hours` of activity."""
    close_db = db is None
    if close_db:
        db = SessionLocal()

    try:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        workspaces = db.query(models.Workspace).all()

        for workspace in workspaces:
            members = db.query(models.Member).filter_by(workspace_id=workspace.id).all()
            if not members:
                continue

            standups = []
            for member in members:
                events = (
                    db.query(models.ActivityEvent)
                    .filter(
                        models.ActivityEvent.member_id == member.id,
                        models.ActivityEvent.occurred_at >= since,
                    )
                    .order_by(models.ActivityEvent.occurred_at.desc())
                    .all()
                )

                name = member.display_name or member.git_username
                standup_text = generate_standup(name, events)
                flags = detect_flags(name, events)
                standups.append({"member": name, "standup": standup_text, "flags": flags})
                content = standup_text.split("\n", 1)[1] if "\n" in standup_text else standup_text
                db.add(models.StandupEntry(
                    workspace_id=workspace.id,
                    member_name=name,
                    content=content,
                    flags=flags,
                    ran_at=datetime.now(timezone.utc),
                ))

            db.commit()
            post_team_standup(workspace.slack_bot_token, workspace.slack_channel_id, standups)
            print(f"[{datetime.now()}] Standup posted for workspace {workspace.id}")

    finally:
        if close_db:
            db.close()


def _cron_to_trigger(cron: str) -> CronTrigger:
    minute, hour, day, month, day_of_week = cron.split()
    return CronTrigger(
        minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week,
    )


def start_scheduler():
    # Prefer the workspace's configured cron (falls back to env, then 9am Mon-Fri)
    cron = os.getenv("STANDUP_CRON", "0 9 * * 1-5")
    try:
        db = SessionLocal()
        ws = db.query(models.Workspace).first()
        if ws and ws.standup_cron:
            cron = ws.standup_cron
        db.close()
    except Exception:
        pass

    _scheduler.add_job(
        run_standups,
        _cron_to_trigger(cron),
        id="daily_standup",
        replace_existing=True,
    )
    _scheduler.start()
    print(f"Scheduler started — cron: {cron}")


def reschedule(cron: str):
    """Live-update the cron of the daily standup job."""
    _scheduler.reschedule_job("daily_standup", trigger=_cron_to_trigger(cron))
    print(f"Rescheduled standup — cron: {cron}")
