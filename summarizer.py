import os
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

_PROMPT_TEMPLATE = """You generate daily standup updates from developer activity logs.

Rules:
- Be specific — use exact PR titles, commit messages, repo names
- Sound human, not robotic
- Keep each section to 1-3 bullet points max
- For "Today", infer the likely continuation based on what they were doing
- For "Blockers", only flag if genuinely stuck — otherwise write "None"

Generate a standup for {name}.

Activity in the last 24 hours:
{activity}

Format your response exactly like this:
*Yesterday*
<bullet points>

*Today*
<bullet points>

*Blockers*
<bullet points or "None">"""


def generate_standup(member_name: str, events: list) -> str:
    if not events:
        return f"*{member_name}*\n• No activity recorded in the last 24 hours"

    activity_lines = "\n".join([
        f"- [{e.source}] {e.event_type}: {e.title} ({e.repo})"
        for e in events
    ])

    prompt = _PROMPT_TEMPLATE.format(name=member_name, activity=activity_lines)

    try:
        response = _client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
        )
        return f"*{member_name}*\n{response.choices[0].message.content.strip()}"
    except Exception as e:
        print(f"Groq error for {member_name}: {e}")
        return f"*{member_name}*\n• Could not generate standup (AI error)"


def detect_flags(member_name: str, events: list) -> list[str]:
    """Rule-based flag detection — no LLM cost."""
    flags = []

    if not events:
        flags.append(f"⚠️ No activity recorded for {member_name} in 24h")
        return flags

    open_prs = [e for e in events if e.event_type == "pr_opened"]
    if open_prs:
        for pr in open_prs:
            flags.append(f"⚠️ PR awaiting review: _{pr.title}_ in `{pr.repo}`")

    return flags
