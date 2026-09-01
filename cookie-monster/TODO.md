# TODO — future versions

This file tracks work explicitly **out of scope** for the current
milestone (Gmail OAuth → scan → classify → dashboard → confirm). Nothing
here should be started until the current milestone has been used on real
data and judged useful.

## Milestone 2 — Privacy research (done, for deletion; other request types not started)

Deletion-method research is now automatic and self-growing - `app/deletion_research.py`
(interface + `WebResearchProvider`), `app/research_crawl.py` (same-domain
crawl), `app/research_search.py` (optional Brave Search fallback),
`app/research_extract.py` (regex + optional Claude-assisted extraction),
cached in the shared `deletion_recipes` table (`app/deletion_resolver.py`).
See README "How deletion-method research works". Still open:

- The research pipeline only looks for **deletion** rights today. Right to
  know/access, correction rights, and sale/sharing opt-out use the same
  `DeletionMethod`/recipe shape conceptually but nothing extracts or stores
  them yet - would need either a second recipe type per company or an
  additional field set on the existing one, plus matching UI.
- `WebResearchProvider`'s coverage depends on what's configured: with no
  `BRAVE_SEARCH_API_KEY`/`ANTHROPIC_API_KEY`, only the same-domain crawl +
  regex extraction run. Real, but narrower - more companies land in
  `NEEDS_RESEARCH` than would with both enabled.
- The background research worker (`app/deletion_queue.py`) is a single
  in-process `asyncio` loop - the *queue state* is durable (it lives in the
  `deletion_recipes` table, so a restart never loses queued work), but the
  worker itself is not distributed/multi-process. Fine for one local user;
  a real product would swap this for Celery/RQ/cloud tasks against the same
  `deletion_resolver.process_pending()` entry point.
- No rate-limit/backoff tuning beyond "N per tick, cheap network no-ops on
  failure" - see "Longer-term product ideas" below for real retry/backoff.

## Milestone 3 — Request generation (deletion done; others not started)

Deletion requests work end-to-end for every method the recipe cache can
support (`deletion_engine.py`) - see README "What's really automatic when
you click Delete my data". Still open:

- Request types beyond deletion (access/know, correct, opt-out of
  sale/sharing) - only deletion is wired up today.
- `DeletionStatus.VERIFICATION_NEEDED`/`IN_PROGRESS`/`MORE_INFO_REQUIRED`/
  `REJECTED`/`UNKNOWN_RESPONSE` exist in the state machine but nothing sets
  them yet - see "Milestone 4" below.
- `DeletionMethod.API` is fully wired in the engine and the recipe model,
  but no recipe has a real, documented deletion API yet, so it's inert.
- The email draft (`deletion_engine.build_email_draft`) is a generic
  CCPA/CPRA template using only your connected Gmail address - it doesn't
  yet use a recipe's `required_subject`/`required_request_fields`
  (fields exist on `DeletionRecipe` but nothing extracts or applies them
  yet), and there's no user identity profile to draw a name/jurisdiction
  from - see "Milestone 5" below.

## Milestone 4 — Company response tracking (not started, needs a scope decision)

Reading a company's reply to a sent deletion email and classifying it
(`IN_PROGRESS`/`VERIFICATION_NEEDED`/`MORE_INFO_REQUIRED`/`COMPLETED`/
`REJECTED`/`UNKNOWN_RESPONSE`). Requires `gmail.readonly` - Gmail's OAuth
model has no thread/label-scoped grant, and `gmail.metadata` cannot return
body text, so there's no narrower scope that can read a reply's content
(verified against current third-party documentation of Gmail API scopes,
not assumed). This is a materially bigger permission than anything in the
current version and needs its own explicit user decision before building -
see README "Not requested (yet): gmail.readonly".

If/when approved:

- `Company.deletion_thread_id` is already captured at send time (this
  version), so a response-tracker can filter to *only* threads Cookie
  Monster itself started - never a general inbox read.
- Classification must be conservative: an acknowledgment, a support ticket
  creation, or a verification request must never become `COMPLETED` -
  `COMPLETED` requires the company's own text stating the deletion actually
  happened. Ambiguous replies become `UNKNOWN_RESPONSE`, not a guess.
- Needs to run as background/periodic polling (same durable-queue-state
  approach as `deletion_queue.py`), never a per-page-load Gmail call, and
  needs to be idempotent (never process the same message twice) and rate
  limited (Gmail API quotas).

## Milestone 5 — User identity profile (not started)

A small, explicit profile (full name, primary email, optional alternate
emails, optional state/jurisdiction) the user fills in once, used to
personalize deletion request drafts beyond just the connected Gmail
address. Anything beyond name/email (government ID, DOB, full address,
financial info) must never be sent without the user explicitly reviewing
and approving that specific disclosure - never auto-attached because a
company's recipe says it wants it.

## Longer-term product ideas (explicitly deferred)

- Multi-user accounts, auth, and per-user data isolation. Note: the
  `DeletionRecipe` cache is already architected as global/shared rather than
  per-user (that's the whole point of it - see README), so this is mostly
  about `Company`/`OAuthToken`/`DeletionEvent` gaining a `user_id` and the
  routes gaining auth, not about redesigning the recipe cache.
- Real audit-trail-driven notifications (verification needed, request
  completed, request failed/rejected) - the event model in
  `deletion_events.py` already carries enough structure to support this
  later; no notification infrastructure exists yet.
- A reviewed, tested `WebResearchProvider` improvement pass: sitemap.xml
  parsing (currently skipped - homepage links + common-path guesses only),
  a second search backend option (Google Programmable Search / Tavily /
  Exa) behind the existing `SearchBackend` interface, and real-world
  accuracy tuning of the regex/LLM extraction against a sample of actual
  companies once this runs somewhere with unrestricted network access.
- Production deployment (currently: local-only, `127.0.0.1`, no auth).
- Move OAuth token storage from a local encrypted SQLite row to a real
  secrets manager / KMS.
- Support additional inbox providers (Outlook, Yahoo, IMAP).
- Batch Gmail API requests instead of one-by-one `messages.get` calls, to
  scan larger inboxes efficiently.
- Replace the regex classifier with a reviewed ML/LLM-assisted classifier,
  keeping the same "human must confirm/reject" gate and the same
  "detected because: ..." explanation requirement.
- Learn from user corrections (spec section 11: corrections should improve
  future classification) — currently corrections only override that one
  company's record, they don't feed back into the ruleset.
- Assisted (not automated) identity verification flows, with the user still
  performing anything CAPTCHA-gated or login-gated themselves.
- Potential complementary integration with California's DROP platform for
  data-broker requests (DROP itself is out of scope to automate — see
  README). Any such integration must preserve DROP's own consent and
  verification flow, not bypass it.
- Formal legal review of any auto-generated privacy-rights language before
  it's presented as more than a draft/suggestion.
- Rate limiting / abuse protection if this ever moves beyond a
  single-user local tool.
- Replace the hand-rolled additive migration in `app/migrations.py` with a
  real migration framework (e.g. Alembic) if the schema keeps evolving -
  today's version only knows how to add columns and normalize the specific
  legacy `deletion_status`/`deletion_evidence` shape from the previous
  version; it isn't a general migration tool.
- `index.html` (the home page) still uses pre-redesign CSS classes
  (`.hero`, `.lede`, `.permission-list`, `.connected`, `.card.warning`,
  `.fine-print`) that no longer exist in `style.css` after the Y2K redesign
  of `dashboard.html`/`base.html` - it likely renders unstyled. Worth a pass
  once the redesign continues past the dashboard.

## Explicitly not planned (guardrails, not gaps)

- No billing, no ads, no data monetization, no CAPTCHA bypass, no
  automated privacy-request submission without explicit per-request user
  approval, no scraping of authenticated pages without explicit user
  authorization, no impersonating the user, no circumventing login
  requirements.
