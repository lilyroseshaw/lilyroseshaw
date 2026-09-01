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
6. **Resolve a deletion method** for each company automatically, from a
   small human-curated registry (no live web research, no guessing — see
   "Deletion requests" below).
7. **Delete my data**: one button per confirmed company. What it actually
   does depends on the company's verified method — see "Deletion requests -
   what's really automatic" below. Nothing is ever submitted without you
   confirming it first, and nothing is marked "submitted" without real
   evidence that it happened.

Privacy-policy *research beyond the curated registry* and general request
generation for methods other than email are **not built yet** — see `TODO.md`.

## Deletion requests — what's really automatic

Every confirmed company gets a deletion method resolved automatically from
`app/deletion_registry.py` — a small, human-curated list (not a live web
scraper; see that file's docstring for why). If a company isn't in it yet,
the dashboard shows "Deletion method not verified yet" and a "Research
deletion method" button rather than pretending to know.

What actually happens when you click **Delete my data** depends on the
method:

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
- **`API`**: wired in the state machine for completeness, but no registry
  entry currently has a real, documented deletion API, so this path is
  inert today. It will never fabricate one.
- **`UNKNOWN`**: nothing is ever submitted.

**Status is never faked.** `SUBMITTED` is reserved for cases Cookie Monster
has real evidence for (a Gmail message ID from an actual send). Completing
a web form or emailing a company yourself and telling Cookie Monster so is
recorded as `COMPLETED` and visibly labeled "marked by you" — that
distinction is deliberate, not a bug. Clicking "Delete my data" again after
a request already has an outcome shows a warning instead of silently
resubmitting.

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
- The deletion registry ships with exactly two entries (Lyft, Edikted,
  carried over from this app's previous hardcoded version) plus whatever
  you add — see `app/deletion_registry.py` for how to add more responsibly.

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

### 4. Required environment variables

| Variable | Purpose |
|---|---|
| `GOOGLE_CLIENT_ID` | OAuth client ID from Google Cloud Console |
| `GOOGLE_CLIENT_SECRET` | OAuth client secret — keep this out of source control |
| `GOOGLE_REDIRECT_URI` | Must exactly match the redirect URI on the OAuth client |
| `COOKIE_MONSTER_SECRET_KEY` | Fernet key used to encrypt the stored OAuth refresh token |
| `SESSION_SECRET` | Signs the local session cookie used for OAuth CSRF state |
| `DATABASE_PATH` | Where the local SQLite file lives (default `./data/cookie_monster.db`) |
| `APP_HOST` / `APP_PORT` | Local bind address (default `127.0.0.1:8000`) |

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
aggregation, the deletion registry/resolver/engine (including duplicate-send
prevention and that `SUBMITTED` is never set without real evidence), and the
SQLite migration. They run entirely offline against fixture data and an
in-memory/temp-file database — no Gmail account or network access required.

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

## How Gmail data is processed

1. A scan runs a handful of targeted Gmail search queries (by subject
   keywords, e.g. "welcome", "order", "receipt", "password reset") to avoid
   pulling the entire mailbox.
2. For each matching message ID, the app fetches **`format=metadata`** with
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
deletion_evidence (JSON - message IDs, timestamps, confirmation
references, or a short user-typed note; never credentials, tokens,
or third-party account data)
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
beyond the Subject/From/Date headers of matched messages.

### Delete all imported data

The dashboard has a **"Delete all imported data"** button
(`POST /api/delete-all`) that wipes the `companies` table entirely. It does
not affect your Gmail connection — use **Disconnect Gmail**
(`POST /auth/disconnect`) separately to revoke and delete the stored OAuth
token (this also attempts to revoke the token at Google directly).

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
  network call of its own — this app's only outbound network traffic is to
  Google's OAuth/Gmail API endpoints.
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
