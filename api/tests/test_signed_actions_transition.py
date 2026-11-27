"""
Signed actions in transition mode (REQUIRE_SIGNED_ACTIONS off).

While a coordinator still serves outdated clients, unsigned writes are
accepted, but only for robots that never sent a valid signed action here. Once
a robot has signed once its client is known to sign, so any unsigned write for
it can only be a replayed bearer token and is refused. Signatures that are
present are always verified, whatever the flag says.

These run through the real views with the test robots' PGP keys; no Lightning
node is needed (make, pause, stealth, robot settings and review do not touch
it).
"""

import time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from api.models import Currency, Order, Robot
from api.utils import get_federation_short_alias
from tests.utils.pgp import sign_action
from tests.utils.trade import maker_form_buy_with_range


def read_file(path):
    with open(path, "r") as f:
        return f.read()


def auth(index):
    return {
        "HTTP_AUTHORIZATION": (
            f"Token {read_file(f'tests/robots/{index}/b91_token')}"
            f" | Public {read_file(f'tests/robots/{index}/pub_key')}"
            f" | Private {read_file(f'tests/robots/{index}/enc_priv_key')}"
            f" | Nostr {read_file(f'tests/robots/{index}/nostr_pubkey')}"
        )
    }


def timed(action, index, alias=None, ts=None):
    alias = get_federation_short_alias() if alias is None else alias
    ts = int(time.time()) if ts is None else ts
    return sign_action(f"{action}:{alias}:{ts}", index)


def order_sig(action, order_id, index, alias=None):
    alias = get_federation_short_alias() if alias is None else alias
    return sign_action(f"{action}:{order_id}:{alias}", index)


class SignedActionsBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        Currency.objects.create(
            id=1, currency=1, exchange_rate=Decimal(60000), timestamp="2026-01-01"
        )

    def setUp(self):
        # Register robots 1 and 2 (first authenticated request creates them).
        for index in (1, 2):
            self.client.get(reverse("robot"), **auth(index))

    def user(self, index):
        return User.objects.get(username=read_file(f"tests/robots/{index}/nickname"))

    def signs(self, index):
        return Robot.objects.get(user=self.user(index)).signs_actions_since is not None

    def make(self, index=1, signature=None):
        body = dict(maker_form_buy_with_range)
        if signature is not None:
            body["signature"] = signature
        return self.client.post(reverse("make"), body, **auth(index))

    def stealth(self, index=1, signature=None, wants=True):
        body = {"wantsStealth": wants}
        if signature is not None:
            body["signature"] = signature
        return self.client.post(
            reverse("stealth"), body, content_type="application/json", **auth(index)
        )

    def pause(self, order_id, index=1, signature=None):
        body = {"action": "pause"}
        if signature is not None:
            body["signature"] = signature
        return self.client.post(
            reverse("order") + f"?order_id={order_id}", body, **auth(index)
        )

    def publish(self, order_id):
        Order.objects.filter(id=order_id).update(status=Order.Status.PUB)


@patch("api.utils.REQUIRE_SIGNED_ACTIONS", False)
class TransitionModeTest(SignedActionsBase):
    def test_old_client_keeps_working_until_it_signs(self):
        # Outdated client, never signed: served.
        response = self.make()
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.stealth().status_code, 200)
        self.assertFalse(self.signs(1))

    def test_first_valid_signature_makes_unsigned_writes_fail(self):
        response = self.stealth(signature=timed("stealth", 1))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(self.signs(1))

        # From now on the bearer alone is not enough, for every write.
        response = self.stealth()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], 1059)

        response = self.make()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], 1059)
        self.assertEqual(Order.objects.count(), 0)

        # Signed writes still work.
        response = self.make(signature=timed("make", 1))
        self.assertEqual(response.status_code, 201, response.content)

        # The other robot never signed and is not affected.
        self.assertEqual(self.stealth(index=2).status_code, 200)

    def test_unsigned_order_action_after_signing_is_refused(self):
        response = self.make(signature=timed("make", 1))
        self.assertEqual(response.status_code, 201, response.content)
        order_id = response.json()["id"]
        self.publish(order_id)

        response = self.pause(order_id)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], 1059)
        self.assertEqual(Order.objects.get(id=order_id).status, Order.Status.PUB)

        response = self.pause(order_id, signature=order_sig("pause", order_id, 1))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Order.objects.get(id=order_id).status, Order.Status.PAU)

    def test_signed_order_action_marks_robot(self):
        response = self.make()
        self.assertEqual(response.status_code, 201)
        order_id = response.json()["id"]
        self.publish(order_id)

        response = self.pause(order_id, signature=order_sig("pause", order_id, 1))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(self.signs(1))
        self.publish(order_id)
        self.assertEqual(self.pause(order_id).json()["error_code"], 1059)

    def test_forged_signature_is_refused_and_does_not_mark(self):
        # Garbage, or a signature by another robot's key: refused even though
        # unsigned requests are still accepted for this robot.
        for signature in ("not a signature", timed("stealth", 2)):
            response = self.stealth(signature=signature)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error_code"], 1048)
        self.assertFalse(self.signs(1))

    def test_signature_for_another_coordinator_is_refused(self):
        response = self.stealth(signature=timed("stealth", 1, alias="lake"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], 1061)
        self.assertFalse(self.signs(1))

    def test_stale_signature_is_refused(self):
        response = self.make(signature=timed("make", 1, ts=int(time.time()) - 3600))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], 1060)

    def test_signature_replayed_on_another_order_or_action(self):
        # One active order per robot: robot 1 makes the first, robot 2 the second.
        first = self.make(1, signature=timed("make", 1)).json()["id"]
        second = self.make(2, signature=timed("make", 2)).json()["id"]
        self.publish(first)
        self.publish(second)

        # A valid signature by robot 2 for another order: refused on its own order.
        response = self.pause(second, 2, signature=order_sig("pause", first, 2))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], 1048)
        # Another action's signature on this order: refused.
        response = self.pause(first, signature=order_sig("cancel", first, 1))
        self.assertEqual(response.json()["error_code"], 1048)
        # Signed for the same order at another coordinator: refused.
        response = self.pause(first, signature=order_sig("pause", first, 1, "lake"))
        self.assertEqual(response.json()["error_code"], 1048)
        # Robot 1's signature used with robot 2's bearer: refused.
        response = self.pause(second, 2, signature=order_sig("pause", second, 1))
        self.assertEqual(response.json()["error_code"], 1048)
        self.assertEqual(Order.objects.get(id=first).status, Order.Status.PUB)
        self.assertEqual(Order.objects.get(id=second).status, Order.Status.PUB)

    def test_robot_settings_and_review(self):
        response = self.client.put(
            reverse("robot"),
            {"webhook_enabled": False, "signature": timed("robot", 1)},
            content_type="application/json",
            **auth(1),
        )
        self.assertNotEqual(response.status_code, 500)
        self.assertTrue(self.signs(1))
        response = self.client.put(
            reverse("robot"),
            {"webhook_enabled": False},
            content_type="application/json",
            **auth(1),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], 1059)

        pubkey = read_file("tests/robots/1/nostr_pubkey")
        response = self.client.post(reverse("review"), {"pubkey": pubkey}, **auth(1))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], 1059)

    def test_old_client_plain_statement_in_dispute(self):
        order_id = self.make(1).json()["id"]
        Order.objects.filter(id=order_id).update(
            status=Order.Status.DIS, taker=self.user(2)
        )
        statement = "The buyer never sent the fiat. " * 5
        # The order built here has no bonds, so the GET that answers the action
        # fails afterwards; what matters is that the statement was stored.
        self.client.raise_request_exception = False
        self.client.post(
            reverse("order") + f"?order_id={order_id}",
            {"action": "submit_statement", "statement": statement},
            **auth(1),
        )
        self.assertEqual(
            Order.objects.get(id=order_id).maker_statement, statement.strip()
        )

    def test_signed_statement_must_verify(self):
        order_id = self.make(1).json()["id"]
        Order.objects.filter(id=order_id).update(
            status=Order.Status.DIS, taker=self.user(2)
        )
        statement = "The buyer never sent the fiat. " * 5
        body = {
            "action": "submit_statement",
            "statement": statement,
            "signature": order_sig("submit_statement", order_id, 1),
        }
        url = reverse("order") + f"?order_id={order_id}"
        # A signing client must also sign the statement itself.
        response = self.client.post(url, body, **auth(1))
        self.assertEqual(response.json()["error_code"], 1048)
        self.assertIsNone(Order.objects.get(id=order_id).maker_statement)
        body["statement"] = sign_action(statement, 1)
        self.client.raise_request_exception = False
        self.client.post(url, body, **auth(1))
        self.assertEqual(
            Order.objects.get(id=order_id).maker_statement.strip(), statement.strip()
        )


@patch("api.utils.REQUIRE_SIGNED_ACTIONS", True)
class EnforcedModeTest(SignedActionsBase):
    def test_unsigned_refused_even_if_never_signed(self):
        response = self.make()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], 1059)
        self.assertEqual(self.stealth().json()["error_code"], 1059)

    def test_signed_accepted(self):
        response = self.make(signature=timed("make", 1))
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.stealth(signature=timed("stealth", 1)).status_code, 200)
