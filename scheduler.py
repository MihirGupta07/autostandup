import os
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from database import SessionLocal
import models
from summarizer import generate_standup, detect_flags
from slack_bot import post_team_standup

_scheduler = AsyncIOScheduler()


async def run_standups(db=None):
    """Generate and post standups for all workspaces."""
    close_db = db is None
    if close_db:
        db = SessionLocal()

    try:
        since = datetime.now(timezone.utc) - timedelta(hours=24)
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


def start_scheduler():
    cron = os.getenv("STANDUP_CRON", "0 9 * * 1-5")
    minute, hour, day, month, day_of_week = cron.split()

    _scheduler.add_job(
        run_standups,
        CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
        ),
        id="daily_standup",
        replace_existing=True,
    )
    _scheduler.start()
    print(f"Scheduler started — cron: {cron}")
