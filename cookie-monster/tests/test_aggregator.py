import datetime

from app.aggregator import aggregate
from app.classifier import Classification


def _c(domain, evidence_type, relationship_type, subject="Subject"):
    return Classification(
        domain=domain,
        company_name=domain.split(".")[0].capitalize(),
        evidence_type=evidence_type,
        relationship_type=relationship_type,
        reasons=[f"matched {evidence_type}"],
        subject=subject,
    )


def test_single_weak_evidence_is_low_confidence():
    stream = [(_c("smallshop.com", "marketing_newsletter", "marketing"), datetime.datetime(2022, 1, 1))]
    result = aggregate(iter(stream))
    agg = result["smallshop.com"]
    assert agg.domain == "smallshop.com"
    assert sum(agg.evidence_counter.values()) == 1


def test_multiple_evidence_types_boost_confidence():
    stream = [
        (_c("amazon.com", "order_confirmation", "transactional", "Order #1"), datetime.datetime(2021, 1, 1)),
        (_c("amazon.com", "shipping_confirmation", "transactional", "Shipped!"), datetime.datetime(2021, 1, 3)),
        (_c("amazon.com", "receipt", "transactional", "Your receipt"), datetime.datetime(2026, 6, 1)),
    ]
    result = aggregate(iter(stream))
    agg = result["amazon.com"]
    assert sum(agg.evidence_counter.values()) == 3
    assert agg.first_seen == datetime.datetime(2021, 1, 1)
    assert agg.last_seen == datetime.datetime(2026, 6, 1)
    assert len(agg.example_subjects) == 3


def test_example_subjects_capped_at_three():
    stream = [
        (_c("shop.com", "order_confirmation", "transactional", f"Order #{i}"), datetime.datetime(2022, 1, i + 1))
        for i in range(5)
    ]
    result = aggregate(iter(stream))
    assert len(result["shop.com"].example_subjects) == 3


def test_distinct_domains_stay_separate():
    stream = [
        (_c("amazon.com", "order_confirmation", "transactional"), datetime.datetime(2022, 1, 1)),
        (_c("netflix.com", "subscription_confirmation", "subscription"), datetime.datetime(2022, 1, 1)),
    ]
    result = aggregate(iter(stream))
    assert set(result.keys()) == {"amazon.com", "netflix.com"}
