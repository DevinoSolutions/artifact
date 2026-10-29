# Changelog

Callers pin the floating `v1` tag (`DevinoSolutions/artifact/upload@v1`, `.../download@v1`)
or a full commit SHA. Each release's full notes are on its GitHub Release.

## Tag moves

- **2026-10-14: `v1` moved from `739b3ab` (v1.1.0) to `44df166` (v1.2.1).** Until then, every
  `@v1` caller still ran v1.1.0, which has no retries and whose only fallback for the `mc`
  client download is `dl.min.io`, which now returns HTTP 410. No input, output, default or
  object-key change: `upload/action.yml` and `download/action.yml` are identical between the
  two. The one visible difference is the wording of a failed download that is not a 404 (see
  v1.2.0).

## Unreleased

- A failed `mc` call whose output names a Cloudflare origin error (520, 521, 522, 523 or 524)
  is now a transport error, retried on the usual schedule (5 attempts). Before, only 429, 500,
  502, 503 and 504 counted, so `error code 520` read as unknown and a download gave up after 1
  attempt: that failed a whole DevinoSolutions/stealth-chrome-devtools-mcp publish (run
  40937037630, attempt 1). 501, 525 and 526 are configuration answers and still fail at once,
  as does a missing object.
- The GitHub OIDC token fetch and the MinIO STS exchange now retry for about 2 minutes on the
  same jittered 2/4/8/16/32 s schedule as `mc`, instead of 3 attempts over about 12 s. Only HTTP
  429, HTTP 5xx and connection errors or timeouts are retried; any other 4xx fails at once as
  before. Each retry logs one line naming the attempt and the reason. A GitHub-side 503 that
  lasted past the old budget failed uploads in two runs of
  DevinoSolutions/stealth-chrome-devtools-mcp (39956132580, 40398194992).

## v1.2.1 (2026-09-13, `44df166`)

- The `mc` client download falls back to the `minio/mc` GitHub release assets after the org
  mirror and `dl.min.io`. Every download, from any source, is checked against a pinned
  sha256 for linux-amd64, linux-arm64, darwin-amd64, darwin-arm64 and windows-amd64 (#3).
- This is the release to pin.

## v1.2.0 (2026-09-13, `6a4cf63`)

- `mc` transport failures are retried: 5 attempts, backoff 2/4/8/16 s with +/-25% jitter. A
  real 404 and a rejected credential are never retried (#2).
- A failed download says "Artifact not found" only for a real 404; any other failure now
  leads with `mc`'s own error text.

## v1.1.0 (2026-09-02, `739b3ab`)

- Adds a 90-day retention bucket; an unset `retention-days` now expires after 14 days (#1).

## v1.0.0 (2026-09-02, `31a7d9f`)

- First release: self-hosted upload/download actions backed by the shared MinIO store at
  storage.devino.ca, authenticated with GitHub OIDC and a claim-based policy.
