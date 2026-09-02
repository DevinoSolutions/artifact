# DevinoSolutions/artifact

Drop-in replacements for `actions/upload-artifact` and `actions/download-artifact`
that store artifacts on Devino's own MinIO (`https://storage.devino.ca`, bucket
`gh-artifacts`) instead of GitHub's metered artifact storage.

```yaml
permissions:
  id-token: write        # required: the action authenticates with the job's OIDC token

steps:
  - uses: DevinoSolutions/artifact/upload@v1
    with:
      name: playwright-report
      path: playwright-report/
      retention-days: 7

  - uses: DevinoSolutions/artifact/download@v1
    with:
      pattern: release-evidence-*
      merge-multiple: true
      path: release-evidence
```

## Why

The org is on GitHub's free plan (500 MB of artifact storage, hard-capped by a
$0 Actions budget). Once the quota is crossed every `upload-artifact` step is
rejected. Self-hosted runners do not help: the upstream action always writes
to GitHub's blob store. This action writes to devino instead.

## How it works

1. The job's GitHub OIDC token (audience `storage.devino.ca`) is exchanged with
   MinIO STS (`AssumeRoleWithWebIdentity`) for one-hour credentials. MinIO picks the policy named after the token's
   `repository_owner_id` claim, so only workflows owned by DevinoSolutions get
   access; tokens from any other owner map to no policy and are refused.
2. A pinned `mc` (MinIO client) is fetched from `storage.devino.ca/tools/`
   (fallback: dl.min.io) and cached in the runner tool cache.
3. Upload: matched files are packed into one `.tgz` whose root mirrors
   upstream semantics (a single directory uploads its contents; several
   paths share their least common ancestor), then copied to
   `gh-artifacts/<owner>/<repo>/<run_id>/<name>.tgz`. Large files are
   uploaded as 16 MiB multipart chunks, which keeps each request under
   Cloudflare's 100 MB limit.
4. Download: fetches by `name`, by `pattern` (with `merge-multiple`), or all
   artifacts of the run, and extracts safely into `path`.

Retention is enforced by bucket lifecycle rules: `retention-days` is rounded
up to 1/3/5/7/14/30/90 days and stored as an object tag (values above 90 are
capped at 90). When `retention-days` is not set the artifact expires after
14 days, which matches how the org's test reports and screenshots are used.

## Inputs

`upload`: `name`, `path`, `if-no-files-found`, `retention-days`,
`compression-level`, `include-hidden-files`, `overwrite` (accepted, always
overwrites). Outputs: `artifact-id`, `artifact-url`, `artifact-digest`.

`download`: `name`, `path`, `pattern`, `merge-multiple`, `run-id`,
`repository`, `github-token` (ignored). Output: `download-path`.

Both accept `endpoint`, `bucket`, `access-key`, `secret-key` to bypass OIDC.

## Finding an artifact afterwards

Every step writes the `s3://` location to the job summary. Fetch it with:

```sh
mc cp devino/gh-artifacts/<owner>/<repo>/<run_id>/<name>.tgz .
```

or browse `https://minio-console.devino.ca`.

## Runner requirements

Python 3.8+ and `bash` (all GitHub-hosted images and the `ubuntu-devino`
ARC image qualify). Supported platforms: Linux x64/arm64, macOS x64/arm64,
Windows x64.
