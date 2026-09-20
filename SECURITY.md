# Security Policy

## Reporting a vulnerability

This is a portfolio/reference project, not a production service with an
active user base — but if you find a genuine security issue (e.g. a
signature-verification bypass, an injection vector, a credential leak),
please report it privately rather than opening a public issue: open a
[GitHub Security Advisory](../../security/advisories/new) on this
repository, or contact the maintainer directly. Please don't file a
public issue for anything that could be actively exploited before a fix
ships.

Include: what you found, how to reproduce it, and the potential impact.
Expect an acknowledgment within a few days; this is maintained on a
best-effort basis, not under an SLA.

## Supported versions

There is no formal release/support cadence yet (see `ROADMAP.md`) — fixes
land on `main`. Once tagged releases exist, only the latest tag receives
security fixes.

## Known security-relevant design points

Documented here so a reviewer doesn't have to rediscover them by reading
every phase report:

- **Webhook authenticity.** `agents/watcher/main.py` verifies GitHub's
  `X-Hub-Signature-256` HMAC (SHA-256, constant-time comparison) against
  `GITHUB_WEBHOOK_SECRET` before processing any payload. A missing/wrong
  signature is rejected with 401 before any parsing happens. **Rotate
  `GITHUB_WEBHOOK_SECRET`** away from the `.env.example` default before
  exposing a watcher instance publicly.
- **Strict, closed message contracts.** Every `shared/contracts.py` model
  uses `strict=True, extra="forbid"` — an unexpected field or a
  type-coerced value is rejected rather than silently accepted, which
  closes off a class of injection/confusion attack via a malformed
  message on the broker.
- **No secrets in the message envelope.** `Envelope`/payload models never
  carry credentials; LLM API keys and the Slack webhook URL are read from
  environment variables only, never logged (`shared/llm.py`/`shared/slack.py`
  log provider/model/outcome, never the key or the full webhook URL).
- **Default credentials are dev-only.** `.env.example`'s passwords
  (`swarm_dev_password`, `admin`/`admin` for Grafana) are placeholders for
  local development. `docker-compose.prod.yml` deliberately has **no**
  fallback for any credential — it refuses to start without one set
  explicitly (see that file's `${VAR:?required}` syntax).
- **Non-root containers.** Every agent Dockerfile
  (`agents/*/Dockerfile`) runs as a dedicated non-root `swarm` user
  (uid/gid 10001) in the runtime stage.
- **No secrets committed.** `.env` is gitignored; only `.env.example`
  (placeholder values) is tracked. If you accidentally commit a real
  credential, rotate it immediately — removing it from a later commit
  does not remove it from git history.
- **Dependency-confusion-shaped surface.** `agents/researcher/repository.py`
  runs `git clone`/`fetch`/`checkout` against a URL built from an
  attacker-influenced field (`CommitDetected.repo`, ultimately from a
  GitHub webhook payload) — the base URL is fixed by
  `REPO_CLONE_BASE_URL` (an operator-controlled env var, not
  payload-controlled), which limits this to "clone attacker-chosen
  repos/branches under a fixed host," not arbitrary URL fetching.
- **LLM prompt inputs are commit metadata, not user-authenticated
  input** in the traditional sense, but they do originate from whoever
  can push to a watched branch — `agents/reviewer/prompts.py` explicitly
  instructs the model never to override the deterministically-computed
  risk score (`agents/reviewer/scoring.py`), so a crafted commit
  message/diff can influence the narrative's wording but not the
  score/severity/status that actually gates the review outcome.
