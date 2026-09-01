# 🍪 Cookie Monster

**Find who has your data. Take it back.**

Cookie Monster is a **local, private, single-user prototype**. It is not a SaaS
product, has no accounts/billing, and does not submit anything on your
behalf. It exists to answer one question:

> Does your own Gmail inbox contain enough information to rebuild a useful
> map of the companies you've actually given your data to?

## What it does (current version)

1. **Connect Gmail** via Google OAuth (read-only, metadata-only — see below).
2. **Scan** your inbox for evidence of company relationships: order
   confirmations, receipts, shipping notices, welcome/account emails,
   password resets, subscription/membership confirmations, loyalty
   programs, customer service threads, and marketing newsletters.
3. **Classify** each matching message with a transparent, rule-based
   detector (regex over Subject + sender domain) — every classification
   records *why* it fired.
4. **Aggregate** message-level detections into one row per company
   (domain, relationship type, evidence types, confidence, first/last seen,
   a few example subject lines).
5. **Review** the results in a dashboard: confirm, reject, correct, merge
   duplicates, search/filter.
6. **Resolve a deletion method** for each company automatically. The first
   time Cookie Monster (any user, any scan) ever sees a domain, it researches
   that company's own site for an official deletion process and caches a
   verified "recipe" — every company after that reuses the cache instantly.
   Research runs in the background so it never slows down a scan — see
   "How deletion-method research works" below.
7. **Delete my data**: one button per confirmed company. What it actually
   does depends on the company's verified method — see "What's really
   automatic" below. Nothing is ever submitted without you confirming it
   first, and nothing is marked "submitted" without real evidence that it
   happened.

Company-response tracking (reading a company's reply to a sent deletion
email) is **not built yet** — see `TODO.md`.

## How deletion-method research works

Every confirmed company's domain is checked against `DeletionRecipe` — a
local, shared, reusable cache table (not one row per user; see
`app/models.py`). If a domain is already there and still fresh (see
freshness below), it's used immediately. If not, the domain is queued
(`METHOD_LOOKUP` — no network call yet, so scanning is never slowed down)
and a background worker researches it shortly after:

1. **Same-domain crawl** (`app/research_crawl.py`, always on, no API key
   needed): fetches the company's own homepage and a handful of conventional
   paths (`/privacy`, `/ccpa`, `/privacy/requests`, etc.), looking for a
   privacy/deletion page.
2. **Optional search fallback** (`app/research_search.py`, Brave Search API,
   only if `BRAVE_SEARCH_API_KEY` is set): used only when step 1 finds
   nothing, scoped to `site:<company domain>`.
3. **Extraction** (`app/research_extract.py`): a regex/keyword pass first
   (deterministic, same style as the email classifier); an optional
   Claude-assisted pass (only if `ANTHROPIC_API_KEY` is set) for messier
   privacy-policy prose the regex pass can't confidently parse. The model is
   used only to *locate* facts already on the page it's given — any
   URL/email it returns is verified verbatim-present in that page's actual
   text before being trusted at all, so it cannot invent one.
4. **Verification** (`DeletionResearchProvider.verify_recipe`): the result's
   source must be the company's own domain, or a third-party privacy portal
   that a domain-verified official page explicitly linked to (that referring
   page is kept as evidence). Anything else is rejected outright, and the
   recipe is marked `NEEDS_RESEARCH` rather than guessed.

**No search/LLM key configured?** Cookie Monster still works — the
same-domain crawl and regex extraction run with zero external services and
zero cost, just with lower coverage. Companies it can't resolve show
"Deletion method not verified yet" with a manual "Research deletion method"
retry, never a fabricated answer.

**Freshness:** a verified recipe is trusted for `DELETION_RECIPE_FRESHNESS_DAYS`
(default 150) before being re-researched; a company research couldn't
verify gets a `DELETION_RECIPE_RETRY_COOLDOWN_DAYS` (default 7) cooldown
before being retried, so a hard-to-verify company isn't re-hit constantly.
A failed *re-check* of an already-verified recipe never destroys the
last-known-good data — it stays usable and is retried at the next cycle.

**Seeds:** `app/deletion_seeds.py` carries over the two entries this project
already had (Lyft, Edikted) as a starting point, loaded once at startup.
They are not the mechanism — everything past them is expected to come from
the research pipeline above, not from hand-editing source code.

## What's really automatic when you click "Delete my data"

What actually happens depends on the method:

- **`EMAIL_REQUEST`**: Cookie Monster always drafts the request. It only
  **sends** it for you if you've completed a *separate*, explicitly-labeled
  OAuth consent for `gmail.send` (Home page → "Enable automatic sending" —
  off by default). Without that, you get the draft to copy/send yourself.
- **`WEB_FORM` / `PRIVACY_PORTAL` / `ACCOUNT_SETTING`**: **not** automated.
  Virtually every real one of these requires login, email verification, or
  a CAPTCHA — automating around that is exactly what this project's own
  safety rules forbid (see Part 10 of the design brief / Security
  considerations below). Cookie Monster deep-links you to the company's
  official page; you complete it, then tell Cookie Monster it's done.
- **`API`**: wired in the state machine for completeness, but no recipe
  currently has a real, documented deletion API, so this path is inert
  today. It will never fabricate one.
- **`UNKNOWN`**: nothing is ever submitted.

**Status is never faked.** `SUBMITTED` is reserved for cases Cookie Monster
has real evidence for (a Gmail message ID *and* thread ID from an actual
send). Completing a web form or emailing a company yourself and telling
Cookie Monster so is recorded as `COMPLETED` and visibly labeled "marked by
you" — that distinction is deliberate, not a bug. Clicking "Delete my data"
again after a request already has an outcome shows a warning instead of
silently resubmitting. Every transition (method found, you confirmed, email
sent, marked complete, failed, …) is also written to an append-only
`deletion_events` audit log (`app/deletion_events.py`), not just the current
status column, so what actually happened stays reconstructable later.

## Current limitations

- Single local user, one Gmail account at a time, one SQLite file.
- Classification is regex/keyword-based, not ML. It will produce false
  positives and false negatives — that's exactly why every company must be
  human-reviewed (confirm/reject/correct) before it means anything.
- Company-name guessing (from the email "From" display name) is a heuristic
  and often needs manual correction.
- Only Gmail is supported (no other providers).
- Scans are capped (default 600 messages) per run to keep runtime and API
  usage bounded; you can re-run scans to pick up more.
- No automated privacy request submission except sending one email, and only
  when you've explicitly opted in via the separate `gmail.send` consent
  step. Nothing is ever submitted via a company's website on your behalf.
- Deletion-method research quality depends on what's configured: with no
  `BRAVE_SEARCH_API_KEY`/`ANTHROPIC_API_KEY` set, only the same-domain
  crawl + regex extraction run — real, but lower coverage than with those
  enabled. Either way, an unverifiable company is reported as such, never
  guessed.
- The background research worker (`app/deletion_queue.py`) is a single
  in-process asyncio loop, not a real task queue — fine for one local user,
  not for anything bigger. See `TODO.md`.
- No company-response tracking yet (reading a company's reply to a sent
  deletion email) — see `TODO.md` for the Gmail scope that would require.

## Setup instructions

### 1. Prerequisites

- Python 3.11+
- A Google account (the one whose Gmail you want to scan)
- A Google Cloud project (free)

### 2. Google Cloud OAuth setup

1. Go to [console.cloud.google.com](https://console.cloud.google.com/) and
   create a new project (or reuse one you control).
2. **APIs & Services → Library**: enable the **Gmail API**.
3. **APIs & Services → OAuth consent screen**:
   - User type: **External** (or Internal if you use Google Workspace).
   - Publishing status: leave it in **Testing** — this is a personal
     prototype, not a public app, so it does not need Google's app
     verification review.
   - Under **Test users**, add your own Gmail address. Only test users can
     complete the OAuth flow while the app is unverified.
   - Scopes: you do not need to add scopes here manually; the app requests
     `gmail.metadata` at runtime.
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type: **Web application**.
   - Authorized redirect URI: `http://localhost:8000/auth/callback`
     (must match `GOOGLE_REDIRECT_URI` in your `.env` exactly).
   - Save the generated **Client ID** and **Client secret**.
5. Because the app is unverified, Google will show an interstitial warning
   ("Google hasn't verified this app") when you connect. This is expected
   for a personal prototype you control — click **Advanced → Go to Cookie
   Monster (unsafe)** to proceed. This warning goes away only if the app is
   published and verified by Google, which is out of scope for this
   prototype.

### 3. Install and configure

```bash
git clone <this repo>
cd cookie-monster
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env:
#   - GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET from step 2 above
#   - COOKIE_MONSTER_SECRET_KEY: generate with
#       python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#   - SESSION_SECRET: any random string
```

### 4. Environment variables

**Required:**

| Variable | Purpose |
|---|---|
| `GOOGLE_CLIENT_ID` | OAuth client ID from Google Cloud Console |
| `GOOGLE_CLIENT_SECRET` | OAuth client secret — keep this out of source control |
| `GOOGLE_REDIRECT_URI` | Must exactly match the redirect URI on the OAuth client |
| `COOKIE_MONSTER_SECRET_KEY` | Fernet key used to encrypt the stored OAuth refresh token |
| `SESSION_SECRET` | Signs the local session cookie used for OAuth CSRF state |
| `DATABASE_PATH` | Where the local SQLite file lives (default `./data/cookie_monster.db`) |
| `APP_HOST` / `APP_PORT` | Local bind address (default `127.0.0.1:8000`) |

**Optional (deletion-method research — see `.env.example` for full descriptions):**

| Variable | Purpose |
|---|---|
| `DELETION_RESEARCH_ENABLED` | Master switch (default `true`). `false` disables all outbound research. |
| `BRAVE_SEARCH_API_KEY` | Enables the Tier B search fallback. Unset = same-domain crawl only. |
| `ANTHROPIC_API_KEY` | Enables LLM-assisted extraction for messy privacy-policy pages. |
| `DELETION_RESEARCH_LLM_MODEL` | Which Claude model to use for extraction (default `claude-haiku-4-5-20251001`). |
| `DELETION_RECIPE_FRESHNESS_DAYS` / `DELETION_RECIPE_RETRY_COOLDOWN_DAYS` | Recipe re-check cadence. |
| `DELETION_QUEUE_INTERVAL_SECONDS` / `DELETION_QUEUE_BATCH_SIZE` | Background worker cadence/batch size. |

`.env` is gitignored. Never commit it.

### 5. Run the application locally

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open http://localhost:8000, click **Connect Gmail with Google**, authorize,
then click **Scan inbox**, then **Open dashboard**.

### 6. Run tests

```bash
pytest
```

Tests cover domain extraction, the evidence classifier, per-company
aggregation, the deletion recipe cache/resolver/engine/queue (including
duplicate-send prevention, that `SUBMITTED` is never set without real
evidence, and that a failed re-check never destroys a good recipe), the
research pipeline (crawl/search/extract/verify, with the HTTP client and
LLM client mocked - no real network or API calls), and the SQLite migration.
They run entirely offline against fixture data and an in-memory/temp-file
database — no Gmail account, search API key, or Anthropic API key required.

## Gmail OAuth scope — exactly what is requested and why

Cookie Monster requests exactly one scope:

```
https://www.googleapis.com/auth/gmail.metadata
```

This is Gmail's most restrictive read scope. It grants access to message
**headers and labels only** (From, Subject, Date, etc.) — Google's API
itself refuses `format=full`/`raw` requests under this scope, so the app is
structurally incapable of reading a message body or attachment, not just
policy-restricted from it.

**Never requested by the connect/scan flow:** `gmail.readonly` (body
access), `gmail.send`, `gmail.modify`, `gmail.labels`, any
Contacts/Drive/Calendar scope, or your Google password (Google's login page
handles authentication entirely; this app never sees your credentials).

**Never done:** deleting mail, labeling/archiving mail, modifying anything
in your inbox.

### Optional second scope: `gmail.send`

If you want Cookie Monster to actually send deletion-request emails for you
(instead of just drafting them), there's a **separate** opt-in on the home
page ("Enable automatic sending") that requests one more scope:

```
https://www.googleapis.com/auth/gmail.send
```

This is send-only — it cannot read, delete, or modify anything already in
your inbox. It is never requested as part of connecting/scanning Gmail,
never silently combined with `gmail.metadata`, and every email it sends
still requires you to click "Continue with deletion" for that specific
company first. Declining this just means you copy/send the drafted request
yourself, which is the default.

### Not requested (yet): `gmail.readonly`

A future version could track a company's *reply* to a sent deletion email
(acknowledged / needs verification / done / rejected). Gmail's OAuth model
has no scope that limits reads to specific threads or labels - that
filtering can only happen in application code after a broad-enough scope is
granted, and `gmail.metadata` cannot return body text at all, so reading a
reply's content genuinely needs `gmail.readonly` (Google's narrowest scope
that can). That's a materially bigger permission than anything above, so
it's deliberately **not implemented** in this version - see `TODO.md`.

## How Gmail data is processed

1. A scan paginates message IDs from your mailbox, up to the message cap you
   set (default 600).
2. For each message ID, the app fetches **`format=metadata`** with
   an explicit header allowlist (`From`, `Subject`, `Date`) — never the
   message body.
3. The classifier runs regex/keyword rules against the subject line and
   sender domain only.
4. Matches are aggregated in memory into one summary row per company
   (domain).
5. Only the aggregate is written to SQLite. Message IDs, raw headers, and
   full subject-line history are discarded once the scan finishes.

## What data is stored (and what is not)

**Persisted in the local SQLite database:**

```
company name, domain, relationship_type, evidence_type(s),
first_seen, last_seen, evidence_count, confidence,
up to 3 example subject lines (truncated to 120 chars),
up to 5 human-readable detection reasons,
review status (pending/confirmed/rejected)
```

Plus, per company, deletion-request tracking:

```
deletion_method, deletion_action_capability, deletion_status,
deletion_url, deletion_email, deletion_instructions,
deletion_verified, deletion_source_url, deletion_last_checked,
deletion_requested_at, deletion_completed_at, deletion_error,
deletion_thread_id (Gmail thread ID, only for a sent email request -
  unused until a future response-tracking phase),
deletion_evidence (JSON - message IDs, timestamps, confirmation
references, or a short user-typed note; never credentials, tokens,
or third-party account data)
```

Plus two shared tables, independent of any one company:

```
deletion_recipes - the reusable "how does this domain handle deletion"
  cache: domain, method, url, email, login/verification flags,
  known consequences, source_url, confidence, status, origin,
  recipe_version, verified_at, expires_at, research_attempts

deletion_events - an append-only audit log of what actually happened
  per company (method discovered, you confirmed, email sent, marked
  complete, failed, ...), each with a timestamp and safe evidence -
  never just the current status column
```

Plus one encrypted OAuth refresh token (Fernet-encrypted with
`COOKIE_MONSTER_SECRET_KEY`), so you don't have to re-authenticate every
run. If you've enabled automatic sending, the same token row also records
that the `gmail.send` scope was granted (needed to know whether to send or
just draft) — no separate secret is stored for it.

**Never fetched, never stored:** message bodies, snippets, attachments,
full header sets, message IDs (beyond the duration of one scan, in memory
only), sender email addresses, names, order numbers, shipping addresses,
payment details, third-party account passwords, or any other content
beyond the Subject/From/Date headers of matched messages. Deletion-method
research works the same way: fetched web pages (HTML, full text) are held
in memory only for the duration of one research attempt and discarded -
only the extracted, verified facts (method/url/email/etc.) land in
`deletion_recipes`.

### Delete all imported data

The dashboard has a **"Delete all imported data"** button
(`POST /api/delete-all`) that wipes the `companies` table (and, with it,
that company's deletion request history) entirely. It does not affect your
Gmail connection — use **Disconnect Gmail** (`POST /auth/disconnect`)
separately to revoke and delete the stored OAuth token (this also attempts
to revoke the token at Google directly). It also does not clear
`deletion_recipes` - that cache is deliberately independent of any one
company/user (see "How deletion-method research works" above); delete the
database file entirely (below) if you want a completely clean slate.

To wipe everything including the database file itself:

```bash
rm -f data/cookie_monster.db
```

## Security considerations

- The OAuth refresh token is the only real secret this app persists; it's
  encrypted at rest and the encryption key lives only in your local `.env`.
- This app is designed to run on `127.0.0.1` for a single local user. It has
  no authentication of its own (no login screen) — do not expose it on a
  network or bind it to `0.0.0.0` without adding that.
- `data/` and `.env` are gitignored; double-check `git status` before
  committing if you fork/modify this.
- `tldextract` is configured with `suffix_list_urls=()` so it never makes a
  network call of its own. This app's outbound network traffic is: Google's
  OAuth/Gmail API endpoints, and (only if `DELETION_RESEARCH_ENABLED=true`,
  the default) public pages on companies' own domains during deletion-method
  research, plus Brave Search / Anthropic's API if you've configured those
  keys. Set `DELETION_RESEARCH_ENABLED=false` to disable that third category
  entirely.
- The research crawler (`app/research_fetch.py`) only fetches public,
  unauthenticated pages, respects `robots.txt`, and identifies itself with
  an honest User-Agent - it never logs in, never touches an authenticated
  page, and times out/backs off rather than hammering a site.
- The classifier can misclassify senders. Nothing is auto-confirmed;
  everything requires explicit human review in the dashboard.
- This prototype does not implement multi-user isolation, rate limiting, or
  production-grade secret storage (e.g. a KMS) — see `TODO.md` for what a
  real product would need.
- Schema changes to an existing database run through `app/migrations.py`,
  which is additive-only (never drops/renames a column) and always copies
  the `.db` file aside first (`data/cookie_monster.db.bak-<timestamp>`)
  before changing anything in an existing table.
- Cookie Monster never automates a third-party company's login, CAPTCHA, or
  web form. The only action it can take on your behalf without you doing it
  yourself afterward is sending one email, and only after the separate
  `gmail.send` opt-in plus a per-company confirmation click.
