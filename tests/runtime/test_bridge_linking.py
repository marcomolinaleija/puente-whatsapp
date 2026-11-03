from __future__ import annotations
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from slidge_whatsapp.linking import (
    LinkingError, LinkingSession, RosterRateLimited, install, install_session,
    roster_retry_after, state,
)
from slidge_whatsapp.group import Bookmarks

PNG = b"\x89PNG\r\n\x1a\nfictional"

class LinkingOwnerTests(unittest.IsolatedAsyncioTestCase):
    def session(self):
        session = SimpleNamespace(
            whatsapp=SimpleNamespace(Disconnect=Mock(), PairPhone=Mock(return_value="ABCD-1234")),
            xmpp=SimpleNamespace(login_wrap=AsyncMock()), logged=False, is_logging_in=False,
        )
        return session, LinkingSession(session)

    async def test_start_is_idempotent_and_requires_cancel_for_failed_live_attempt(self):
        session, owner = self.session()
        pending = asyncio.Event()
        session.xmpp.login_wrap = AsyncMock(side_effect=lambda _: pending.wait())

        async def login(_):
            await pending.wait()

        session.xmpp.login_wrap.side_effect = login
        await owner.start("fixture-request")
        attempt = owner.attempt
        await owner.start("fixture-request")
        await owner.start("another-resource")
        self.assertEqual(owner.attempt, attempt)
        await asyncio.sleep(0)
        session.xmpp.login_wrap.assert_awaited_once()
        owner.change("connection_error", "fixture")
        with self.assertRaisesRegex(LinkingError, "cancel_required"):
            await owner.start("new-request")
        await owner.cancel(attempt)
        self.assertEqual(owner.phase, "canceled")
        session.whatsapp.Disconnect.assert_called_once()
        await owner.cancel(attempt)
        session.whatsapp.Disconnect.assert_called_once()

    async def test_failed_cancel_retains_attempt_and_never_logs_out(self):
        session, owner = self.session()
        owner.reserve()
        attempt = owner.attempt
        session.whatsapp.Disconnect.side_effect = RuntimeError("fixture failure")
        with self.assertRaisesRegex(LinkingError, "cancel_failed"):
            await owner.cancel(attempt)
        self.assertEqual(owner.attempt, attempt)
        self.assertEqual(owner.reason, "cancel_failed")
        self.assertFalse(hasattr(session.whatsapp, "Logout"))

    async def test_cancel_rejects_other_attempt_and_authenticated_session(self):
        session, owner = self.session()
        owner.reserve()
        with self.assertRaisesRegex(LinkingError, "stale_attempt"):
            await owner.cancel("other")
        owner.authenticated = True
        with self.assertRaisesRegex(LinkingError, "already_connected"):
            await owner.cancel(owner.attempt)
        session.whatsapp.Disconnect.assert_not_called()

    async def test_phone_waits_for_current_native_ready_and_rejects_expired_qr(self):
        session, owner = self.session()
        owner.reserve()
        task = asyncio.create_task(owner.phone(owner.attempt, "+12025550100"))
        await asyncio.sleep(0)
        session.whatsapp.PairPhone.assert_not_called()
        owner.qr_deadline = owner.clock() + 5
        owner.ready.set()
        self.assertEqual(await task, "ABCD-1234")
        session.whatsapp.PairPhone.assert_called_once_with("+12025550100")
        owner.qr_deadline = 0
        with self.assertRaisesRegex(LinkingError, "expired"):
            await owner.phone(owner.attempt, "+12025550100")

    async def test_qr_rotation_keeps_exact_expiry_and_cancel_ignores_late_qr(self):
        _, owner = self.session()
        owner.clock = lambda: 100.0
        image = SimpleNamespace(save=lambda stream, **_: stream.write(PNG))
        with patch.dict("sys.modules", {"qrcode": SimpleNamespace(make=lambda _: image)}):
            await owner.received_qr(json.dumps({"code": "fictional-one", "ttl": 60}))
            self.assertEqual(owner.qr_deadline, 160)
            await owner.received_qr(json.dumps({"code": "fictional-two", "ttl": 20}))
            self.assertEqual(owner.qr_deadline, 120)
            self.assertEqual(owner.qr_revision, 2)
            self.assertEqual(owner.qr, PNG)
            owner.change("canceled")
            await owner.received_qr(json.dumps({"code": "late", "ttl": 60}))
            self.assertEqual(owner.qr, b"")
            self.assertEqual(owner.phase, "canceled")

    async def test_phone_retry_reuses_code_without_changing_phone(self):
        session, owner = self.session()
        owner.reserve()
        owner.ready.set()
        owner.qr_deadline = owner.clock() + 60
        one = await owner.phone(owner.attempt, "+12025550100")
        two = await owner.phone(owner.attempt, "+12025550100")
        self.assertEqual(one, two)
        session.whatsapp.PairPhone.assert_called_once()
        with self.assertRaisesRegex(LinkingError, "cancel_required"):
            await owner.phone(owner.attempt, "+12025550101")

    async def test_cancel_is_not_blocked_by_phone_readiness_wait(self):
        _, owner = self.session()
        owner.reserve()
        pending = asyncio.create_task(owner.phone(owner.attempt, "+12025550100"))
        await asyncio.sleep(0)
        await asyncio.wait_for(owner.cancel(owner.attempt), 1)
        self.assertEqual(owner.phase, "canceled")
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)

    async def test_sync_failure_does_not_revoke_authenticated_transport(self):
        _, owner = self.session()
        owner.authenticated = True
        owner.change("connected")
        owner.sync_error = True
        snapshot = owner.snapshot()
        self.assertEqual(snapshot["authenticated"], "1")
        self.assertEqual(snapshot["sync"], "error")

    async def test_authentication_wait_has_a_bounded_deadline(self):
        _, owner = self.session()
        owner.reserve()
        owner.clock = lambda: owner.changed_at + 191
        self.assertEqual(owner.snapshot()["phase"], "connection_error")
        self.assertEqual(owner.reason, "connection_timeout")

    async def test_pair_success_is_not_authentication_and_hooks_are_preserved(self):
        calls = []

        class Session:
            async def login(self):
                return "connected"

            async def on_wa_qr(self, value):
                calls.append("qr")

            async def on_wa_pair(self, value):
                calls.append("persist-device")

            async def on_wa_connect(self, value):
                calls.append("existing-contact-voice-hooks")

            async def logout(self):
                calls.append("existing-logout")

            async def on_wa_logged_out(self, value):
                calls.append("existing-unlinked")

        install_session(Session)
        session = Session()
        await session.on_wa_pair("fictional-device")
        self.assertEqual(state(session).phase, "paired")
        self.assertFalse(state(session).authenticated)
        self.assertEqual(calls, ["persist-device"])
        await session.on_wa_connect(SimpleNamespace(Error="", JID="fixture@example.invalid"))
        self.assertTrue(state(session).authenticated)
        self.assertIn("existing-contact-voice-hooks", calls)
        await session.on_wa_connect(SimpleNamespace(Error="can-link:disconnected", JID=""))
        self.assertFalse(state(session).authenticated)
        self.assertEqual(state(session).phase, "connecting")
        await session.on_wa_logged_out(SimpleNamespace())
        self.assertEqual(state(session).phase, "logged_out")
        self.assertEqual(calls[-1], "existing-unlinked")
        pending = state(session).task = asyncio.create_task(asyncio.Event().wait())
        await session.logout()
        self.assertTrue(pending.cancelled())
        self.assertEqual(calls[-1], "existing-logout")

class LinkingSyncRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def gateway(self, *, authenticated=True, failures=1, contacts_failed=False):
        class Session:
            login = on_wa_qr = on_wa_pair = on_wa_connect = logout = on_wa_logged_out = AsyncMock()

        class Gateway(SimpleNamespace):
            def __getitem__(self, _):
                return SimpleNamespace(add_feature=Mock())

        loop = asyncio.get_running_loop()
        contacts = SimpleNamespace(ready=loop.create_future(), _fill=AsyncMock())
        groups = SimpleNamespace(ready=loop.create_future(), fill=AsyncMock())
        store = SimpleNamespace(session=Mock(return_value=Mock(__enter__=Mock(),
                                                             __exit__=Mock(return_value=False))))
        gateway = Gateway(default_ns="jabber:component:accept", GROUPS=True, store=store,
                          register_handler=Mock())
        session = SimpleNamespace(xmpp=gateway, contacts=contacts, bookmarks=groups,
                                  logged=True, is_logging_in=True, send_gateway_status=Mock())
        owner = state(session)
        owner.authenticated = authenticated
        owner.change("connected" if authenticated else "connecting")
        fill = contacts._fill if contacts_failed else groups.fill
        fill.side_effect = [RuntimeError("fixture groups timeout")] * failures + [None]
        if not contacts_failed:
            contacts.ready.set_result(True)

        async def original(_):
            await fill()
            return "fixture original result"

        gateway.login_wrap = AsyncMock(side_effect=original)
        original_login = gateway.login_wrap
        with patch.dict("sys.modules", {"tools.session": SimpleNamespace(Session=Session)}):
            install(gateway)
        return gateway, session, owner, original_login

    async def test_post_auth_sync_failure_does_not_escape_and_recovers_without_login(self):
        for contacts_failed in (False, True):
            with self.subTest(contacts_failed=contacts_failed):
                gateway, session, owner, original = self.gateway(contacts_failed=contacts_failed)
                sleeps = []

                async def sleep(delay, sleeps=sleeps):
                    sleeps.append(delay)

                with patch("slidge_whatsapp.linking.asyncio.sleep", side_effect=sleep):
                    await gateway.login_wrap(session)
                    self.assertTrue(owner.authenticated)
                    self.assertEqual(owner.snapshot()["sync"], "error")
                    self.assertFalse(session.is_logging_in)
                    await owner.sync_task
                self.assertEqual(sleeps, [30])
                self.assertEqual(owner.snapshot()["sync"], "ready")
                self.assertTrue(session.logged)
                session.send_gateway_status.assert_called_once_with("Logged in", show="chat")
                original.assert_awaited_once()

    async def test_failed_auth_still_raises_without_retry(self):
        gateway, session, owner, _ = self.gateway(authenticated=False)
        with self.assertRaises(RuntimeError):
            await gateway.login_wrap(session)
        self.assertFalse(owner.authenticated)
        self.assertIsNone(getattr(owner, "sync_task", None))
        self.assertFalse(session.is_logging_in)

    async def test_rate_limit_recovers_after_old_budget_with_capped_slow_retries(self):
        gateway, session, owner, original = self.gateway()
        session.bookmarks.fill.side_effect = [
            RuntimeError("failed getting groups: info query returned status 429: rate-overlimit")
        ] * 6 + [None]
        sleep = AsyncMock()
        with patch("slidge_whatsapp.linking.asyncio.sleep", sleep):
            await gateway.login_wrap(session)
            self.assertEqual(owner.snapshot()["sync"], "error")
            await owner.sync_task
        self.assertEqual([c.args[0] for c in sleep.await_args_list],
                         [300, 600, 1200, 1800, 1800, 1800])
        self.assertEqual(owner.snapshot()["sync"], "ready")
        session.contacts._fill.assert_not_awaited()
        self.assertEqual(session.bookmarks.fill.await_count, 7)
        original.assert_awaited_once()

    async def test_rate_limit_preserves_cooldown_and_cancels_on_disconnect(self):
        gateway, session, owner, original = self.gateway()
        session.bookmarks.fill.side_effect = RosterRateLimited(900)
        entered = asyncio.Event()

        async def sleep(delay):
            self.assertEqual(delay, 900)
            entered.set()
            await asyncio.Event().wait()

        with patch("slidge_whatsapp.linking.asyncio.sleep", side_effect=sleep):
            await gateway.login_wrap(session)
            task = owner.sync_task
            owner.retry_sync()
            self.assertIs(owner.sync_task, task)
            await entered.wait()
            self.assertEqual(owner.snapshot()["sync"], "error")
            owner.authenticated = False
            owner.change("connecting", "disconnected")
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        self.assertFalse(session.bookmarks.ready.done())
        session.bookmarks.fill.assert_awaited_once()
        original.assert_awaited_once()

    async def test_real_group_fill_recovers_readiness_after_repeated_native_429(self):
        gateway, session, owner, original = self.gateway()
        session.whatsapp = SimpleNamespace(GetGroups=Mock(side_effect=[RuntimeError(
            "failed getting groups: info query returned status 429: rate-overlimit"
        )] * 4 + [["fictional-group"]]))
        session.bookmarks.session = session
        session.bookmarks.add_whatsapp_group = AsyncMock()

        async def fill():
            await Bookmarks.fill(session.bookmarks)

        session.bookmarks.fill.side_effect = fill
        delays = []

        async def sleep(delay):
            self.assertFalse(session.bookmarks.ready.done())
            self.assertEqual(owner.snapshot()["sync"], "error")
            delays.append(delay)
            # Advance only the private cooldown, never the event loop clock.
            session.bookmarks._can_group_retry_at = 0

        with patch("slidge_whatsapp.linking.asyncio.sleep", side_effect=sleep):
            await gateway.login_wrap(session)
            await owner.sync_task
        self.assertEqual(delays, [300, 600, 1200, 1800])
        self.assertEqual(owner.snapshot()["sync"], "ready")
        self.assertEqual(session.whatsapp.GetGroups.call_count, 5)
        session.bookmarks.add_whatsapp_group.assert_awaited_once_with("fictional-group")
        original.assert_awaited_once()

    async def test_only_exact_rate_limit_gets_slow_recovery(self):
        for message in ("status 429: other", "rate-overlimit", "fixture timeout 429"):
            self.assertEqual(roster_retry_after(RuntimeError(message)), 0)

    async def test_snapshot_does_not_acknowledge_failed_or_cancelled_futures(self):
        for failure in ("cancel", "exception", "false"):
            with self.subTest(failure=failure):
                _, session, owner, _ = self.gateway()
                if failure == "cancel":
                    session.bookmarks.ready.cancel()
                elif failure == "exception":
                    session.bookmarks.ready.set_exception(RuntimeError("fixture"))
                else:
                    session.bookmarks.ready.set_result(False)
                self.assertNotEqual(owner.snapshot()["sync"], "ready")

    async def test_retries_are_bounded_and_exhaustion_stays_in_error(self):
        gateway, session, owner, original = self.gateway(failures=4)
        sleep = AsyncMock()
        with patch("slidge_whatsapp.linking.asyncio.sleep", sleep):
            await gateway.login_wrap(session)
            await owner.sync_task
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [30, 60, 120])
        self.assertEqual(session.bookmarks.fill.await_count, 4)
        self.assertEqual(owner.snapshot()["sync"], "error")
        self.assertTrue(owner.authenticated)
        self.assertFalse(session.bookmarks.ready.done())
        original.assert_awaited_once()

    async def test_logout_disconnect_and_new_attempt_cancel_retry_without_relogging(self):
        for action in ("logged_out", "connecting", "new_attempt"):
            with self.subTest(action=action):
                gateway, session, owner, original = self.gateway(failures=4)
                gate = asyncio.Event()
                entered = asyncio.Event()

                async def sleep(_, entered=entered, gate=gate):
                    entered.set()
                    await gate.wait()

                with patch("slidge_whatsapp.linking.asyncio.sleep", side_effect=sleep):
                    await gateway.login_wrap(session)
                    task = owner.sync_task
                    await entered.wait()
                    if action == "new_attempt":
                        owner.reserve()
                    else:
                        owner.authenticated = False
                        owner.change(action)
                    await asyncio.gather(task, return_exceptions=True)
                self.assertTrue(task.cancelled())
                self.assertEqual(session.bookmarks.fill.await_count, 1)
                original.assert_awaited_once()

    async def test_duplicate_failure_shares_one_retry_and_stale_result_cannot_mark_ready(self):
        gateway, session, owner, _ = self.gateway(failures=4)
        gate = asyncio.Event()
        with patch("slidge_whatsapp.linking.asyncio.sleep", side_effect=lambda _: gate.wait()):
            await gateway.login_wrap(session)
            task = owner.sync_task
            await gateway.login_wrap(session)
            self.assertIs(task, owner.sync_task)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        gateway, session, owner, _ = self.gateway()

        async def stale_result():
            owner.attempt = "replacement-attempt"

        with patch("slidge_whatsapp.linking.asyncio.sleep", new_callable=AsyncMock):
            await gateway.login_wrap(session)
            session.bookmarks.fill.side_effect = stale_result
            await owner.sync_task
        self.assertFalse(session.bookmarks.ready.done())
        self.assertTrue(owner.sync_error)

    async def test_cancelled_readiness_is_not_falsely_reported_ready(self):
        gateway, session, owner, _ = self.gateway()
        session.bookmarks.ready.cancel()
        with patch("slidge_whatsapp.linking.asyncio.sleep", new_callable=AsyncMock):
            await gateway.login_wrap(session)
            await owner.sync_task
        self.assertEqual(owner.snapshot()["sync"], "error")

    async def test_successful_login_keeps_original_result_and_has_no_retry(self):
        gateway, session, owner, original = self.gateway(failures=0)
        result = await gateway.login_wrap(session)
        self.assertEqual(result, "fixture original result")
        self.assertIsNone(owner.sync_task)
        self.assertFalse(owner.sync_error)
        original.assert_awaited_once()

class GroupListRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def bookmarks(self):
        session = SimpleNamespace(whatsapp=SimpleNamespace(GetGroups=Mock(return_value=[])))
        owner = state(session)
        owner.authenticated = True
        owner.attempt = "fictional-attempt"
        return SimpleNamespace(session=session, add_whatsapp_group=AsyncMock())

    async def test_duplicate_fill_and_cancelled_waiter_share_native_work(self):
        groups = self.bookmarks()
        entered, release = asyncio.Event(), asyncio.Event()

        async def native(_):
            entered.set()
            await release.wait()
            return ["fictional-group"]

        with patch("slidge_whatsapp.group.asyncio.to_thread", side_effect=native) as worker:
            first = asyncio.create_task(Bookmarks.fill(groups))
            await entered.wait()
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            second = asyncio.create_task(Bookmarks.fill(groups))
            release.set()
            await second
            await Bookmarks.fill(groups)
        worker.assert_awaited_once()
        groups.add_whatsapp_group.assert_awaited_once_with("fictional-group")

    async def test_rate_limit_cooldown_no_extra_queries_and_account_isolation(self):
        groups, other = self.bookmarks(), self.bookmarks()
        groups.session.whatsapp.GetGroups.side_effect = RuntimeError(
            "failed getting groups: info query returned status 429: rate-overlimit")
        for delay in (300, 600, 1200, 1800, 1800):
            groups._can_group_retry_at = 0
            with self.assertRaises(RosterRateLimited) as caught:
                await Bookmarks.fill(groups)
            self.assertEqual(caught.exception.retry_after, delay)
            self.assertFalse(hasattr(groups, "_can_group_filled_until"))
            calls = groups.session.whatsapp.GetGroups.call_count
            with self.assertRaises(RosterRateLimited):
                await Bookmarks.fill(groups)
            self.assertEqual(groups.session.whatsapp.GetGroups.call_count, calls)
        await Bookmarks.fill(other)
        other.session.whatsapp.GetGroups.assert_called_once()
        groups.session.whatsapp.GetGroups.side_effect = None
        groups._can_group_retry_at = 0
        await Bookmarks.fill(groups)
        self.assertEqual(groups._can_group_backoff, 0)
        self.assertEqual(groups._can_group_retry_at, 0)

    async def test_success_cache_expires_and_is_not_reused_by_new_attempt(self):
        groups = self.bookmarks()
        await Bookmarks.fill(groups)
        await Bookmarks.fill(groups)
        self.assertEqual(groups.session.whatsapp.GetGroups.call_count, 1)
        groups._can_group_filled_until = 0
        await Bookmarks.fill(groups)
        self.assertEqual(groups.session.whatsapp.GetGroups.call_count, 2)
        groups.session._can_linking.attempt = "fictional-new-attempt"
        await Bookmarks.fill(groups)
        self.assertEqual(groups.session.whatsapp.GetGroups.call_count, 3)

    async def test_cancelled_attempt_cannot_apply_native_result_or_cache_success(self):
        for action in ("disconnect", "new_attempt"):
            with self.subTest(action=action):
                groups = self.bookmarks()

                async def native(_, groups=groups, action=action):
                    if action == "disconnect":
                        groups.session._can_linking.authenticated = False
                    else:
                        groups.session._can_linking.attempt = "replacement"
                    return ["fictional-group"]

                with patch("slidge_whatsapp.group.asyncio.to_thread", side_effect=native):
                    with self.assertRaisesRegex(RuntimeError, "Roster session changed"):
                        await Bookmarks.fill(groups)
                groups.add_whatsapp_group.assert_not_awaited()
                self.assertFalse(hasattr(groups, "_can_group_filled_until"))

    async def test_unrelated_failure_keeps_its_type_and_does_not_start_cooldown(self):
        groups = self.bookmarks()
        groups.session.whatsapp.GetGroups.side_effect = ValueError("fictional error")
        with self.assertRaisesRegex(ValueError, "fictional error"):
            await Bookmarks.fill(groups)
        self.assertFalse(hasattr(groups, "_can_group_retry_at"))


if __name__ == "__main__":
    unittest.main()
