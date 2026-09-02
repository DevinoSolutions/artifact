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
    urls = [
        "%s/tools/mc/%s/%s/%s" % (endpoint.rstrip("/"), MC_VERSION, key, binname),
        "https://dl.min.io/client/mc/release/%s/archive/mc.%s" % (key, MC_VERSION),
    ]
    tmp = dest_dir / ("%s.%d.tmp" % (binname, os.getpid()))
    last = None
    for url in urls:
        try:
            data = http(urllib.request.Request(url), timeout=180)
            with open(str(tmp), "wb") as f:
                f.write(data)
            os.chmod(str(tmp), 0o755)
            os.replace(str(tmp), str(dest))
            log("Installed mc %s from %s" % (MC_VERSION, url))
            return str(dest)
        except Exception as e:  # noqa: BLE001
            last = e
    fail("Could not download the MinIO client: %s" % last)


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

    def run(self, *args, check=True):
        cmd = [self.mc, "--config-dir", self.cfg, "--no-color", "--disable-pager"] + list(args)
        p = subprocess.run(cmd, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = p.stdout.decode("utf-8", "replace")
        if check and p.returncode != 0:
            fail("mc %s failed (exit %d):\n%s" % (args[0], p.returncode, out.strip()))
        return p.returncode, out

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
            fail("Artifact not found: %s (s3://%s/%s)\n%s" % (n, store.bucket, key, out.strip()))
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
