"""Automatic campaign discovery: the rule, the cache, the hourly job (app/smartlead/watch.py)."""

from types import SimpleNamespace
from typing import Any

import pytest

from app.config.settings import parse_campaign_ids
from app.smartlead.errors import SmartleadError
from app.smartlead.watch import (
    AttachJob,
    CampaignWatcher,
    cascade_ignore,
    is_ours,
    watched_campaigns,
    webhook_url,
)

BASE = "https://bot.example.app"
ANY_URL = webhook_url(BASE, "s" * 40)

CAMPAIGNS: list[dict[str, Any]] = [
    {"id": 1, "name": "GMC - Lenders", "status": "ACTIVE"},
    {"id": 2, "name": "Lenders POS REPLY", "parent_campaign_id": 1},
    {"id": 3, "name": "gmc - banks"},  # lower case still counts
    {"id": 4, "name": "Acme - GMC style", "client_id": None},  # not GMC at the start
    {"id": 5, "name": "GMC - Acme", "client_id": 77},  # a client campaign
    {"id": 6, "name": "Acme POS REPLY", "parent_campaign_id": 5},  # its child
    {"id": 7, "name": "GMC - sub", "parent_campaign_id": 1, "client_id": 9},
    {"id": 8, "name": "Child of child", "parent_campaign_id": 2},
    {"id": 9, "name": "Unrelated"},
    {"id": 10, "name": "Orphan POS REPLY", "parent_campaign_id": 999},
]


def ids(watched: list[Any]) -> list[int]:
    return sorted(w.id for w in watched)


def settings(**over: Any) -> SimpleNamespace:
    base = {
        "GMC_CAMPAIGN_IDS": "",
        "IGNORE_CAMPAIGN_IDS": "",
        "PUBLIC_BASE_URL": BASE,
        "AUTO_ATTACH_WEBHOOKS": False,
        "WEBHOOK_SECRET": "s" * 40,
    }
    base.update(over)
    return SimpleNamespace(
        **base,
        gmc_campaign_ids=parse_campaign_ids(base["GMC_CAMPAIGN_IDS"]),
        ignore_campaign_ids=parse_campaign_ids(base["IGNORE_CAMPAIGN_IDS"]),
    )


# ------------------------------------------------------------------ the rule


def test_gmc_name_without_client_id_and_children_by_parent_id() -> None:
    assert ids(watched_campaigns(CAMPAIGNS)) == [1, 2, 3, 8]


def test_a_client_campaign_and_its_pos_reply_child_are_skipped() -> None:
    watched = ids(watched_campaigns(CAMPAIGNS))
    assert 5 not in watched and 6 not in watched  # client campaign, and its child
    assert 7 not in watched  # a child that has a client_id itself


def test_a_child_is_found_by_parent_id_not_by_name() -> None:
    by_id = {w.id: w for w in watched_campaigns(CAMPAIGNS)}
    assert by_id[2].parent_name == "GMC - Lenders" and 10 not in by_id  # POS REPLY, wrong parent
    assert by_id[8].parent_name == "Lenders POS REPLY"


def test_ignored_ids_are_removed() -> None:
    assert ids(watched_campaigns(CAMPAIGNS, ignore=frozenset({3, 2}))) == [1]  # 8 follows 2


def test_ignoring_a_campaign_also_ignores_its_children_and_their_children() -> None:
    assert ids(watched_campaigns(CAMPAIGNS, ignore=frozenset({1}))) == [3]  # 2, 8 follow 1
    assert ids(watched_campaigns(CAMPAIGNS, ignore=frozenset({2}))) == [1, 3]  # 8 follows 2
    assert ids(watched_campaigns(CAMPAIGNS, ignore=frozenset({8}))) == [1, 2, 3]  # a leaf


def test_the_cascade_reaches_children_whose_name_is_not_gmc() -> None:
    assert cascade_ignore(CAMPAIGNS, frozenset({1})) == {1, 2, 7, 8}  # 7 hangs off 1 too


def test_the_cascade_applies_to_the_override_and_the_watcher() -> None:
    assert ids(watched_campaigns(CAMPAIGNS, frozenset({1, 2, 3}), frozenset({1}))) == [3]
    sl = FakeSmartlead()
    w = CampaignWatcher(sl, lambda: settings(IGNORE_CAMPAIGN_IDS="1"), clock=Clock())
    assert not w.is_watched(1) and not w.is_watched(2) and not w.is_watched(8) and w.is_watched(3)


def test_the_override_watches_only_those_ids() -> None:
    out = watched_campaigns(CAMPAIGNS, override=frozenset({9, 1}))
    assert ids(out) == [1, 9]  # the rule is not applied: 9 is not a GMC campaign
    assert ids(watched_campaigns(CAMPAIGNS, frozenset({9, 1}), frozenset({9}))) == [1]


# ------------------------------------------------------------------ the cache and the webhook check


class FakeSmartlead:
    def __init__(self, campaigns: list[dict[str, Any]] | None = None) -> None:
        self.campaigns = list(CAMPAIGNS if campaigns is None else campaigns)
        self.hooks: dict[int, list[dict[str, Any]]] = {}
        self.list_calls = 0
        self.created: list[tuple[int, str]] = []
        self.fail_list = False
        self.fail_create: set[int] = set()

    def list_campaigns(self) -> list[dict[str, Any]]:
        self.list_calls += 1
        if self.fail_list:
            raise SmartleadError("down")
        return list(self.campaigns)

    def list_campaign_webhooks(self, campaign_id: int) -> list[dict[str, Any]]:
        return self.hooks.get(campaign_id, [])

    def create_campaign_webhook(self, campaign_id: int, name: str, url: str) -> None:
        if campaign_id in self.fail_create:
            raise SmartleadError("nope")
        self.created.append((campaign_id, url))


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def make_watcher(sl: FakeSmartlead, s: SimpleNamespace, clock: Clock) -> CampaignWatcher:
    return CampaignWatcher(sl, lambda: s, clock=clock)


def test_the_campaign_list_is_cached_for_ten_minutes() -> None:
    sl, clock = FakeSmartlead(), Clock()
    w = make_watcher(sl, settings(), clock)
    assert w.is_watched(1) and w.is_watched(2) and not w.is_watched(9)
    assert sl.list_calls == 1
    clock.now = 599
    w.is_watched(1)
    assert sl.list_calls == 1
    clock.now = 601
    w.is_watched(1)
    assert sl.list_calls == 2


def test_an_unknown_campaign_id_refreshes_the_list_once() -> None:
    sl, clock = FakeSmartlead(), Clock()
    w = make_watcher(sl, settings(), clock)
    assert not w.is_watched(500)  # still unknown after the refresh: ignored
    assert sl.list_calls == 1  # a fresh fetch already happened in this call, no second one
    sl.campaigns.append({"id": 600, "name": "GMC - New"})
    assert w.is_watched(600)  # unknown to the cache: refreshed once, then found
    assert sl.list_calls == 2


def test_a_known_but_unwatched_campaign_does_not_refresh() -> None:
    sl, clock = FakeSmartlead(), Clock()
    w = make_watcher(sl, settings(), clock)
    w.is_watched(1)
    assert not w.is_watched(9) and not w.is_watched(5)
    assert sl.list_calls == 1


def test_the_override_needs_no_smartlead_call() -> None:
    sl = FakeSmartlead()
    w = make_watcher(sl, settings(GMC_CAMPAIGN_IDS="9", IGNORE_CAMPAIGN_IDS="4"), Clock())
    assert w.is_watched(9) and not w.is_watched(1)
    assert sl.list_calls == 0


def test_a_failed_list_raises_so_the_webhook_can_answer_503() -> None:
    sl = FakeSmartlead()
    sl.fail_list = True
    with pytest.raises(SmartleadError):
        make_watcher(sl, settings(), Clock()).is_watched(1)


# ------------------------------------------------------------------ the job


class Store:
    """The announced_campaigns table, in memory."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[int, str]] = {}

    def announced(self, kind: str) -> set[int]:
        return set(self.rows.get(kind, {}))

    def record(self, kind: str, campaign_id: int, name: str) -> None:
        self.rows.setdefault(kind, {})[campaign_id] = name


def make_job(
    sl: FakeSmartlead, s: SimpleNamespace, store: Store | None = None
) -> tuple[AttachJob, list[str], Store]:
    notes: list[str] = []
    store = store or Store()
    return AttachJob(sl, lambda: s, notes.append, store.announced, store.record), notes, store


def campaign_list(n: int) -> list[dict[str, Any]]:
    return [{"id": 1000 + i, "name": f"GMC - batch {i:02d}"} for i in range(n)]


def test_the_first_ever_run_posts_one_summary_with_the_total_only() -> None:
    sl = FakeSmartlead()
    job, notes, store = make_job(sl, settings(AUTO_ATTACH_WEBHOOKS=True))
    result = job.run_once()
    assert notes == ["Watching 4 campaigns."]  # no names, one message not four
    assert result.summary == notes[0]
    assert store.announced("watching") == {1, 2, 3, 8}


def test_a_restart_with_nothing_new_posts_nothing() -> None:
    sl, store = FakeSmartlead(), Store()
    make_job(sl, settings(AUTO_ATTACH_WEBHOOKS=True), store)[0].run_once()
    for cid in (1, 2, 3, 8):  # our webhook is now on each, as after the first run
        sl.hooks[cid] = [{"id": cid, "webhook_url": f"{BASE}/webhooks/smartlead?secret=x"}]
    job, notes, _ = make_job(sl, settings(AUTO_ATTACH_WEBHOOKS=True), store)  # a new process
    job.run_once()
    job.run_once()
    assert notes == [] and sorted(c for c, _ in sl.created) == [1, 2, 3, 8]


def test_a_new_campaign_gives_one_summary_naming_it() -> None:
    sl, store = FakeSmartlead(), Store()
    make_job(sl, settings(AUTO_ATTACH_WEBHOOKS=True), store)[0].run_once()
    for cid in (1, 2, 3, 8):
        sl.hooks[cid] = [{"id": cid, "webhook_url": f"{BASE}/webhooks/smartlead?secret=x"}]
    sl.campaigns.append({"id": 50, "name": "GMC - Fresh"})
    job, notes, _ = make_job(sl, settings(AUTO_ATTACH_WEBHOOKS=True), store)
    job.run_once()
    assert notes == ["Watching 5 campaigns. New: GMC - Fresh."]
    job.run_once()  # now announced, and our webhook is not on it only in this fake: no repost
    assert len(notes) == 1


def test_many_new_campaigns_are_one_message_with_ten_names_and_a_count() -> None:
    sl, store = FakeSmartlead(campaign_list(3)), Store()
    make_job(sl, settings(AUTO_ATTACH_WEBHOOKS=True), store)[0].run_once()  # first run
    sl.campaigns = campaign_list(28)
    for c in sl.campaigns[:3]:
        sl.hooks[c["id"]] = [{"id": 1, "webhook_url": f"{BASE}/webhooks/smartlead"}]
    job, notes, _ = make_job(sl, settings(AUTO_ATTACH_WEBHOOKS=True), store)
    job.run_once()
    assert len(notes) == 1
    names = ", ".join(f"GMC - batch {i:02d}" for i in range(3, 13))
    assert notes[0] == f"Watching 28 campaigns. New: {names}, and 15 more."


def test_with_auto_attach_off_it_reports_in_one_message_and_attaches_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO)
    sl = FakeSmartlead()
    job, notes, store = make_job(sl, settings())
    result = job.run_once()
    assert sl.created == [] and len(result.would_attach) == 4
    assert "would attach to GMC - Lenders" in caplog.text
    assert notes == ["Would watch (AUTO_ATTACH_WEBHOOKS is off): 4 campaigns."]
    job.run_once()
    assert len(notes) == 1  # not repeated
    assert store.announced("would_watch") == {1, 2, 3, 8} and store.announced("watching") == set()


def test_turning_auto_attach_on_later_does_not_flood_slack() -> None:
    sl, store = FakeSmartlead(), Store()
    make_job(sl, settings(), store)[0].run_once()  # off: "would watch"
    job, notes, _ = make_job(sl, settings(AUTO_ATTACH_WEBHOOKS=True), store)
    job.run_once()  # on: the first-ever "watching" announcement is a total only
    assert notes == ["Watching 4 campaigns."]


def test_it_attaches_where_ours_is_missing_and_leaves_other_webhooks_alone() -> None:
    sl = FakeSmartlead()
    sl.hooks[1] = [{"id": 5, "webhook_url": f"{BASE}/webhooks/smartlead?secret=old"}]  # ours
    sl.hooks[3] = [{"id": 6, "webhook_url": "https://crm.example/hook"}]  # someone else's
    job, notes, _ = make_job(sl, settings(AUTO_ATTACH_WEBHOOKS=True))
    result = job.run_once()
    assert sorted(c for c, _ in sl.created) == [2, 3, 8]  # 1 already has ours
    assert all(u == webhook_url(BASE, "s" * 40) for _, u in sl.created)
    assert len(notes) == 1 and result.failed == 0
    assert sl.hooks[3] == [{"id": 6, "webhook_url": "https://crm.example/hook"}]  # untouched


def test_a_failed_attach_is_not_announced_and_is_retried() -> None:
    sl = FakeSmartlead()
    sl.fail_create = {1}
    job, notes, store = make_job(sl, settings(AUTO_ATTACH_WEBHOOKS=True, GMC_CAMPAIGN_IDS="1"))
    assert job.run_once().failed == 1 and notes == [] and store.announced("watching") == set()
    sl.fail_create = set()
    job.run_once()
    assert notes == ["Watching 1 campaign."]


def test_a_slack_failure_is_not_recorded_so_the_next_run_posts_it() -> None:
    sl, store = FakeSmartlead(), Store()
    calls: list[str] = []

    def boom(text: str) -> None:
        calls.append(text)
        raise RuntimeError("slack down")

    job = AttachJob(
        sl, lambda: settings(AUTO_ATTACH_WEBHOOKS=True), boom, store.announced, store.record
    )
    assert job.run_once().summary == "" and store.announced("watching") == set()
    job.run_once()
    assert len(calls) == 2  # tried again


def test_the_job_attaches_only_to_the_override_ids_and_skips_ignored() -> None:
    sl = FakeSmartlead()
    job, _, _ = make_job(
        sl, settings(AUTO_ATTACH_WEBHOOKS=True, GMC_CAMPAIGN_IDS="1,3,9", IGNORE_CAMPAIGN_IDS="3")
    )
    job.run_once()
    assert sorted(c for c, _ in sl.created) == [1, 9]


def test_the_job_skips_ignored_campaigns_with_their_children_and_client_campaigns() -> None:
    sl = FakeSmartlead()
    job, _, _ = make_job(sl, settings(AUTO_ATTACH_WEBHOOKS=True, IGNORE_CAMPAIGN_IDS="1"))
    job.run_once()
    assert {c for c, _ in sl.created} == {3}  # 1, its children 2, 7, 8, and client ones are out
    sl2 = FakeSmartlead()
    make_job(sl2, settings(AUTO_ATTACH_WEBHOOKS=True))[0].run_once()
    assert not {5, 6, 7, 9, 10} & {c for c, _ in sl2.created}


def test_without_a_public_base_url_the_job_does_nothing() -> None:
    sl = FakeSmartlead()
    job, notes, _ = make_job(sl, settings(PUBLIC_BASE_URL="", AUTO_ATTACH_WEBHOOKS=True))
    assert job.run_once().skipped and sl.list_calls == 0 and sl.created == [] and notes == []


def test_is_ours_ignores_the_secret_and_other_hosts() -> None:
    assert is_ours({"webhook_url": f"{BASE}/webhooks/smartlead?secret=a"}, BASE)
    assert not is_ours({"webhook_url": "https://x.example/webhooks/smartlead"}, BASE)
    assert not is_ours({}, BASE)


def test_announced_campaigns_are_stored_once_per_kind() -> None:
    from app.db.client import fetch_announced_campaign_ids, record_announced_campaign
    from tests.fakedb import FakeDb

    db = FakeDb()
    record_announced_campaign(db, "watching", 7, "GMC - A")
    record_announced_campaign(db, "watching", 7, "GMC - A")
    record_announced_campaign(db, "would_watch", 7, "GMC - A")
    assert fetch_announced_campaign_ids(db, "watching") == {7}
    assert len(db.tables["announced_campaigns"]) == 2
