#!/usr/bin/env python3
"""Devino self-hosted artifact action.

Drop-in replacement for actions/upload-artifact and actions/download-artifact.
Artifacts are stored as one .tgz per artifact name on Devino's MinIO
(storage.devino.ca) under <owner>/<repo>/<run_id>/<name>.tgz.

Credentials come from the job's GitHub OIDC token, exchanged with MinIO STS
(AssumeRoleWithWebIdentity). No long-lived secrets are needed; the job only
requires `permissions: id-token: write`.

Standard library only. Works on Linux, macOS and Windows (Python >= 3.8).
"""
import fnmatch
import hashlib
import json
import os
import pathlib
import platform
import random
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

MC_VERSION = "RELEASE.2025-08-13T08-35-41Z"
# sha256 of the pinned `mc` binary for each supported platform. Every download,
# from whichever source, is checked against this table before it is installed:
# the primary source is an org-controlled bucket and the last resort is an
# archived third-party repository, so the pin is what makes the three sources
# interchangeable instead of three different trust levels.
#
# Taken on 2026-09-12 from the `.sha256sum` asset published next to each binary
# on the archived upstream release
# https://github.com/minio/mc/releases/tag/RELEASE.2025-08-13T08-35-41Z
# (e.g. mc.linux-amd64.RELEASE.2025-08-13T08-35-41Z.sha256sum). Re-pin these
# whenever MC_VERSION changes.
MC_SHA256 = {
    "linux-amd64": "01f866e9c5f9b87c2b09116fa5d7c06695b106242d829a8bb32990c00312e891",
    "linux-arm64": "14c8c9616cfce4636add161304353244e8de383b2e2752c0e9dad01d4c27c12c",
    "darwin-amd64": "2862c79cce11b09be9a8911a279b2e9465bebf74b9f01abca9c348a0d795f0cb",
    "darwin-arm64": "a877fd0c183409da9f20f9d6e1811987298bbbca1aa03428eebdffba79fb9445",
    "windows-amd64": "c8db13ebeda31497f354c0e950809db0ae9b2a2a69b8afee68c128c37300c157",
}
DEFAULT_ENDPOINT = "https://storage.devino.ca"
DEFAULT_BUCKET = "gh-artifacts"
# MinIO maps the token's repository_owner_id claim to a policy of the same name
# (claim-based mode), so no RoleArn is sent unless one is configured.
DEFAULT_ROLE_ARN = ""
DEFAULT_AUDIENCE = "storage.devino.ca"
# Lifecycle rules on the bucket expire objects tagged retention=<N> after N
# days; untagged objects expire after 90 days (same default as GitHub).
RETENTION_BUCKETS = [1, 3, 5, 7, 14, 30, 90]
GLOB_CHARS = set("*?[")

# `mc` does not retry, and this action is now on the critical path of required
# checks in every repository that moved off the GitHub artifact sink. A single
# TCP reset against the endpoint therefore reds a gate fleet-wide, so transport
# failures are retried with exponential backoff. MC_ATTEMPTS attempts means
# MC_ATTEMPTS - 1 waits: 2, 4, 8 and 16 s by default. The 32 s step is only
# reached if MC_ATTEMPTS is raised.
MC_ATTEMPTS = 5
MC_BACKOFF = [2, 4, 8, 16, 32]
MC_JITTER = 0.25  # +/- 25%, so a fleet-wide blip does not retry in lockstep


# ── GitHub Actions helpers ───────────────────────────────────────────────────
def log(msg):
    print(msg, flush=True)


def warn(msg):
    print("::warning::" + msg, flush=True)


def fail(msg, code=1):
    print("::error::" + msg, flush=True)
    sys.exit(code)


def mask(value):
    if value:
        print("::add-mask::" + value, flush=True)


def inp(name, default=""):
    v = os.environ.get("INPUT_" + name.upper().replace("-", "_"), "")
    return v if v.strip() != "" else default


def truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def append_file(env_name, text):
    path = os.environ.get(env_name)
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")


def set_output(key, value):
    append_file("GITHUB_OUTPUT", "%s=%s" % (key, value))


def summary(text):
    append_file("GITHUB_STEP_SUMMARY", text)


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


def temp_dir():
    base = os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    return tempfile.mkdtemp(prefix="devino-artifact-", dir=base)


USER_AGENT = "devino-artifact/1.0 (+https://github.com/DevinoSolutions/artifact)"


def http(req, timeout=120, retries=3):
    if isinstance(req, str):
        req = urllib.request.Request(req)
    req.add_header("User-Agent", USER_AGENT)
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            last = "HTTP %s: %s" % (e.code, body[:800])
            if 400 <= e.code < 500 and e.code != 429:
                break
        except Exception as e:  # noqa: BLE001
            last = repr(e)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(last)


# ── mc client ────────────────────────────────────────────────────────────────
# `mc` reports every failure on stderr with the same `mc: <ERROR> ...` shape and
# exits 1, so the exit code alone cannot tell a missing object from a dead
# socket. These patterns read the message instead. They are ordered: a DNS
# failure says "no such host" and must not be read as a missing object, and a
# 503 must not be read as a permission problem.
MC_TRANSPORT_RE = re.compile(
    r"connection reset"
    r"|connection refused"
    r"|connection timed out"
    r"|broken pipe"
    r"|i/o timeout"
    r"|tls handshake timeout"
    r"|context deadline exceeded"
    r"|\bno such host\b"
    r"|server misbehaving"
    r"|temporary failure in name resolution"
    r"|network is unreachable"
    r"|host is unreachable"
    r"|unexpected eof"
    r"|:\s*EOF\b"
    r"|bad gateway"
    r"|service unavailable"
    r"|gateway time-?out"
    r"|internal server error"
    r"|\bslow ?down\b"
    r"|\binternalerror\b"
    r"|\brequesttimeout\b"
    r"|too many requests"
    # A bare status code is only a status code when something says so. The
    # object key is echoed in mc's error text, so an artifact literally named
    # "coverage-503" must not be read as a 503.
    r"|(?:status|code|responded with|returned)\s*[:=]?\s*(?:429|500|502|503|504)\b",
    re.IGNORECASE,
)
MC_AUTH_RE = re.compile(
    r"\baccess ?denied\b"
    r"|invalidaccesskeyid"
    r"|signaturedoesnotmatch"
    r"|expiredtoken"
    r"|invalidtoken"
    r"|token has expired"
    r"|permission denied"
    r"|\bforbidden\b"
    r"|\bunauthorized\b"
    r"|(?:status|code|responded with|returned)\s*[:=]?\s*(?:401|403)\b",
    re.IGNORECASE,
)
MC_NOT_FOUND_RE = re.compile(
    r"nosuchkey"
    r"|nosuchbucket"
    r"|nosuchversion"
    r"|no such object"
    r"|does not exist"
    r"|\bnot found\b"
    r"|\b404\b",
    re.IGNORECASE,
)


def first_line(text):
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:400]
    return ""


def classify_mc_error(returncode, output):
    """Classify one `mc` invocation.

    Returns "ok", "transport", "auth", "not-found" or "unknown".

    Only "transport" is retried. A missing object or a rejected credential is a
    deterministic answer: retrying it just delays the report by half a minute
    and hides the real cause behind four more identical lines.
    """
    if returncode == 0:
        return "ok"
    text = output or ""
    if MC_TRANSPORT_RE.search(text):
        return "transport"
    if MC_AUTH_RE.search(text):
        return "auth"
    if MC_NOT_FOUND_RE.search(text):
        return "not-found"
    return "unknown"


def retry_delay(attempt, rng=None):
    """Seconds to wait after a failed attempt (0-based). Jittered +/- MC_JITTER."""
    base = MC_BACKOFF[min(max(attempt, 0), len(MC_BACKOFF) - 1)]
    r = rng if rng is not None else random
    return base * (1.0 - MC_JITTER + 2.0 * MC_JITTER * r.random())


def ensure_mc(endpoint):
    osn = os.environ.get("RUNNER_OS") or platform.system()
    arch = os.environ.get("RUNNER_ARCH") or platform.machine()
    key = {
        ("Linux", "X64"): "linux-amd64",
        ("Linux", "ARM64"): "linux-arm64",
        ("macOS", "X64"): "darwin-amd64",
        ("macOS", "ARM64"): "darwin-arm64",
        ("Windows", "X64"): "windows-amd64",
    }.get((osn, arch))
    if not key:
        fail("Unsupported runner platform %s/%s" % (osn, arch))
    binname = "mc.exe" if osn == "Windows" else "mc"
    cache = os.environ.get("RUNNER_TOOL_CACHE") or os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    dest_dir = pathlib.Path(cache) / "devino-mc" / MC_VERSION / key
    dest = dest_dir / binname
    if dest.is_file():
        return str(dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    want = MC_SHA256.get(key)
    if not want:
        fail("No pinned sha256 for mc %s on %s; refusing to install an unverified binary" % (MC_VERSION, key))
    # Sources in order of preference: the org mirror, then the two public
    # copies. The mirror is populated and is normally the source that answers.
    #
    # On 2026-09-12 it did not. The Docker daemon on the storage host restarted
    # around 05:00Z; the shared MinIO compose has no `restart:` policy, so its
    # container stayed exited (255) while every other app on the host came back.
    # With no container, Traefik had no router for storage.devino.ca and the
    # requests fell through to another app, which 404s every MinIO path --
    # including /minio/health/live -- and answers with that app's headers. The
    # same failure appears twice in this service's deploy history as "Redeploy
    # shared MinIO - was returning 404". Starting the container restored it at
    # 08:10Z. So the mirror was down, not empty, and the 404 was a symptom.
    #
    # dl.min.io is gone for good: 410 Gone for every mc release since
    # 2026-09-11/12 ("the MinIO Client project is archived ... these files are
    # no longer served from this site"). The mirror being down and the secondary
    # being retired on the same day left no source at all, which is what took
    # every consumer job in the org down here. The release assets of the
    # archived github.com/minio/mc repository are the last public copy of this
    # build, so they go last: a fallback, not something to depend on.
    #
    # TODO: add `restart: unless-stopped` to the shared-minio compose (owner
    # action) so a daemon restart cannot take the mirror -- and with it STS and
    # every `mc cp`/`mc ls` in this file -- down until someone notices.
    urls = [
        "%s/tools/mc/%s/%s/%s" % (endpoint.rstrip("/"), MC_VERSION, key, binname),
        "https://dl.min.io/client/mc/release/%s/archive/mc.%s" % (key, MC_VERSION),
        "https://github.com/minio/mc/releases/download/%s/mc.%s.%s%s"
        % (MC_VERSION, key, MC_VERSION, ".exe" if osn == "Windows" else ""),
    ]
    tmp = dest_dir / ("%s.%d.tmp" % (binname, os.getpid()))
    errors = []
    for url in urls:
        try:
            data = http(urllib.request.Request(url), timeout=180)
            got = hashlib.sha256(data).hexdigest()
            if got != want:
                # Never install it, and do not stop: the next source may be
                # intact. A wrong binary is a worse outcome than no binary.
                errors.append(
                    (url, "sha256 mismatch: expected %s, got %s (%d bytes)" % (want, got, len(data)))
                )
                continue
            with open(str(tmp), "wb") as f:
                f.write(data)
            os.chmod(str(tmp), 0o755)
            os.replace(str(tmp), str(dest))
            log("Installed mc %s from %s (sha256 %s verified)" % (MC_VERSION, url, got))
            return str(dest)
        except Exception as e:  # noqa: BLE001
            errors.append((url, str(e) or repr(e)))
    # Name every source with its own error. When only the last one was reported,
    # a 410 from dl.min.io read as if the org mirror had never been consulted.
    fail(
        "Could not download the MinIO client %s for %s; all %d source(s) failed:\n%s"
        % (MC_VERSION, key, len(urls), "\n".join("  %s: %s" % (u, m) for u, m in errors))
    )


class Store(object):
    def __init__(self):
        self.endpoint = inp("endpoint", os.environ.get("ARTIFACT_ENDPOINT", DEFAULT_ENDPOINT)).rstrip("/")
        self.bucket = inp("bucket", os.environ.get("ARTIFACT_BUCKET", DEFAULT_BUCKET))
        self.mc = ensure_mc(self.endpoint)
        self.cfg = temp_dir()
        ak, sk, st = self.credentials()
        parsed = urllib.parse.urlparse(self.endpoint)
        host = "%s://%s:%s%s@%s" % (parsed.scheme, ak, sk, (":" + st) if st else "", parsed.netloc)
        self.env = dict(os.environ)
        self.env["MC_HOST_devino"] = host
        self.env["MC_CONFIG_DIR"] = self.cfg

    def credentials(self):
        ak = inp("access-key", os.environ.get("ARTIFACT_ACCESS_KEY", ""))
        sk = inp("secret-key", os.environ.get("ARTIFACT_SECRET_KEY", ""))
        if ak and sk:
            mask(sk)
            return ak, sk, ""
        url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
        tok = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
        if not url or not tok:
            fail(
                "No GitHub OIDC token is available to this job. Add\n"
                "    permissions:\n      id-token: write\n"
                "to the job (or the workflow), or pass access-key/secret-key inputs."
            )
        audience = inp("audience", DEFAULT_AUDIENCE)
        role_arn = inp("role-arn", DEFAULT_ROLE_ARN)
        sep = "&" if "?" in url else "?"
        req = urllib.request.Request(
            url + sep + "audience=" + urllib.parse.quote(audience),
            headers={"Authorization": "bearer " + tok, "Accept": "application/json; api-version=2.0"},
        )
        try:
            jwt = json.loads(http(req, timeout=60).decode("utf-8"))["value"]
        except Exception as e:  # noqa: BLE001
            fail("Could not obtain the GitHub OIDC token: %s" % e)
        params = {
            "Action": "AssumeRoleWithWebIdentity",
            "Version": "2011-06-15",
            "DurationSeconds": "3600",
            "WebIdentityToken": jwt,
        }
        if role_arn:
            params["RoleArn"] = role_arn
        data = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint + "/", data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        try:
            body = http(req, timeout=60)
        except Exception as e:  # noqa: BLE001
            fail("MinIO STS AssumeRoleWithWebIdentity failed: %s" % e)
        # The STS reply is a tiny, fixed-shape XML document from our own
        # server; pull the three fields with a regex rather than an XML parser.
        text = body.decode("utf-8", "replace")
        creds = {}
        for tag in ("AccessKeyId", "SecretAccessKey", "SessionToken"):
            m = re.search(r"<%s>\s*([^<]+?)\s*</%s>" % (tag, tag), text)
            if m:
                creds[tag] = m.group(1)
        if not creds.get("AccessKeyId"):
            fail("MinIO STS response had no credentials: %s" % text[:500])
        mask(creds["SecretAccessKey"])
        mask(creds.get("SessionToken", ""))
        return creds["AccessKeyId"], creds["SecretAccessKey"], creds.get("SessionToken", "")

    # Set by run(); read by callers that need to word an error correctly.
    last_error_kind = "ok"
    last_attempts = 0

    def run(self, *args, check=True, attempts=None):
        """Run one `mc` command, retrying transport failures only.

        Both verbs this action uses are safe to repeat: `mc cp` writes a whole
        object under a key derived from the run, and `mc ls` is read-only.
        """
        attempts = MC_ATTEMPTS if attempts is None else max(1, int(attempts))
        cmd = [self.mc, "--config-dir", self.cfg, "--no-color", "--disable-pager"] + list(args)
        rc, out, kind = 1, "", "unknown"
        for attempt in range(attempts):
            p = subprocess.run(cmd, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            rc = p.returncode
            out = p.stdout.decode("utf-8", "replace")
            kind = classify_mc_error(rc, out)
            self.last_error_kind = kind
            self.last_attempts = attempt + 1
            if kind == "ok":
                return rc, out
            if kind != "transport" or attempt == attempts - 1:
                break
            delay = retry_delay(attempt)
            warn(
                "mc %s: transport error, retrying in %.1fs (attempt %d of %d): %s"
                % (args[0], delay, attempt + 2, attempts, first_line(out))
            )
            time.sleep(delay)
        if check:
            fail(
                "mc %s failed after %d attempt(s) (exit %d, %s error):\n%s"
                % (args[0], self.last_attempts, rc, kind, out.strip())
            )
        return rc, out

    def target(self, key):
        return "devino/%s/%s" % (self.bucket, key)


def run_prefix():
    repository = inp("repository", os.environ.get("GITHUB_REPOSITORY", ""))
    run_id = inp("run-id", os.environ.get("GITHUB_RUN_ID", ""))
    if not repository or not run_id:
        fail("GITHUB_REPOSITORY / GITHUB_RUN_ID are not set")
    return "%s/%s/" % (repository, run_id)


def safe_name(name):
    name = name.strip()
    if not name:
        fail("Artifact name must not be empty")
    for ch in '\\/:"<>|*?\r\n':
        name = name.replace(ch, "_")
    return name


# ── upload ───────────────────────────────────────────────────────────────────
def glob_base(pattern):
    """Split a pattern into (base path without glob chars, remaining pattern parts)."""
    parts = pathlib.PurePath(pattern).parts
    base_parts = []
    for part in parts:
        if any(c in part for c in GLOB_CHARS):
            break
        base_parts.append(part)
    rest = parts[len(base_parts):]
    return base_parts, rest


def expand_pattern(pattern, cwd):
    """Return (files, search_base) for one include pattern, mimicking @actions/glob."""
    pattern = os.path.expanduser(pattern.strip().rstrip("/\\"))
    if not pattern:
        return [], None
    base_parts, rest = glob_base(pattern)
    base = pathlib.Path(*base_parts) if base_parts else pathlib.Path(".")
    if not base.is_absolute():
        base = cwd / base
    base = pathlib.Path(os.path.normpath(str(base)))
    files = []
    if not rest:
        if base.is_dir():
            files = [p for p in base.rglob("*") if p.is_file()]
            return files, base
        if base.is_file():
            return [base], base.parent
        return [], base.parent if base_parts else base
    if not base.is_dir():
        return [], base
    sub = str(pathlib.PurePosixPath(*[p.replace("\\", "/") for p in rest]))
    for m in base.glob(sub):
        if m.is_dir():
            files.extend(p for p in m.rglob("*") if p.is_file())
        elif m.is_file():
            files.append(m)
    return files, base


def hidden(rel_parts):
    return any(part.startswith(".") and part not in (".", "..") for part in rel_parts)


def resolve_files(path_input, include_hidden):
    cwd = pathlib.Path.cwd()
    includes, excludes = [], []
    for raw in path_input.splitlines():
        s = raw.strip()
        if not s:
            continue
        (excludes if s.startswith("!") else includes).append(s.lstrip("!").strip())
    files = {}
    bases = []
    for pat in includes:
        matched, base = expand_pattern(pat, cwd)
        if base is not None and (matched or not bases):
            bases.append(base)
        for f in matched:
            if not include_hidden:
                try:
                    rel = f.relative_to(base).parts
                except ValueError:
                    rel = f.parts
                if hidden(rel):
                    continue
            files[str(f)] = f
    for pat in excludes:
        matched, _ = expand_pattern(pat, cwd)
        for f in matched:
            files.pop(str(f), None)
    if not files:
        return [], None
    bases = [b for b in bases if b is not None]
    if len(bases) == 1:
        root = bases[0]
    else:
        try:
            root = pathlib.Path(os.path.commonpath([str(b) for b in bases]))
        except ValueError:
            root = cwd
    return sorted(files.values(), key=lambda p: str(p)), root


def retention_tag(days):
    if not days:
        return None
    try:
        n = int(str(days).strip())
    except ValueError:
        fail("retention-days must be an integer, got %r" % days)
    if n <= 0:
        return None
    for b in RETENTION_BUCKETS:
        if n <= b:
            return b
    return RETENTION_BUCKETS[-1]  # > 90 days: cap at the longest rule


def do_upload():
    name = safe_name(inp("name", "artifact"))
    path_input = inp("path")
    if not path_input.strip():
        fail("Input 'path' is required")
    include_hidden = truthy(inp("include-hidden-files", "false"))
    on_none = inp("if-no-files-found", "warn").strip().lower()
    level = inp("compression-level", "6")
    try:
        level = max(0, min(9, int(level)))
    except ValueError:
        level = 6

    files, root = resolve_files(path_input, include_hidden)
    if not files:
        msg = "No files were found with the provided path: %s. No artifacts will be uploaded." % path_input.strip().replace("\n", ", ")
        if on_none == "error":
            fail(msg)
        elif on_none == "warn":
            warn(msg)
        else:
            log(msg)
        return

    log("With the provided path, there will be %d file(s) uploaded (root: %s)" % (len(files), root))
    store = Store()
    work = temp_dir()
    archive = os.path.join(work, name + ".tgz")
    total = 0
    mode = "w:gz" if level > 0 else "w"
    kwargs = {"compresslevel": level} if level > 0 else {}
    with tarfile.open(archive, mode, **kwargs) as tf:
        for f in files:
            rel = os.path.relpath(str(f), str(root))
            if rel.startswith(".."):
                fail("File %s is outside the artifact root %s" % (f, root))
            tf.add(str(f), arcname=pathlib.PurePath(rel).as_posix(), recursive=False)
            total += f.stat().st_size
    size = os.path.getsize(archive)
    digest = hashlib.sha256()
    with open(archive, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)

    key = run_prefix() + name + ".tgz"
    tag = retention_tag(inp("retention-days", ""))
    args = ["cp", "--quiet"]
    if tag:
        args += ["--tags", "retention=%d" % tag]
    args += [archive, store.target(key)]
    t0 = time.time()
    store.run(*args)
    log("Uploaded %s (%s, %d files, %s raw) to s3://%s/%s in %.1fs" % (name, human(size), len(files), human(total), store.bucket, key, time.time() - t0))
    shutil.rmtree(work, ignore_errors=True)

    set_output("artifact-id", key)
    set_output("artifact-url", "s3://%s/%s" % (store.bucket, key))
    set_output("artifact-digest", "sha256:" + digest.hexdigest())
    summary(
        "📦 Artifact **%s** → `s3://%s/%s` (%s, %d files, expires in %s days)\n"
        % (name, store.bucket, key, human(size), len(files), tag or 90)
    )


# ── download ─────────────────────────────────────────────────────────────────
def safe_extract(archive, dest):
    dest = pathlib.Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as tf:
        members = []
        for m in tf.getmembers():
            p = pathlib.PurePosixPath(m.name)
            if p.is_absolute() or ".." in p.parts:
                fail("Refusing to extract unsafe path %s" % m.name)
            members.append(m)
        if hasattr(tarfile, "data_filter"):
            tf.extractall(str(dest), members=members, filter="data")
        else:
            tf.extractall(str(dest), members=members)
    return len(members)


def download_error_message(name, bucket, key, kind, attempts, output):
    """Word a failed download after its real cause.

    A genuine 404 keeps the familiar "Artifact not found". Anything else leads
    with `mc`'s own text, because announcing a TCP reset as a missing artifact
    sends whoever reads the annotation looking for an upload that exists and
    succeeded.
    """
    detail = (output or "").strip()
    if kind == "not-found":
        return "Artifact not found: %s (s3://%s/%s)\n%s" % (name, bucket, key, detail)
    label = {
        "transport": "transport error reaching the storage endpoint",
        "auth": "authorization error",
    }.get(kind, "mc error")
    # ASCII only: fail() prints to stdout, and on Windows that is the console
    # code page (cp1252 before Python 3.15), so a non-ASCII character here would
    # raise UnicodeEncodeError inside the error path itself.
    return (
        "Could not download artifact %s (s3://%s/%s) after %d attempt(s): %s, "
        "not a missing artifact.\n%s" % (name, bucket, key, attempts, label, detail)
    )


def do_download():
    name = inp("name", "").strip()
    pattern = inp("pattern", "").strip()
    merge = truthy(inp("merge-multiple", "false"))
    dest = inp("path", os.environ.get("GITHUB_WORKSPACE") or os.getcwd())
    dest = pathlib.Path(os.path.expanduser(dest))
    if not dest.is_absolute():
        dest = pathlib.Path.cwd() / dest
    prefix = run_prefix()
    store = Store()

    if name:
        wanted = [safe_name(name)]
    else:
        _, out = store.run("ls", "--json", store.target(prefix))
        names = []
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            k = obj.get("key", "")
            if k.endswith(".tgz"):
                names.append(k[:-4])
        wanted = [n for n in names if not pattern or fnmatch.fnmatchcase(n, pattern)]
    if not wanted:
        fail("Unable to find any artifacts for the associated workflow (prefix %s%s)" % (prefix, (", pattern " + pattern) if pattern else ""))

    work = temp_dir()
    lines = []
    for n in wanted:
        key = prefix + n + ".tgz"
        local = os.path.join(work, n + ".tgz")
        rc, out = store.run("cp", "--quiet", store.target(key), local, check=False)
        if rc != 0:
            fail(download_error_message(n, store.bucket, key, store.last_error_kind, store.last_attempts, out))
        target = dest if (name or merge) else dest / n
        count = safe_extract(local, target)
        log("Downloaded %s (%s, %d entries) to %s" % (n, human(os.path.getsize(local)), count, target))
        lines.append("- **%s** → `%s` (%s)" % (n, target, human(os.path.getsize(local))))
    shutil.rmtree(work, ignore_errors=True)
    set_output("download-path", str(dest))
    summary("📥 Downloaded %d artifact(s) from `s3://%s/%s`\n%s\n" % (len(wanted), store.bucket, prefix, "\n".join(lines)))


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("upload", "download"):
        fail("usage: main.py upload|download")
    if sys.argv[1] == "upload":
        do_upload()
    else:
        do_download()


if __name__ == "__main__":
    main()
