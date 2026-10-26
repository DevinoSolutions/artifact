"""Unit tests for the retry budget on the GitHub OIDC token fetch and the MinIO
STS exchange (Store.credentials).

Standard library only: `python -m unittest discover -s tests`. urlopen, the
sleep and the clock are all replaced, so nothing touches the network and
nothing waits in real time.

The motivating failures are HTTP 503s from the GitHub OIDC endpoint in
DevinoSolutions/stealth-chrome-devtools-mcp runs 39956132580 and 40398194992,
and a DNS URLError against the STS endpoint on 2026-10-20.
"""
import contextlib
import importlib.util
import io
import os
import socket
import ssl
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "devino_artifact_main_creds", os.path.join(ROOT, "lib", "main.py")
)
main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(main)

STS_XML = (
    b"<AssumeRoleWithWebIdentityResponse><Credentials>"
    b"<AccessKeyId>AKID</AccessKeyId><SecretAccessKey>SECRET</SecretAccessKey>"
    b"<SessionToken>TOKEN</SessionToken></Credentials></AssumeRoleWithWebIdentityResponse>"
)
OIDC_OK = b'{"value": "header.payload.signature"}'
ENV = {
    "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.example.invalid/oidc?x=1",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
}


def http_error(code):
    return urllib.error.HTTPError("https://x.invalid/", code, "msg", {}, io.BytesIO(b"upstream"))


class StalledBody(object):
    """An HTTPError body whose read times out."""

    def read(self, *args):
        raise TimeoutError("timed out")

    def close(self):
        pass


class FakeResponse(object):
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeUrlopen(object):
    """Replays scripted outcomes (bytes -> response, exception -> raised); the
    last entry repeats forever."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = 0
        self.timeouts = []

    def __call__(self, req, timeout=None):
        self.timeouts.append(timeout)
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        return FakeResponse(item)


class FakeClock(object):
    """time.monotonic that only moves when sleep is called."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@contextlib.contextmanager
def harness(*script):
    urlopen = FakeUrlopen(*script)
    clock = FakeClock()
    buf = io.StringIO()
    with mock.patch.dict(os.environ, ENV, clear=True), mock.patch.object(
        main.urllib.request, "urlopen", urlopen
    ), mock.patch.object(main.time, "sleep", clock.sleep), mock.patch.object(
        main.time, "monotonic", clock.monotonic
    ), contextlib.redirect_stdout(buf):
        store = main.Store.__new__(main.Store)
        store.endpoint = "https://storage.example.invalid"
        yield store, urlopen, clock, buf


class TestOidcFetch(unittest.TestCase):
    def test_503_then_success(self):
        with harness(http_error(503), OIDC_OK, STS_XML) as (store, urlopen, clock, buf):
            creds = store.credentials()
        self.assertEqual(creds, ("AKID", "SECRET", "TOKEN"))
        self.assertEqual(urlopen.calls, 3)
        self.assertEqual(len(clock.sleeps), 1)
        text = buf.getvalue()
        self.assertIn("GitHub OIDC token: attempt 1 failed (HTTP 503)", text)
        self.assertEqual(len([ln for ln in text.splitlines() if "retrying" in ln]), 1)

    def test_503_for_longer_than_the_old_budget_still_succeeds(self):
        # The old schedule gave up after 3 attempts (about 12 s of waiting).
        outage = [http_error(503)] * 6
        with harness(*(outage + [OIDC_OK, STS_XML])) as (store, urlopen, clock, _):
            creds = store.credentials()
        self.assertEqual(creds[0], "AKID")
        self.assertEqual(urlopen.calls, 8)
        self.assertGreater(sum(clock.sleeps), 12)
        self.assertLessEqual(sum(clock.sleeps), main.CREDENTIAL_BUDGET)

    def test_503_every_time_fails_after_the_budget_without_real_sleeping(self):
        with harness(http_error(503)) as (store, urlopen, clock, buf):
            with self.assertRaises(SystemExit) as cm:
                store.credentials()
        self.assertEqual(cm.exception.code, 1)
        self.assertGreater(urlopen.calls, 4)
        self.assertLessEqual(sum(clock.sleeps), main.CREDENTIAL_BUDGET)
        # Stopped because the next wait would pass the budget, not early: the
        # longest single wait is 32 s +25%.
        self.assertGreater(sum(clock.sleeps), main.CREDENTIAL_BUDGET - 40)
        text = buf.getvalue()
        self.assertIn("::error::Could not obtain the GitHub OIDC token: HTTP 503", text)
        self.assertEqual(len([ln for ln in text.splitlines() if "retrying" in ln]), urlopen.calls - 1)

    def test_403_is_not_retried(self):
        with harness(http_error(403)) as (store, urlopen, clock, buf):
            with self.assertRaises(SystemExit):
                store.credentials()
        self.assertEqual(urlopen.calls, 1)
        self.assertEqual(clock.sleeps, [])
        self.assertNotIn("retrying", buf.getvalue())

    def test_429_is_retried(self):
        with harness(http_error(429), OIDC_OK, STS_XML) as (store, urlopen, clock, _):
            store.credentials()
        self.assertEqual(urlopen.calls, 3)

    def test_urlerror_then_success(self):
        err = urllib.error.URLError(socket.gaierror(-3, "Temporary failure in name resolution"))
        with harness(err, OIDC_OK, STS_XML) as (store, urlopen, clock, buf):
            creds = store.credentials()
        self.assertEqual(creds[0], "AKID")
        self.assertEqual(urlopen.calls, 3)
        self.assertIn("attempt 1 failed (URLError: [Errno -3] Temporary failure in name resolution)", buf.getvalue())

    def test_certificate_failure_gets_one_attempt(self):
        err = urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
        with harness(err) as (store, urlopen, clock, buf):
            with self.assertRaises(SystemExit):
                store.credentials()
        self.assertEqual(urlopen.calls, 1)
        self.assertEqual(clock.sleeps, [])
        self.assertNotIn("retrying", buf.getvalue())

    def test_value_error_gets_one_attempt(self):
        with harness(ValueError("bad url")) as (store, urlopen, clock, _):
            with self.assertRaises(SystemExit):
                store.credentials()
        self.assertEqual(urlopen.calls, 1)
        self.assertEqual(clock.sleeps, [])

    def test_timeout_and_connection_errors_raised_directly_are_retried(self):
        for err in (TimeoutError("timed out"), ConnectionResetError("reset")):
            with harness(err, OIDC_OK, STS_XML) as (store, urlopen, clock, _):
                self.assertEqual(store.credentials()[0], "AKID")
            self.assertEqual(urlopen.calls, 3)

    def test_stalled_5xx_body_is_still_retried(self):
        stalled = urllib.error.HTTPError("https://x.invalid/", 503, "msg", {}, StalledBody())
        with harness(stalled, OIDC_OK, STS_XML) as (store, urlopen, clock, buf):
            self.assertEqual(store.credentials()[0], "AKID")
        self.assertEqual(urlopen.calls, 3)
        self.assertIn("attempt 1 failed (HTTP 503)", buf.getvalue())

    def test_stalled_403_body_still_fails_at_once(self):
        stalled = urllib.error.HTTPError("https://x.invalid/", 403, "msg", {}, StalledBody())
        with harness(stalled) as (store, urlopen, clock, _):
            with self.assertRaises(SystemExit):
                store.credentials()
        self.assertEqual(urlopen.calls, 1)

    def test_attempt_timeout_shrinks_as_the_budget_runs_down(self):
        with harness(http_error(503)) as (store, urlopen, clock, _):
            with self.assertRaises(SystemExit):
                store.credentials()
        timeouts = urlopen.timeouts
        self.assertEqual(timeouts[0], 60)
        self.assertEqual(timeouts, sorted(timeouts, reverse=True))
        self.assertLess(timeouts[-1], 60)
        self.assertGreaterEqual(min(timeouts), main.HTTP_MIN_TIMEOUT)


class TestStsExchange(unittest.TestCase):
    def test_503_then_success(self):
        with harness(OIDC_OK, http_error(503), STS_XML) as (store, urlopen, clock, buf):
            creds = store.credentials()
        self.assertEqual(creds, ("AKID", "SECRET", "TOKEN"))
        self.assertEqual(urlopen.calls, 3)
        self.assertIn("MinIO STS exchange: attempt 1 failed (HTTP 503)", buf.getvalue())

    def test_503_every_time_fails_after_the_budget(self):
        with harness(OIDC_OK, http_error(503)) as (store, urlopen, clock, buf):
            with self.assertRaises(SystemExit):
                store.credentials()
        self.assertGreater(urlopen.calls, 5)
        self.assertLessEqual(sum(clock.sleeps), main.CREDENTIAL_BUDGET)
        self.assertIn("::error::MinIO STS AssumeRoleWithWebIdentity failed: HTTP 503", buf.getvalue())

    def test_403_is_not_retried(self):
        with harness(OIDC_OK, http_error(403)) as (store, urlopen, clock, _):
            with self.assertRaises(SystemExit):
                store.credentials()
        self.assertEqual(urlopen.calls, 2)
        self.assertEqual(clock.sleeps, [])

    def test_urlerror_then_success(self):
        with harness(OIDC_OK, urllib.error.URLError(socket.gaierror(-2, "dns")), STS_XML) as (store, urlopen, clock, _):
            creds = store.credentials()
        self.assertEqual(creds[0], "AKID")
        self.assertEqual(urlopen.calls, 3)

    def test_no_secret_reaches_the_log(self):
        with harness(http_error(503), OIDC_OK, http_error(503), STS_XML) as (store, _, _c, buf):
            store.credentials()
        text = buf.getvalue()
        for secret in ("request-token", "header.payload.signature"):
            self.assertNotIn(secret, text)


class TestDefaultHttpUnchanged(unittest.TestCase):
    def test_without_a_budget_http_still_makes_three_attempts(self):
        with harness(http_error(503)) as (_store, urlopen, clock, buf):
            with self.assertRaises(RuntimeError):
                main.http("https://x.invalid/")
        self.assertEqual(urlopen.calls, 3)
        self.assertEqual(clock.sleeps, [2, 4])
        self.assertNotIn("retrying", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
