"""Unit tests for `mc` error classification and the transport retry.

Standard library only, like lib/main.py itself: `python -m unittest discover -s tests`.
Every test mocks `subprocess.run`, so nothing here touches the network, the
storage endpoint, `mc`, or the OIDC flow.

The transport sample is the real failure from
DevinoSolutions/stealth-chrome-devtools-mcp run 34640838095, job 103404145715,
which a passing upload reported as "Artifact not found".
"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "devino_artifact_main", os.path.join(ROOT, "lib", "main.py")
)
main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(main)


RESET = (
    "mc: <ERROR> Unable to prepare URL for copying. Get "
    '"https://storage.devino.ca/gh-artifacts/?location=": read tcp '
    "10.1.0.14:44618->172.67.206.3:443: read: connection reset by peer"
)
MISSING = (
    "mc: <ERROR> Unable to validate source "
    "`devino/gh-artifacts/o/r/1/missing.tgz`. Object does not exist."
)
DENIED = "mc: <ERROR> Unable to copy. Access Denied."


class FakeCompleted(object):
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout


class FakeRun(object):
    """Replays a list of (returncode, output); repeats the last entry forever."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, cmd, env=None, stdout=None, stderr=None, **kwargs):
        self.calls.append(list(cmd))
        rc, out = self.results[min(len(self.calls) - 1, len(self.results) - 1)]
        return FakeCompleted(rc, out.encode("utf-8"))


def make_store():
    """A Store with no __init__: no mc download, no OIDC exchange, no network."""
    s = main.Store.__new__(main.Store)
    s.mc = "mc"
    s.cfg = os.path.join(tempfile.gettempdir(), "devino-test-cfg")
    s.env = {}
    s.bucket = "gh-artifacts"
    s.endpoint = "https://storage.example.invalid"
    return s


@contextlib.contextmanager
def harness(*results):
    """Patch subprocess.run and time.sleep; yield (store, runner, sleeps, out)."""
    runner = FakeRun(*results)
    sleeps = []
    buf = io.StringIO()
    with mock.patch.object(main.subprocess, "run", runner), mock.patch.object(
        main.time, "sleep", sleeps.append
    ), contextlib.redirect_stdout(buf):
        yield make_store(), runner, sleeps, buf


class TestClassify(unittest.TestCase):
    def test_success_is_ok(self):
        self.assertEqual(main.classify_mc_error(0, ""), "ok")
        self.assertEqual(main.classify_mc_error(0, MISSING), "ok")

    def test_the_observed_reset_is_transport(self):
        self.assertEqual(main.classify_mc_error(1, RESET), "transport")

    def test_other_transport_shapes(self):
        for text in (
            "mc: <ERROR> dial tcp 1.2.3.4:443: connect: connection refused",
            "mc: <ERROR> Get https://s/: net/http: TLS handshake timeout",
            "mc: <ERROR> read tcp: i/o timeout",
            "mc: <ERROR> context deadline exceeded",
            "mc: <ERROR> Put https://s/: EOF",
            "mc: <ERROR> 503 Service Unavailable",
            "mc: <ERROR> 502 Bad Gateway",
            "mc: <ERROR> server responded with 503",
            "mc: <ERROR> status code: 504",
            "mc: <ERROR> Please reduce your request rate. SlowDown",
            "mc: <ERROR> We encountered an internal error, please try again: InternalError",
            "mc: <ERROR> write: broken pipe",
            "mc: <ERROR> connect: network is unreachable",
        ):
            self.assertEqual(main.classify_mc_error(1, text), "transport", text)

    def test_missing_object_is_not_found(self):
        for text in (
            MISSING,
            "mc: <ERROR> Unable to stat. The specified key does not exist.",
            "mc: <ERROR> NoSuchKey",
            "mc: <ERROR> The specified bucket does not exist. NoSuchBucket",
            "mc: <ERROR> 404 Not Found",
        ):
            self.assertEqual(main.classify_mc_error(1, text), "not-found", text)

    def test_credentials_are_auth_not_transport(self):
        for text in (
            DENIED,
            "mc: <ERROR> InvalidAccessKeyId",
            "mc: <ERROR> SignatureDoesNotMatch",
            "mc: <ERROR> ExpiredToken: the security token has expired",
            "mc: <ERROR> 403 Forbidden",
        ):
            self.assertEqual(main.classify_mc_error(1, text), "auth", text)

    def test_dns_failure_is_transport_not_not_found(self):
        # "no such host" contains no 404 wording, but a naive not-found rule
        # that fired on "no such" would misread it. Ordering guard.
        text = "mc: <ERROR> dial tcp: lookup storage.devino.ca: no such host"
        self.assertEqual(main.classify_mc_error(1, text), "transport")

    def test_digits_in_an_artifact_name_are_not_status_codes(self):
        # mc echoes the object key, so a bare numeric rule would read these as
        # 5xx and retry a missing object five times under the wrong label.
        for name in ("coverage-503", "shard-502", "e2e-500", "build-429", "run-504"):
            text = (
                "mc: <ERROR> Unable to validate source "
                "`devino/gh-artifacts/o/r/1/%s.tgz`. Object does not exist." % name
            )
            self.assertEqual(main.classify_mc_error(1, text), "not-found", name)

    def test_digits_in_an_artifact_name_are_not_auth_codes(self):
        for name in ("coverage-403", "smoke-401"):
            text = (
                "mc: <ERROR> Unable to validate source "
                "`devino/gh-artifacts/o/r/1/%s.tgz`. Object does not exist." % name
            )
            self.assertEqual(main.classify_mc_error(1, text), "not-found", name)

    def test_socket_details_are_not_status_codes(self):
        # The observed reset carries ports and IPs; none of them is an HTTP code.
        self.assertEqual(main.classify_mc_error(1, RESET), "transport")
        quiet = "mc: <ERROR> Unable to stat `devino/gh-artifacts/o/r/500/a.tgz`. Object does not exist."
        self.assertEqual(main.classify_mc_error(1, quiet), "not-found")

    def test_unrecognised_failure_is_unknown(self):
        self.assertEqual(main.classify_mc_error(1, "mc: <ERROR> something new"), "unknown")

    def test_unknown_output_is_never_silently_ok(self):
        self.assertNotEqual(main.classify_mc_error(1, ""), "ok")


class TestRetryDelay(unittest.TestCase):
    def test_schedule_is_exponential(self):
        rng = mock.Mock()
        rng.random.return_value = 0.5  # no jitter at the midpoint
        got = [main.retry_delay(i, rng=rng) for i in range(5)]
        self.assertEqual(got, [2.0, 4.0, 8.0, 16.0, 32.0])

    def test_jitter_stays_within_25_percent(self):
        for i, base in enumerate(main.MC_BACKOFF):
            for value in (0.0, 0.5, 1.0):
                rng = mock.Mock()
                rng.random.return_value = value
                d = main.retry_delay(i, rng=rng)
                self.assertGreaterEqual(d, base * 0.75)
                self.assertLessEqual(d, base * 1.25)

    def test_jitter_actually_varies(self):
        values = {round(main.retry_delay(0), 6) for _ in range(50)}
        self.assertGreater(len(values), 1)

    def test_index_is_clamped(self):
        rng = mock.Mock()
        rng.random.return_value = 0.5
        self.assertEqual(main.retry_delay(99, rng=rng), float(main.MC_BACKOFF[-1]))
        self.assertEqual(main.retry_delay(-3, rng=rng), float(main.MC_BACKOFF[0]))


class TestStoreRun(unittest.TestCase):
    def test_success_runs_once_and_never_sleeps(self):
        with harness((0, "done")) as (store, runner, sleeps, _):
            rc, out = store.run("cp", "--quiet", "src", "dst")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "done")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(store.last_error_kind, "ok")
        self.assertEqual(store.last_attempts, 1)

    def test_transport_error_then_success(self):
        with harness((1, RESET), (1, RESET), (0, "ok")) as (store, runner, sleeps, buf):
            rc, _ = store.run("cp", "--quiet", "src", "dst")
        self.assertEqual(rc, 0)
        self.assertEqual(len(runner.calls), 3)
        self.assertEqual(len(sleeps), 2)
        self.assertLess(sleeps[0], sleeps[1])  # backoff grows
        self.assertGreaterEqual(sleeps[0], 2 * 0.75)
        self.assertLessEqual(sleeps[1], 4 * 1.25)
        self.assertEqual(store.last_attempts, 3)
        self.assertIn("::warning::", buf.getvalue())
        self.assertIn("retrying", buf.getvalue())

    def test_transport_error_exhausts_attempts_then_fails(self):
        with harness((1, RESET)) as (store, runner, sleeps, buf):
            with self.assertRaises(SystemExit) as cm:
                store.run("cp", "--quiet", "src", "dst")
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(len(runner.calls), main.MC_ATTEMPTS)
        self.assertEqual(len(sleeps), main.MC_ATTEMPTS - 1)
        text = buf.getvalue()
        self.assertIn("failed after %d attempt(s)" % main.MC_ATTEMPTS, text)
        self.assertIn("transport error", text)
        self.assertIn("connection reset by peer", text)  # mc's text, verbatim

    def test_not_found_is_never_retried(self):
        with harness((1, MISSING)) as (store, runner, sleeps, buf):
            with self.assertRaises(SystemExit):
                store.run("cp", "--quiet", "src", "dst")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(sleeps, [])
        self.assertIn("not-found error", buf.getvalue())

    def test_auth_failure_is_never_retried(self):
        with harness((1, DENIED)) as (store, runner, sleeps, _):
            with self.assertRaises(SystemExit):
                store.run("cp", "--quiet", "src", "dst")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(store.last_error_kind, "auth")

    def test_unknown_failure_is_never_retried(self):
        with harness((1, "mc: <ERROR> what")) as (store, runner, sleeps, _):
            with self.assertRaises(SystemExit):
                store.run("cp", "--quiet", "src", "dst")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(sleeps, [])

    def test_check_false_returns_instead_of_exiting(self):
        with harness((1, MISSING)) as (store, runner, sleeps, _):
            rc, out = store.run("cp", "--quiet", "src", "dst", check=False)
        self.assertEqual(rc, 1)
        self.assertIn("does not exist", out)
        self.assertEqual(store.last_error_kind, "not-found")
        self.assertEqual(store.last_attempts, 1)

    def test_check_false_still_retries_transport_errors(self):
        with harness((1, RESET)) as (store, runner, sleeps, _):
            rc, _ = store.run("cp", "--quiet", "src", "dst", check=False)
        self.assertEqual(rc, 1)
        self.assertEqual(len(runner.calls), main.MC_ATTEMPTS)
        self.assertEqual(store.last_error_kind, "transport")

    def test_attempts_override(self):
        with harness((1, RESET)) as (store, runner, sleeps, _):
            with self.assertRaises(SystemExit):
                store.run("ls", "--json", "dst", attempts=2)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(len(sleeps), 1)

    def test_listing_is_retried_too(self):
        with harness((1, RESET), (0, "{}")) as (store, runner, sleeps, _):
            rc, _ = store.run("ls", "--json", "dst")
        self.assertEqual(rc, 0)
        self.assertEqual(len(runner.calls), 2)


class TestUploadPath(unittest.TestCase):
    """The upload path shells out through the same Store.run, so it inherits
    the retry. These assert the upload's own argument shape survives it."""

    def test_tagged_upload_retries_transport_and_succeeds(self):
        args = ["cp", "--quiet", "--tags", "retention=7", "/tmp/a.tgz", "devino/gh-artifacts/o/r/1/a.tgz"]
        with harness((1, RESET), (0, "")) as (store, runner, sleeps, _):
            rc, _ = store.run(*args)
        self.assertEqual(rc, 0)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(len(sleeps), 1)
        for call in runner.calls:
            self.assertEqual(call[-len(args):], args)  # same command each time
            self.assertIn("--tags", call)

    def test_upload_not_found_bucket_is_not_retried(self):
        # A bucket that does not exist is a configuration error, not a blip.
        text = "mc: <ERROR> Unable to copy. The specified bucket does not exist."
        with harness((1, text)) as (store, runner, sleeps, buf):
            with self.assertRaises(SystemExit):
                store.run("cp", "--quiet", "/tmp/a.tgz", "devino/nope/k.tgz")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(sleeps, [])


class TestDownloadErrorMessage(unittest.TestCase):
    def test_real_404_keeps_the_familiar_wording(self):
        msg = main.download_error_message("a", "gh-artifacts", "o/r/1/a.tgz", "not-found", 1, MISSING)
        self.assertTrue(msg.startswith("Artifact not found: a (s3://gh-artifacts/o/r/1/a.tgz)"))
        self.assertIn("does not exist", msg)

    def test_transport_failure_does_not_claim_the_artifact_is_missing(self):
        msg = main.download_error_message(
            "release-evidence-install-smoke-sdist-Linux-X64",
            "gh-artifacts",
            "DevinoSolutions/stealth-chrome-devtools-mcp/34640838095/"
            "release-evidence-install-smoke-sdist-Linux-X64.tgz",
            "transport",
            main.MC_ATTEMPTS,
            RESET,
        )
        self.assertNotIn("Artifact not found", msg)
        self.assertIn("not a missing artifact", msg)
        self.assertIn("connection reset by peer", msg)
        self.assertIn("after %d attempt(s)" % main.MC_ATTEMPTS, msg)

    def test_auth_failure_is_labelled_as_such(self):
        msg = main.download_error_message("a", "b", "k", "auth", 1, DENIED)
        self.assertNotIn("Artifact not found", msg)
        self.assertIn("authorization error", msg)

    def test_every_message_is_ascii_encodable(self):
        # fail() prints to stdout. On Windows that is cp1252 before Python 3.15,
        # so a stray em-dash would raise UnicodeEncodeError inside the error
        # path and replace a useful message with a traceback.
        for kind in ("not-found", "transport", "auth", "unknown"):
            msg = main.download_error_message("a", "b", "k", kind, 5, RESET)
            msg.encode("ascii")  # raises on any non-ASCII character

    def test_unknown_failure_still_shows_mc_output_first(self):
        msg = main.download_error_message("a", "b", "k", "unknown", 1, "mc: <ERROR> weird")
        self.assertNotIn("Artifact not found", msg)
        self.assertIn("mc: <ERROR> weird", msg)


class TestDoDownload(unittest.TestCase):
    """End to end through do_download, so the call site is covered and not just
    the helper. Store is replaced with a pre-built one, so no network."""

    def _run(self, store, results):
        runner = FakeRun(*results)
        sleeps = []
        buf = io.StringIO()
        env = {
            "GITHUB_REPOSITORY": "DevinoSolutions/stealth-chrome-devtools-mcp",
            "GITHUB_RUN_ID": "34640838095",
            "INPUT_NAME": "release-evidence-install-smoke-sdist-Linux-X64",
            "INPUT_PATH": tempfile.mkdtemp(prefix="devino-dl-"),
        }
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            main.subprocess, "run", runner
        ), mock.patch.object(main.time, "sleep", sleeps.append), mock.patch.object(
            main, "Store", lambda: store
        ), contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                main.do_download()
        return runner, sleeps, buf.getvalue()

    def test_transport_error_is_reported_verbatim_and_retried(self):
        store = make_store()
        runner, sleeps, text = self._run(store, [(1, RESET)])
        self.assertEqual(len(runner.calls), main.MC_ATTEMPTS)
        self.assertEqual(len(sleeps), main.MC_ATTEMPTS - 1)
        errors = [ln for ln in text.splitlines() if ln.startswith("::error::")]
        self.assertEqual(len(errors), 1)
        self.assertNotIn("Artifact not found", errors[0])
        self.assertIn("not a missing artifact", errors[0])
        self.assertIn("connection reset by peer", text)

    def test_genuine_missing_artifact_keeps_the_old_message(self):
        store = make_store()
        runner, sleeps, text = self._run(store, [(1, MISSING)])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(sleeps, [])
        errors = [ln for ln in text.splitlines() if ln.startswith("::error::")]
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("::error::Artifact not found: "))


if __name__ == "__main__":
    unittest.main(verbosity=2)
