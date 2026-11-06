"""A fake Garmin legacy reply site for tests.  All ids are synthetic.

Each token maps to a page for one message in one conversation; an unknown token
gets a page with no hidden inputs (how an expired token is assumed to look).
"""

from __future__ import annotations

import httpx

BASE = "http://explore.test"

CONV = {
    "ConversationId": "66666666-7777-4888-9999-aaaaaaaaaaaa",
    "MessengerAccountId": "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
    "SenderMessengerAccountId": "12121212-3434-4565-8787-909090909090",
}
TOKEN1, GUID1 = "Zx9_kQ2-ab7LmN4pR8sT1uV", "11111111-2222-4333-8444-000000000001"
TOKEN2, GUID2 = "Hb4-Tt7_qw2ErY6uI0oP3aS", "11111111-2222-4333-8444-000000000002"
OTHER_TOKEN, OTHER_GUID = "Pq3-Wn8_cd5HjK2mL6vB0xY", "11111111-2222-4333-8444-000000000009"
OTHER_CONV = {
    "ConversationId": "99999999-0000-4111-8222-333333333333",
    "MessengerAccountId": "44444444-5555-4666-8777-888888888888",
    "SenderMessengerAccountId": "abababab-cdcd-4efe-8f0f-010101010101",
}


def fake_page(flag="True", address="aurora@example.com", fields=None, init=True) -> str:
    """A txtmsg page shaped like Garmin's (hidden inputs + replyCommon.init)."""
    fields = {"Guid": GUID1, **CONV, "MessageId": "0"} if fields is None else fields
    inputs = "\n".join(
        f'<input id="{k}" name="{k}" type="hidden" value="{v}" />' for k, v in fields.items()
    )
    script = f"replyCommon.init('{address}', '', '{flag}');" if init else "console.log('hi');"
    return (
        "<!DOCTYPE html><html><head><title>Reply</title></head><body>"
        f'<form id="replyForm">{inputs}\n<textarea id="ReplyMessage" maxlength="160"></textarea></form>'
        f'<script src="/bundles/txtReplyFull.js"></script><script>$(function () {{ {script} }});</script>'
        "</body></html>"
    )


def message_page(guid: str, conv: dict | None = None, flag: str = "True", address="aurora@example.com") -> str:
    return fake_page(flag, address, {"Guid": guid, **(CONV if conv is None else conv), "MessageId": "0"})


class FakeGarmin:
    """Stand-in for explore.garmin.com that records every request."""

    def __init__(self, pages: dict[str, str] | None = None):
        self.pages = {TOKEN1: message_page(GUID1)} if pages is None else pages
        self.page_status = 200
        self.post_responses: list[httpx.Response | Exception] = []
        self.gets: list[httpx.Request] = []
        self.posts: list[httpx.Request] = []
        self.get_errors: list[Exception] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/textmessage/txtmsg":
            self.gets.append(request)
            if self.get_errors:
                raise self.get_errors.pop(0)
            page = self.pages.get(request.url.params.get("extId"), "<html>Expired</html>")
            return httpx.Response(
                self.page_status, text=page, headers={"Set-Cookie": "ASP.NET_SessionId=abc123; path=/"}
            )
        if request.method == "POST":
            self.posts.append(request)
            r = self.post_responses.pop(0) if self.post_responses else httpx.Response(200, json={"Success": True})
            if isinstance(r, Exception):
                raise r
            return r
        return httpx.Response(404, text="not found")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)
