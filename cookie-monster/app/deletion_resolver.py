"""Enriches a Company with its deletion method - registry lookup only, no
network calls, so this is safe to run synchronously right after every scan
without slowing it down (see aggregator.py) or re-running it on every
dashboard load (see the caching check below).
"""
import datetime

from app.deletion_constants import ActionCapability, DeletionMethod, DeletionStatus
from app.deletion_registry import get_provider
from app.models import Company

_RESOLVABLE_STATUSES = {DeletionStatus.NOT_STARTED, DeletionStatus.METHOD_LOOKUP, DeletionStatus.UNKNOWN}


def resolve_deletion_method(company: Company, force: bool = False) -> bool:
    """Looks up company.domain in the verified provider registry and fills in
    the company's deletion_* fields. Returns True if anything changed.

    Caching: once a company has been checked (deletion_last_checked is set)
    and has a known method, this is a no-op unless force=True - so re-running
    a scan, or reloading the dashboard, never re-researches an already-known
    company. An UNKNOWN result also counts as "checked" (we don't silently
    retry every load), but the user can force a re-check via the dashboard's
    "Research deletion method" action once the registry has been updated.
    """
    already_resolved = company.deletion_last_checked is not None
    if already_resolved and not force:
        return False

    provider = get_provider(company.domain)
    now = datetime.datetime.utcnow()

    if provider is None:
        company.deletion_method = DeletionMethod.UNKNOWN
        company.deletion_action_capability = ActionCapability.UNKNOWN
        company.deletion_verified = False
        company.deletion_last_checked = now
        if company.deletion_status in _RESOLVABLE_STATUSES:
            company.deletion_status = DeletionStatus.UNKNOWN
        return True

    company.deletion_method = provider.method
    company.deletion_action_capability = provider.automation
    company.deletion_url = provider.url
    company.deletion_email = provider.email
    # There's no separate "consequences" column (see models.py) - fold it into
    # the instructions text, since it's practically part of "what happens if
    # you do this", which is exactly what the dashboard's confirmation modal
    # needs to show before anything irreversible happens.
    company.deletion_instructions = " ".join(
        part for part in (provider.instructions, provider.consequences) if part
    ) or None
    company.deletion_source_url = provider.source_url
    company.deletion_verified = True
    company.deletion_last_checked = now
    if company.deletion_status in _RESOLVABLE_STATUSES:
        company.deletion_status = DeletionStatus.READY
    return True


def resolve_many(companies: list[Company], force: bool = False) -> int:
    """Runs resolve_deletion_method over a batch (e.g. everything a scan just
    touched). Returns how many companies actually changed."""
    return sum(1 for c in companies if resolve_deletion_method(c, force=force))
