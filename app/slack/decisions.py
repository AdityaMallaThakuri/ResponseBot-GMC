"""Recording a click on a card (C2). The only effect is the card's status in the database.

Approve and Skip record the decision. Edit changes nothing yet. Nothing here calls Smartlead,
sends a reply or touches a suppression: drafting and sending come in later steps. The first click
wins: a card that is no longer 'posted' is left alone.
"""

from app.db.client import Database, find_slack_card, update_slack_card
from app.db.records import CardStatus
from app.slack.card import APPROVE, SKIP

STATUS_FOR_ACTION = {APPROVE: CardStatus.APPROVED, SKIP: CardStatus.SKIPPED}


def record_card_click(db: Database, channel: str, slack_ts: str, action_id: str) -> bool:
    """True when the click was recorded. Unknown cards (such as test cards) are ignored."""
    status = STATUS_FOR_ACTION.get(action_id)
    if status is None:
        return False
    card = find_slack_card(db, channel, slack_ts)
    if card is None or card.get("status") != CardStatus.POSTED.value:
        return False
    update_slack_card(db, card["id"], {"status": status.value})
    return True
