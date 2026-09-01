"""Curated, human-reviewed deletion-method registry.

This is deliberately NOT a live web scraper. Per the project's safety rules
(never invent a privacy URL/email, never trust an unverified source), every
entry here must have been confirmed by a human against the company's own
published privacy materials before it's added - `source_url` should point
at the actual page that was read.

It ships with only the two entries this project already had (see below) -
nothing else was added, because this environment's outbound network access
is restricted, so no other company's page could be fetched and verified
while building this feature. Populate more by visiting a company's real
privacy/data-rights page yourself (or its Data Rights / "Do Not Sell or
Share" / CCPA/CPRA page) and adding a `DeletionProvider` entry below with
that page's exact URL as `source_url`. Never copy a URL from a blog, forum,
or search snippet - only from the company's own site.

deletion_resolver.py checks this registry first (and only this registry -
it does not fall back to guessing) when enriching a newly discovered
company.
"""
from dataclasses import dataclass

from app.classifier import normalize_domain
from app.deletion_constants import ActionCapability, DeletionMethod


@dataclass(frozen=True)
class DeletionProvider:
    domain: str  # normalized registrable domain, e.g. "lyft.com"
    method: str  # DeletionMethod.*
    automation: str  # ActionCapability.*
    source_url: str  # the official page a human confirmed this against
    url: str | None = None  # deletion/privacy-request page or account-settings page, if applicable
    email: str | None = None  # official privacy-request email address, if that's the mechanism
    instructions: str | None = None  # short human-readable summary of the process
    consequences: str | None = None  # what the user stands to lose (account, history, rewards, etc.)


# Keyed by normalized registrable domain.
#
# These two entries were carried over from this app's previous version, which
# hardcoded them directly into the dashboard template. Moving them here is
# what makes them (and every company after them) go through the same
# resolver/engine as everyone else instead of a one-off template conditional.
# Their URLs and warning text are unchanged from before. This environment's
# outbound network access is restricted, so these could not be independently
# re-verified while making this change - re-confirm against the live pages
# occasionally, since privacy-request URLs do change.
PROVIDER_REGISTRY: dict[str, DeletionProvider] = {
    "lyft.com": DeletionProvider(
        domain="lyft.com",
        method=DeletionMethod.ACCOUNT_SETTING,
        automation=ActionCapability.USER_ACTION_REQUIRED,
        source_url="https://account.lyft.com/privacy/data/delete",
        url="https://account.lyft.com/privacy/data/delete",
        instructions="Sign in to your Lyft account and submit the deletion request from Lyft's own data-deletion page.",
        consequences="Lyft states this process deletes your Lyft account. Once completed, it cannot be undone.",
    ),
    "edikted.com": DeletionProvider(
        domain="edikted.com",
        method=DeletionMethod.PRIVACY_PORTAL,
        automation=ActionCapability.USER_ACTION_REQUIRED,
        source_url="https://edikted.com/pages/ccpa-compliance",
        url="https://edikted.com/pages/ccpa-compliance",
        instructions="Submit your deletion request through Edikted's CCPA compliance page.",
        consequences="Edikted states that requesting deletion will also delete your Edikted account.",
    ),
}


def get_provider(domain: str) -> DeletionProvider | None:
    normalized = normalize_domain(domain)
    if normalized is None:
        return None
    return PROVIDER_REGISTRY.get(normalized)


def register_provider(provider: DeletionProvider) -> None:
    """Adds/replaces a registry entry, keyed by its own normalized domain.
    Exists mainly so tests (and any future admin tooling) don't have to poke
    the module-level dict directly."""
    PROVIDER_REGISTRY[normalize_domain(provider.domain) or provider.domain] = provider
