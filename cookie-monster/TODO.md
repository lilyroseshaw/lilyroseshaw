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

## Milestone 3 — Request generation (deletion request done; others not started)

Deletion requests now work end-to-end for the methods a curated registry
entry can support (`app/deletion_registry.py`, `deletion_resolver.py`,
`deletion_engine.py`) - see README "Deletion requests - what's really
automatic". Still open:

- Request types beyond deletion (access/know, correct, opt-out of
  sale/sharing) - only deletion is wired up today.
- Growing the deletion registry past its current two entries. Each new
  entry needs a human to actually visit the company's real privacy page and
  confirm the method/URL/email - do not batch-generate entries from search
  results or an LLM's general knowledge; that's exactly the "guessed URL"
  failure mode this project rules out. A lightweight review script/checklist
  for adding entries would help once there's real usage data on which
  companies matter most.
- `DeletionStatus.VERIFICATION_NEEDED` exists in the state machine but
  nothing sets it yet - there's no way for Cookie Monster to know a company
  is asking for ID verification unless the user tells it (which today just
  goes through the same self-report "Mark as completed" flow). Detecting
  this would need reading a reply from the company, which is out of scope
  while the Gmail scope stays metadata-only.
- `DeletionMethod.API` is fully wired in the engine but no registry entry
  has a real API yet, so it's currently inert.

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
