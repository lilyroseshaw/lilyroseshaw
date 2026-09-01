import datetime

from app.deletion_constants import ActionCapability, DeletionMethod, DeletionStatus
from app.deletion_registry import DeletionProvider, PROVIDER_REGISTRY, register_provider
from app.deletion_resolver import resolve_deletion_method
from app.models import Company


def _company(domain: str, **overrides) -> Company:
    defaults = dict(
        name="Test Co",
        domain=domain,
        relationship_type="transactional",
        status="confirmed",
        confidence="high",
        evidence_count=1,
        evidence_types=[],
        example_subjects=[],
        detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1),
        last_seen=datetime.datetime(2022, 1, 1),
        deletion_method=DeletionMethod.UNKNOWN,
        deletion_action_capability=ActionCapability.UNKNOWN,
        deletion_status=DeletionStatus.NOT_STARTED,
        deletion_verified=False,
    )
    defaults.update(overrides)
    return Company(**defaults)


def test_unresolved_domain_marks_unknown_and_unverified():
    company = _company("not-a-real-registered-company.com")
    changed = resolve_deletion_method(company)
    assert changed is True
    assert company.deletion_method == DeletionMethod.UNKNOWN
    assert company.deletion_verified is False
    assert company.deletion_status == DeletionStatus.UNKNOWN
    assert company.deletion_last_checked is not None


def test_registry_hit_fills_in_verified_fields():
    register_provider(
        DeletionProvider(
            domain="resolvertestco.com",
            method=DeletionMethod.EMAIL_REQUEST,
            automation=ActionCapability.PARTIALLY_AUTOMATABLE,
            source_url="https://resolvertestco.com/privacy",
            email="privacy@resolvertestco.com",
            instructions="Email the privacy team.",
        )
    )
    try:
        company = _company("resolvertestco.com")
        resolve_deletion_method(company)
        assert company.deletion_method == DeletionMethod.EMAIL_REQUEST
        assert company.deletion_verified is True
        assert company.deletion_email == "privacy@resolvertestco.com"
        assert company.deletion_source_url == "https://resolvertestco.com/privacy"
        assert company.deletion_status == DeletionStatus.READY
    finally:
        PROVIDER_REGISTRY.pop("resolvertestco.com", None)


def test_already_resolved_company_is_not_rechecked():
    company = _company("cachetestco.com")
    resolve_deletion_method(company)
    checked_at = company.deletion_last_checked
    assert checked_at is not None

    register_provider(
        DeletionProvider(
            domain="cachetestco.com",
            method=DeletionMethod.WEB_FORM,
            automation=ActionCapability.USER_ACTION_REQUIRED,
            source_url="https://cachetestco.com/privacy",
        )
    )
    try:
        changed = resolve_deletion_method(company)  # no force
        assert changed is False
        assert company.deletion_method == DeletionMethod.UNKNOWN  # unchanged - cache hit, not re-researched
        assert company.deletion_last_checked == checked_at
    finally:
        PROVIDER_REGISTRY.pop("cachetestco.com", None)


def test_force_rechecks_even_if_cached():
    company = _company("forcetestco.com")
    resolve_deletion_method(company)

    register_provider(
        DeletionProvider(
            domain="forcetestco.com",
            method=DeletionMethod.ACCOUNT_SETTING,
            automation=ActionCapability.USER_ACTION_REQUIRED,
            source_url="https://forcetestco.com/account",
        )
    )
    try:
        changed = resolve_deletion_method(company, force=True)
        assert changed is True
        assert company.deletion_method == DeletionMethod.ACCOUNT_SETTING
    finally:
        PROVIDER_REGISTRY.pop("forcetestco.com", None)


def test_subdomain_resolves_to_root_domain_registry_entry():
    company = _company("mail.notifications.lyft.com")
    resolve_deletion_method(company)
    assert company.deletion_method == DeletionMethod.ACCOUNT_SETTING
    assert company.deletion_verified is True
