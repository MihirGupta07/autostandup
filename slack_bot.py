import os
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv

load_dotenv()

_client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))


def post_team_standup(channel_id: str, standups: list[dict]):
    """Post all team standups as a single Slack message with Block Kit formatting."""
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "📋 Daily Standup", "emoji": True},
        },
        {"type": "divider"},
    ]

    for item in standups:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": item["standup"]},
        })
        if item.get("flags"):
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "\n".join(item["flags"])}],
            })
        blocks.append({"type": "divider"})

    fallback_text = "\n\n".join(item["standup"] for item in standups)

    try:
        _client.chat_postMessage(
            channel=channel_id,
            blocks=blocks,
            text=fallback_text,
        )
        print(f"Posted standup to #{channel_id}")
    except SlackApiError as e:
        print(f"Slack error: {e.response['error']}")
