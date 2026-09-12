"""Unit tests for the `mc` download, its source list and its checksum pin.

Standard library only, like lib/main.py itself: `python -m unittest discover -s tests`.
Every test patches `main.http`, so nothing here touches the network, dl.min.io,
github.com or the storage endpoint.

The source list grew a third entry because dl.min.io began answering 410 Gone on
2026-09-11/12 (MinIO archived the client), which reds every consumer job:

    ##[error]Could not download the MinIO client: HTTP 410: 410 Gone
"""
import contextlib
import hashlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "devino_artifact_main_mc", os.path.join(ROOT, "lib", "main.py")
)
main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(main)

ENDPOINT = "https://storage.devino.ca"
GONE = "HTTP 410: 410 Gone\nThe open-source MinIO Client (mc) project is archived."
GOOD = b"\x7fELF fake mc binary"
GOOD_SHA = hashlib.sha256(GOOD).hexdigest()
EVIL = b"\x7fELF something else entirely"


class FakeHTTP(object):
    """Replays one reply per URL. A str reply is raised as RuntimeError."""

    def __init__(self, replies):
        self.replies = replies
        self.urls = []

    def __call__(self, req, timeout=None, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else req
        self.urls.append(url)
        reply = self.replies[min(len(self.urls) - 1, len(self.replies) - 1)]
        if isinstance(reply, str):
            raise RuntimeError(reply)
        return reply


@contextlib.contextmanager
def runner(os_name="Linux", arch="X64", key="linux-amd64", replies=(GOOD,)):
    """Run ensure_mc on a throwaway tool cache with `http` patched out."""
    cache = tempfile.mkdtemp(prefix="devino-mc-test-")
    fake = FakeHTTP(list(replies))
    env = {"RUNNER_OS": os_name, "RUNNER_ARCH": arch, "RUNNER_TOOL_CACHE": cache}
    buf = io.StringIO()
    with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
        main, "http", fake
    ), mock.patch.dict(main.MC_SHA256, {key: GOOD_SHA}), contextlib.redirect_stdout(buf):
        yield fake, cache, buf


def installed(cache, key, binname="mc"):
    return os.path.join(cache, "devino-mc", main.MC_VERSION, key, binname)


class EnsureMcSources(unittest.TestCase):
    def test_the_pinned_version_has_a_digest_for_every_supported_platform(self):
        platforms = {"linux-amd64", "linux-arm64", "darwin-amd64", "darwin-arm64", "windows-amd64"}
        self.assertEqual(set(main.MC_SHA256), platforms)
        for key, digest in main.MC_SHA256.items():
            self.assertRegex(digest, r"^[0-9a-f]{64}$", key)
        self.assertEqual(len(set(main.MC_SHA256.values())), len(platforms), "digests must differ")

    def test_mirror_is_tried_first_and_a_match_is_installed(self):
        with runner() as (fake, cache, buf):
            path = main.ensure_mc(ENDPOINT)
        self.assertEqual(len(fake.urls), 1)
        self.assertTrue(fake.urls[0].startswith(ENDPOINT + "/tools/mc/"))
        self.assertEqual(path, installed(cache, "linux-amd64"))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), GOOD)
        self.assertIn("verified", buf.getvalue())

    def test_falls_back_to_the_github_release_asset_when_both_mirrors_fail(self):
        replies = ["HTTP 404: NoSuchKey", GONE, GOOD]
        with runner(replies=replies) as (fake, cache, buf):
            path = main.ensure_mc(ENDPOINT)
        self.assertEqual(len(fake.urls), 3)
        self.assertEqual(
            fake.urls[2],
            "https://github.com/minio/mc/releases/download/%s/mc.linux-amd64.%s"
            % (main.MC_VERSION, main.MC_VERSION),
        )
        self.assertTrue(os.path.isfile(path))

    def test_windows_asks_for_the_exe_asset(self):
        replies = ["HTTP 404: NoSuchKey", GONE, GOOD]
        with runner("Windows", "X64", "windows-amd64", replies) as (fake, cache, buf):
            path = main.ensure_mc(ENDPOINT)
        self.assertEqual(
            fake.urls[2],
            "https://github.com/minio/mc/releases/download/%s/mc.windows-amd64.%s.exe"
            % (main.MC_VERSION, main.MC_VERSION),
        )
        self.assertEqual(path, installed(cache, "windows-amd64", "mc.exe"))

    def test_a_cached_binary_is_reused_without_any_download(self):
        with runner() as (fake, cache, buf):
            first = main.ensure_mc(ENDPOINT)
            second = main.ensure_mc(ENDPOINT)
        self.assertEqual(first, second)
        self.assertEqual(len(fake.urls), 1, "second call must not hit the network")


class EnsureMcChecksum(unittest.TestCase):
    def test_a_mismatched_payload_is_not_installed_and_the_next_source_is_tried(self):
        with runner(replies=[EVIL, GONE, GOOD]) as (fake, cache, buf):
            path = main.ensure_mc(ENDPOINT)
        self.assertEqual(len(fake.urls), 3)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), GOOD, "the tampered payload must never reach disk")

    def test_every_source_mismatching_fails_and_installs_nothing(self):
        buf = io.StringIO()
        with runner(replies=[EVIL]) as (fake, cache, buf):
            with self.assertRaises(SystemExit):
                main.ensure_mc(ENDPOINT)
        self.assertEqual(len(fake.urls), 3)
        self.assertFalse(os.path.exists(installed(cache, "linux-amd64")))
        text = buf.getvalue()
        self.assertIn("sha256 mismatch", text)
        self.assertIn("expected " + GOOD_SHA, text)

    def test_no_leftover_temp_file_after_a_failure(self):
        with runner(replies=[EVIL]) as (fake, cache, buf):
            with self.assertRaises(SystemExit):
                main.ensure_mc(ENDPOINT)
        d = os.path.dirname(installed(cache, "linux-amd64"))
        self.assertEqual([f for f in os.listdir(d) if f.endswith(".tmp")], [])


class EnsureMcErrorReport(unittest.TestCase):
    def _fail_text(self, replies):
        with runner(replies=replies) as (fake, cache, buf):
            with self.assertRaises(SystemExit):
                main.ensure_mc(ENDPOINT)
        return fake, buf.getvalue()

    def test_the_error_names_every_url_tried_one_per_line(self):
        fake, text = self._fail_text(["HTTP 404: NoSuchKey", GONE, "HTTP 500: nope"])
        lines = text.splitlines()
        for url in fake.urls:
            self.assertEqual(
                len([ln for ln in lines if ln.strip().startswith(url + ":")]),
                1,
                "expected exactly one line for %s in:\n%s" % (url, text),
            )

    def test_each_url_keeps_its_own_error_not_only_the_last(self):
        fake, text = self._fail_text(["HTTP 404: NoSuchKey", GONE, "HTTP 500: nope"])
        mirror, dlmin, github = fake.urls
        self.assertRegex(text, re_line(mirror, "NoSuchKey"))
        self.assertRegex(text, re_line(dlmin, "410 Gone"))
        self.assertRegex(text, re_line(github, "HTTP 500"))

    def test_the_failure_is_a_single_error_annotation(self):
        _, text = self._fail_text([GONE])
        self.assertEqual(len([ln for ln in text.splitlines() if ln.startswith("::error::")]), 1)

    def test_an_unsupported_platform_still_fails_before_any_download(self):
        with runner("Plan9", "X64") as (fake, cache, buf):
            with self.assertRaises(SystemExit):
                main.ensure_mc(ENDPOINT)
        self.assertEqual(fake.urls, [])


def re_line(url, needle):
    import re

    return re.compile(r"%s:.*%s" % (re.escape(url), re.escape(needle)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
