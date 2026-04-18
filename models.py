import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship
from database import Base


def gen_id():
    return str(uuid.uuid4())


class Workspace(Base):
    __tablename__ = "workspaces"

    id = Column(String, primary_key=True, default=gen_id)
    slack_bot_token = Column(String, nullable=False)
    slack_channel_id = Column(String, nullable=True, default="")
    standup_cron = Column(String, default="0 9 * * 1-5")
    github_token = Column(String, nullable=True)
    github_webhook_secret = Column(String, nullable=True)

    members = relationship("Member", back_populates="workspace")


class Member(Base):
    __tablename__ = "members"

    id = Column(String, primary_key=True, default=gen_id)
    workspace_id = Column(String, ForeignKey("workspaces.id"), nullable=False)
    git_username = Column(String, nullable=False)  # username on GitHub/GitLab
    slack_user_id = Column(String, nullable=False)  # Slack @mention ID e.g. U012AB3CD
    display_name = Column(String)

    workspace = relationship("Workspace", back_populates="members")
    events = relationship("ActivityEvent", back_populates="member")


class ActivityEvent(Base):
    __tablename__ = "activity_events"

    id = Column(String, primary_key=True, default=gen_id)
    workspace_id = Column(String, ForeignKey("workspaces.id"), nullable=True)
    member_id = Column(String, ForeignKey("members.id"), nullable=True)
    source = Column(String, nullable=False)    # 'github' | 'gitlab'
    event_type = Column(String, nullable=False) # 'commit' | 'pr_opened' | 'pr_merged' | ...
    title = Column(Text)
    url = Column(String)
    branch = Column(String)
    repo = Column(String)
    occurred_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    raw = Column(JSON)

    member = relationship("Member", back_populates="events")


class StandupEntry(Base):
    __tablename__ = "standup_entries"

    id = Column(String, primary_key=True, default=gen_id)
    workspace_id = Column(String, ForeignKey("workspaces.id"), nullable=True)
    member_name = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    flags = Column(JSON, default=list)
    ran_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class GitHubRepo(Base):
    __tablename__ = "github_repos"

    id = Column(String, primary_key=True, default=gen_id)
    workspace_id = Column(String, ForeignKey("workspaces.id"), nullable=False)
    full_name = Column(String, nullable=False)   # "owner/repo"
    hook_id = Column(Integer, nullable=True)      # GitHub webhook ID
