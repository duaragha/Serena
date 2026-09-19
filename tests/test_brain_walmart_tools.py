"""Walmart, and the two ways it can quietly lie to him.

Both defects tested here were live, not imagined. The block wall was reported
as a successful page read, and a search card's price came back as "$2497" for
an item that costs $24.97. One makes her say the cart is empty when it is not;
the other misquotes what he is about to be charged.
"""

from __future__ import annotations

import time

import pytest

from core import brain_walmart_tools as W


def _text(result) -> str:
    return "\n".join(b.get("text", "") for b in (result.get("content") or []))


def _errored(result) -> bool:
    return bool(result.get("is_error") or result.get("isError"))


class TestTheWall:
    """The press-and-hold check is not a page, and not an empty result."""

    def test_the_title_walmart_actually_uses_is_recognised(self):
        # Measured 2026-09-19: title "Verify Your Identity", body "We like
        # real shoppers, not robots!". The first wordlist missed both and the
        # search reported success with zero products.
        assert W._is_blocked("https://www.walmart.ca/search?q=milk",
                             "Verify Your Identity", "") is True

    def test_the_redirect_url_alone_is_enough(self):
        """The surest signal, before any text renders."""

        assert W._is_blocked(
            "https://www.walmart.ca/blocked?url=L3NlYXJjaA==&g=b", "", "") is True

    def test_the_body_gives_it_away_when_the_title_is_empty(self):
        assert W._is_blocked(
            "https://www.walmart.ca/cart", "",
            "We like real shoppers, not robots!") is True

    def test_a_real_page_is_not_mistaken_for_a_wall(self):
        assert W._is_blocked(
            "https://www.walmart.ca/",
            "Online Shopping Canada: Everyday Low Prices at Walmart.ca!",
            "Skip to Main Content How do you want your items?") is False

    def test_the_wall_message_forbids_reporting_it_as_nothing_found(self):
        assert "NOT an empty cart" in W.BLOCKED_MESSAGE

    def test_solving_it_is_never_offered(self):
        """Pressing and holding it is defeating a bot check. That is his."""

        assert "his to do, not mine" in W.BLOCKED_MESSAGE


class TestThePrice:
    def test_cents_split_across_spans_are_not_read_as_dollars(self):
        """inner_text glues "$24" and "97" into "$2497"."""

        assert W._money("current price $2497") != "$2497"

    def test_a_punctuated_price_is_taken_exactly(self):
        assert W._money("current price $19.97, Was $23.97") == "$19.97"

    def test_a_genuinely_expensive_item_keeps_its_thousands(self):
        """Shifting a decimal in would turn this into $24.97."""

        assert W._money("current price $2,497.00") == "$2,497.00"

    def test_a_whole_dollar_price_still_counts(self):
        assert W._money("current price $9") == "$9"

    def test_nothing_is_better_than_a_guess(self):
        assert W._money("free shipping on orders") is None


class TestTheLink:
    def test_a_sponsored_result_yields_the_product_not_the_tracker(self):
        href = ("/wapcrs/track?bt=1&pos=1&rdf=1&rd=https%3A%2F%2Fwww.walmart.ca"
                "%2Fen%2Fip%2FMilk-2L%2F6V199F9YX7ID&pt=search")
        assert W._real_url(href) == "https://www.walmart.ca/en/ip/Milk-2L/6V199F9YX7ID"

    def test_an_ordinary_link_is_made_absolute(self):
        assert W._real_url("/en/ip/Milk/123") == "https://www.walmart.ca/en/ip/Milk/123"

    def test_no_link_is_not_a_url(self):
        assert W._real_url(None) is None


class TestPlacingAnOrder:
    """Money leaving his account needs two steps that cannot collapse into one."""

    def setup_method(self):
        W._order_tokens.clear()

    def test_it_refuses_without_a_token(self):
        result = W.run(W.walmart_place_order.handler({"confirm_total": "$52.10"}))
        assert _errored(result)
        assert "walmart_review_order" in _text(result)

    def test_it_refuses_a_total_that_does_not_match_the_review(self):
        """The cart changing between review and placing is the dangerous case."""

        W._order_tokens["abcd1234"] = {"total": "$52.10", "at": time.time()}
        result = W.run(W.walmart_place_order.handler(
            {"confirm_token": "abcd1234", "confirm_total": "$9.99"}))
        assert _errored(result)
        assert "$52.10" in _text(result)

    def test_a_stale_token_will_not_buy_anything(self):
        W._order_tokens["abcd1234"] = {
            "total": "$52.10", "at": time.time() - W.ORDER_TOKEN_TTL_SECONDS - 1}
        result = W.run(W.walmart_place_order.handler(
            {"confirm_token": "abcd1234", "confirm_total": "$52.10"}))
        assert _errored(result)
        assert "expired" in _text(result)
        assert "abcd1234" not in W._order_tokens


class TestWhatSheIsAllowedToDo:
    def test_every_tool_is_named_for_the_allow_list(self):
        """Under dontAsk, a tool missing from allowed_tools is denied silently."""

        assert len(W.WALMART_TOOL_NAMES) == len(W.WALMART_TOOLS)
        assert all(n.startswith("mcp__serena-walmart__") for n in W.WALMART_TOOL_NAMES)

    def test_the_brain_mounts_every_one_of_them(self):
        from core.brain_daemon import _build_agent_options

        captured = {}

        def options(**kwargs):
            captured.update(kwargs)
            return type("O", (), kwargs)

        _build_agent_options(
            options, object(), ["mcp__serena-ro__read_file"],
            walmart_tools=object(), walmart_tool_names=W.WALMART_TOOL_NAMES)
        assert "serena-walmart" in captured["mcp_servers"]
        for name in W.WALMART_TOOL_NAMES:
            assert name in captured["allowed_tools"], f"{name} would be denied"

    def test_only_placing_an_order_is_marked_irreversible(self):
        destructive = {t.name for t in W.WALMART_TOOLS
                       if getattr(t.annotations, "destructiveHint", False)}
        assert destructive == {"walmart_place_order"}

    def test_reading_the_cart_never_claims_to_change_it(self):
        for tool in (W.walmart_search, W.walmart_cart, W.walmart_orders,
                     W.walmart_product, W.walmart_review_order):
            assert tool.annotations.readOnlyHint is True, tool.name


@pytest.fixture(autouse=True)
def _never_open_a_browser(monkeypatch):
    """No test here is allowed to launch Edge or touch walmart.ca."""

    async def refuse(*_a, **_k):
        raise AssertionError("a unit test tried to open a real browser")

    monkeypatch.setattr(W, "get_context", refuse)


class TestTheBrowserDoesNotLiveForever:
    """The brain daemon is resident for weeks. Edge must not be."""

    def test_it_is_closed_once_it_has_gone_unused(self):
        import asyncio

        closed = []

        class _Ctx:
            pages = []

            async def close(self):
                closed.append(True)

        async def exercise():
            W._ctx = _Ctx()
            W._touch()
            # Pretend the last call was longer ago than the idle window.
            W._last_use -= W.IDLE_SHUTDOWN_SECONDS + 1
            await W.close_browser()

        asyncio.run(exercise())
        assert closed == [True]
        assert W._ctx is None

    def test_closing_it_forgets_the_warm_up_too(self):
        """A reopened browser has not been to the homepage; it must go again."""

        import asyncio

        class _Ctx:
            pages = []

            async def close(self):
                return None

        async def exercise():
            W._ctx, W._warmed, W._page = _Ctx(), True, object()
            await W.close_browser()

        asyncio.run(exercise())
        assert W._warmed is False
        assert W._page is None

    def test_closing_an_already_closed_browser_is_not_an_error(self):
        import asyncio

        W._ctx = None
        asyncio.run(W.close_browser())


class TestHisSession:
    """Signed in, or the account pages answer like a bot wall for no reason."""

    def test_the_right_key_is_found_by_trying_not_by_the_label(self, monkeypatch):
        """Eight keyring entries share the label "Chromium Safe Storage"."""

        rows = [("www.walmart.ca", "_auth", b"v11XX", "/", 0, 1, 1, 1)]
        monkeypatch.setattr(W, "_safe_storage_secrets",
                            lambda: [b"wrong-1", b"wrong-2", b"right"])
        monkeypatch.setattr(W, "_read_cookie_rows", lambda _like: rows)
        monkeypatch.setattr(
            W, "_decrypt",
            lambda blob, key: "session-token" if key == b"derived-right" else None)
        monkeypatch.setattr(W, "_derive_key", lambda secret: b"derived-" + secret)

        jar = W.edge_cookies()
        assert [c["name"] for c in jar] == ["_auth"]
        assert jar[0]["value"] == "session-token"

    def test_a_cookie_that_will_not_decrypt_is_dropped_not_faked(self, monkeypatch):
        monkeypatch.setattr(W, "_safe_storage_secrets", lambda: [b"k"])
        monkeypatch.setattr(W, "_derive_key", lambda secret: secret)
        monkeypatch.setattr(W, "_read_cookie_rows", lambda _like: [
            ("www.walmart.ca", "good", b"v11A", "/", 0, 1, 1, 1),
            ("www.walmart.ca", "bad", b"v11B", "/", 0, 1, 1, 1),
        ])
        monkeypatch.setattr(W, "_decrypt",
                            lambda blob, key: "v" if blob == b"v11A" else None)
        assert [c["name"] for c in W.edge_cookies()] == ["good"]

    def test_an_expired_cookie_carries_no_expiry_rather_than_a_past_one(
            self, monkeypatch):
        """Playwright rejects a jar whose expiry is already behind it."""

        past = int((time.time() - 86_400 + W.CHROMIUM_EPOCH_OFFSET) * 1_000_000)
        monkeypatch.setattr(W, "_safe_storage_secrets", lambda: [b"k"])
        monkeypatch.setattr(W, "_derive_key", lambda secret: secret)
        monkeypatch.setattr(W, "_read_cookie_rows", lambda _like: [
            ("www.walmart.ca", "stale", b"v11A", "/", past, 1, 1, 1)])
        monkeypatch.setattr(W, "_decrypt", lambda blob, key: "v")
        assert "expires" not in W.edge_cookies()[0]

    def test_no_keyring_is_an_empty_jar_not_a_crash(self, monkeypatch):
        """A locked keyring must degrade to signed-out, never take her down."""

        monkeypatch.setattr(W, "_safe_storage_secrets", lambda: [])
        monkeypatch.setattr(W, "_read_cookie_rows", lambda _like: [
            ("www.walmart.ca", "_auth", b"v11A", "/", 0, 1, 1, 1)])
        assert W.edge_cookies() == []


class TestHisReputationIsNotOursToSpend:
    """Bot-detection ids stay in his browser, where they were earned."""

    def test_his_session_is_carried_and_his_standing_is_not(self, monkeypatch):
        rows = [("www.walmart.ca", name, b"v11A", "/", 0, 1, 1, 1) for name in
                ("_auth", "CID", "customer", "_pxvid", "__pxvid", "_pxhd",
                 "ak_bmsc", "bm_sv", "_abck", "TS010110a1", "pxcts")]
        monkeypatch.setattr(W, "_safe_storage_secrets", lambda: [b"k"])
        monkeypatch.setattr(W, "_derive_key", lambda secret: secret)
        monkeypatch.setattr(W, "_read_cookie_rows", lambda _like: rows)
        monkeypatch.setattr(W, "_decrypt", lambda blob, key: "v")

        carried = {c["name"] for c in W.edge_cookies()}
        assert carried == {"_auth", "CID", "customer"}

    def test_every_detector_family_seen_on_walmart_is_covered(self):
        """PerimeterX, Akamai Bot Manager and F5, all present in his jar."""

        for name in ("_pxvid", "__pxvid", "_pxhd", "pxcts", "ak_bmsc",
                     "bm_sv", "bm_sz", "_abck", "TS0180da25"):
            assert W._is_reputation_cookie(name), name

    def test_an_ordinary_cookie_is_not_swept_up_with_them(self):
        for name in ("_auth", "CID", "customer", "_ga", "assortmentStoreId"):
            assert not W._is_reputation_cookie(name), name
