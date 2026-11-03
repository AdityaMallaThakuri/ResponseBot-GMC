"""Post one sample reply card to a Slack channel.

Usage: python scripts/post_test_card.py <channel name or id>   e.g. reply-bot-test

Needs SLACK_BOT_TOKEN in .env. The card is sample data, not a real lead. The buttons only
acknowledge the click, so the app must be running and reachable at /slack/events for them to work.
"""

import sys

from dotenv import dotenv_values
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from app.slack.card import ThreadSummary, post_test_card

SAMPLE = ThreadSummary(
    thread_ref="sample-thread-0001",
    lead_name="Sample Lead",
    company="Example Advisory",
    campaign="GMC - sample campaign",
    reply_excerpt="This is a sample reply. Sounds interesting - can you send over some times?",
    label="meeting_request",
)

HINTS = {
    "channel_not_found": "check the channel name or id; private channels need the id",
    "not_in_channel": "invite the bot: /invite @<bot name> (needed for private channels)",
    "missing_scope": "add the scope shown by Slack, then reinstall the app",
    "invalid_auth": "SLACK_BOT_TOKEN is wrong or was revoked",
    "not_authed": "SLACK_BOT_TOKEN is missing or empty",
}


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    token = dotenv_values(".env").get("SLACK_BOT_TOKEN")
    if not token:
        print("SLACK_BOT_TOKEN missing from .env")
        return 2
    channel = sys.argv[1]
    try:
        ts = post_test_card(channel, SAMPLE, client=WebClient(token=token))
    except SlackApiError as exc:
        error = exc.response.get("error", "unknown_error")
        print(f"Slack refused the post: {error}. {HINTS.get(error, '')}".strip())
        return 1
    print(f"posted sample card to {channel} (ts {ts})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
