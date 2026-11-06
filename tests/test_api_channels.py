"""API surface for channels: /subscribe validation, /pairings, /health, and the
SMS webhook's carrier keywords (STOP/START/HELP) alongside Y/N replies.
"""

import pytest

from aurora.db import Observation, PendingPairing, Subscription

GUID = "0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9"
LOC = ["41.7,-111.8"]  # "lat,lon" skips geocoding (no network)


class TestSubscribe:
    def test_legacy_phone_body_defaults_to_sms(self, api):
        resp = api.post("/subscribe", json={"phone": "+1 555-123-4567", "locations": LOC})
        assert resp.status_code == 201, resp.text
        (sub,) = resp.json()
        assert sub["channel"] == "sms"
        assert sub["recipient"] == sub["phone"] == "+15551234567"

    def test_explicit_sms_recipient(self, api):
        resp = api.post("/subscribe", json={"channel": "sms", "recipient": "+447911123456", "locations": LOC})
        assert resp.status_code == 201
        assert resp.json()[0]["recipient"] == "+447911123456"

    def test_inreach_guid(self, api, memory_db):
        memory_db.add(PendingPairing(guid=GUID))
        memory_db.commit()
        resp = api.post(
            "/subscribe",
            json={"channel": "InReach", "recipient": GUID.upper().replace("-", ""), "locations": LOC},
        )
        assert resp.status_code == 201, resp.text
        (sub,) = resp.json()
        assert sub["channel"] == "inreach" and sub["recipient"] == GUID and sub["phone"] is None
        assert memory_db.query(PendingPairing).count() == 0  # pairing finished

    @pytest.mark.parametrize(
        "body",
        [
            {"phone": "5551234567"},
            {"channel": "sms", "recipient": "not a phone"},
            {"channel": "inreach", "recipient": "+15551234567"},
            {"channel": "inreach", "recipient": "1234"},
            {"channel": "inreach", "phone": "+15551234567"},
            {"channel": "email", "recipient": "a@b.com"},
            {"channel": "sms"},
        ],
    )
    def test_rejected(self, api, body):
        assert api.post("/subscribe", json={**body, "locations": LOC}).status_code == 422

    def test_list_and_unsubscribe_by_guid(self, api):
        api.post("/subscribe", json={"channel": "inreach", "recipient": GUID, "locations": LOC})
        # Any accepted GUID spelling finds it.
        assert len(api.get(f"/subscriptions/{GUID.upper()}").json()) == 1
        assert api.delete(f"/unsubscribe/{GUID.replace('-', '')}").status_code == 204
        assert api.get(f"/subscriptions/{GUID}").json() == []

    def test_list_by_phone_still_works(self, api):
        api.post("/subscribe", json={"phone": "+15551234567", "locations": LOC})
        assert len(api.get("/subscriptions/+15551234567").json()) == 1


def test_pairings_endpoint(api, memory_db):
    memory_db.add(PendingPairing(guid=GUID, sender="inReach message from James", last_message="hi"))
    memory_db.commit()
    (p,) = api.get("/pairings").json()
    assert p["guid"] == GUID and p["last_message"] == "hi"


def test_health_reports_channels(api):
    body = api.get("/health").json()
    assert body["channels"] == {"sms": True, "inreach": False}
    assert body["sms_enabled"] is True
    assert body["mailbox_enabled"] is False
    assert body["ntfy_enabled"] is False


# ── SMS webhook ───────────────────────────────────────────────────────────────

PHONE = "+15551234567"


def _sms_subs(db, n=2, active=True):
    for i in range(n):
        db.add(Subscription(channel="sms", recipient=PHONE, address=f"site{i}", lat=41.7, lon=-111.8,
                            active=active))
    db.commit()


def _inbound(api, body):
    return api.post("/sms/inbound", data={"From": PHONE, "Body": body})


class TestSmsInbound:
    @pytest.mark.parametrize("word", ["STOP", "stop", "StopAll", "UNSUBSCRIBE", "cancel", "END", "quit."])
    def test_stop_deactivates_without_deleting(self, api, memory_db, word):
        _sms_subs(memory_db)
        resp = _inbound(api, word)
        assert resp.status_code == 200
        assert resp.text == "<Response></Response>"
        subs = memory_db.query(Subscription).all()
        assert len(subs) == 2 and not any(s.active for s in subs)
        assert memory_db.query(Observation).count() == 0

    @pytest.mark.parametrize("word", ["START", "unstop"])
    def test_start_reactivates(self, api, memory_db, word):
        _sms_subs(memory_db, active=False)
        assert _inbound(api, word).text == "<Response></Response>"
        assert all(s.active for s in memory_db.query(Subscription).all())

    def test_yes_resubscribes_only_when_all_stopped(self, api, memory_db):
        _sms_subs(memory_db, active=False)
        _inbound(api, "YES")
        assert all(s.active for s in memory_db.query(Subscription).all())
        assert memory_db.query(Observation).count() == 0

    def test_yes_while_subscribed_is_a_saw_it_label(self, api, memory_db):
        _sms_subs(memory_db)
        resp = _inbound(api, "Yes")
        assert "logged that you saw it" in resp.text
        obs = memory_db.query(Observation).one()
        assert obs.saw_aurora is True and obs.source == "sms_reply"
        assert obs.phone == obs.recipient == PHONE

    def test_help(self, api, memory_db):
        resp = _inbound(api, "help")
        assert "STOP" in resp.text and "Reply Y/N" in resp.text
        assert memory_db.query(Observation).count() == 0

    def test_stop_inside_a_sentence_is_not_a_keyword(self, api, memory_db):
        _sms_subs(memory_db)
        _inbound(api, "no, please don't stop")
        assert all(s.active for s in memory_db.query(Subscription).all())
        assert memory_db.query(Observation).one().saw_aurora is False

    def test_unclassifiable(self, api):
        assert "didn't catch" in _inbound(api, "maybe").text

    def test_replies_are_gsm7(self, api, memory_db):
        from aurora.messages import GSM7_BASIC

        _sms_subs(memory_db)
        for body in ("Y", "N", "help", "maybe"):
            text = _inbound(api, body).text
            assert all(c in GSM7_BASIC for c in text), body

    def test_signature_still_enforced(self, api, monkeypatch):
        import aurora.main as main

        monkeypatch.setattr(main, "validate_twilio_signature", lambda *a: False)
        assert _inbound(api, "Y").status_code == 403
