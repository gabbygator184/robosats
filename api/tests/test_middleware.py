from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings

from robosats.middleware import (
    RobotTokenSHA256AuthenticationMiddleWare,
    robot_creation_allowed,
)

LOCMEM = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "robot-create-test",
    }
}

VALID_TOKEN = "A" * 40


def _patch_config(rate: str, window: str):
    values = {
        "ROBOT_CREATION_RATE": rate,
        "ROBOT_CREATION_WINDOW": window,
    }
    return mock.patch(
        "robosats.middleware.config",
        side_effect=lambda option, default=None: values.get(option, default),
    )


@override_settings(CACHES=LOCMEM)
class RobotCreationAllowedTest(TestCase):
    def setUp(self):
        cache.clear()

    def test_allows_up_to_rate_per_window(self):
        with _patch_config("2", "10"):
            self.assertTrue(robot_creation_allowed())
            self.assertTrue(robot_creation_allowed())
            self.assertFalse(robot_creation_allowed())

    def test_rate_zero_disables_limit(self):
        with _patch_config("0", "10"):
            for _ in range(5):
                self.assertTrue(robot_creation_allowed())


@override_settings(CACHES=LOCMEM)
class RobotCreationMiddlewareTest(TestCase):
    def setUp(self):
        cache.clear()

    def _middleware(self):
        return RobotTokenSHA256AuthenticationMiddleWare(
            get_response=lambda request: HttpResponse()
        )

    def test_returns_429_without_creating_robot_when_limit_exceeded(self):
        window = 10
        # Occupies the only allowed slot for the current window.
        bucket = int(self._now()) // window
        cache.add(f"limiter:robot_create:{bucket}", 1, timeout=window)

        request = RequestFactory().post(
            "/api/make/",
            HTTP_AUTHORIZATION=f"Token {VALID_TOKEN}",
            PUBLIC_KEY="pk",
            ENCRYPTED_PRIVATE_KEY="ek",
            NOSTR_PUBKEY="a" * 64,
        )

        with _patch_config("1", str(window)):
            response = self._middleware()(request)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(User.objects.count(), 0)

    @staticmethod
    def _now():
        import time

        return int(time.time())
