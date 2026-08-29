# TODO — future versions

This file tracks work explicitly **out of scope** for the current
milestone (Gmail OAuth → scan → classify → dashboard → confirm). Nothing
here should be started until the current milestone has been used on real
data and judged useful.

## Milestone 2 — Privacy research (not started)

- For each **confirmed** company, look up publicly available privacy
  information: privacy policy URL, CCPA/CPRA deletion rights, right to
  know/access, correction rights, sale/sharing opt-out, sensitive personal
  information handling, privacy request portal/email.
- Store: source URL, date researched, request type, request method,
  eligibility restrictions, whether identity verification appears required.
- UI must clearly separate **verified** (quoted/sourced from the company's
  own published materials) from **AI-interpreted/suggested** information.
  Do not blindly trust AI-generated conclusions about legal rights.
- Needs a research/caching layer so the same company isn't re-researched on
  every page load, with a visible "last researched" date and a manual
  re-check action.

## Milestone 3 — Request generation (not started)

- For a confirmed company, let the user choose a request type (delete,
  access/know, correct, opt-out of sale/sharing, other).
- Generate a draft request using the company's own published process.
- Provide **[Copy Request]** and, if a real privacy request URL was found
  in Milestone 2, **[Open Company's Privacy Request Page]**.
- Explicitly **never** auto-submit. This stays a hard rule for the
  foreseeable future, not just this prototype phase.

## Longer-term product ideas (explicitly deferred)

- Multi-user accounts, auth, and per-user data isolation.
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
- Structured, versioned migrations for the SQLite schema (currently just
  `create_all` — fine for a prototype, not for real data over time).

## Explicitly not planned (guardrails, not gaps)

- No billing, no ads, no data monetization, no CAPTCHA bypass, no
  automated privacy-request submission without explicit per-request user
  approval, no scraping of authenticated pages without explicit user
  authorization, no impersonating the user, no circumventing login
  requirements.
