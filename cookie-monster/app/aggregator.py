"""Collapses a stream of per-message Classifications into one row per
company - the only thing that gets written to SQLite. Message-level detail
(subject text beyond 3 examples, message IDs, headers) is discarded here.
"""
import datetime
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.classifier import Classification
from app.deletion_resolver import enqueue_pending
from app.models import Company

STRONG_EVIDENCE = {
    "order_confirmation", "receipt", "shipping_confirmation", "account_creation",
    "password_reset", "account_verification", "subscription_confirmation", "customer_service",
}
MAX_EXAMPLES = 3
MAX_REASONS = 5
SUBJECT_TRUNCATE_LEN = 120


@dataclass
class _DomainAgg:
    domain: str
    name: str = ""
    evidence_counter: Counter = field(default_factory=Counter)
    relationship_counter: Counter = field(default_factory=Counter)
    example_subjects: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    first_seen: datetime.datetime | None = None
    last_seen: datetime.datetime | None = None


def _score_confidence_from(total: int, evidence_types: set) -> str:
    if total >= 3 or len(evidence_types) >= 2:
        return "high"
    if evidence_types & STRONG_EVIDENCE:
        return "medium"
    return "low"


def _score_confidence(agg: _DomainAgg) -> str:
    return _score_confidence_from(sum(agg.evidence_counter.values()), set(agg.evidence_counter))


def _dominant_relationship(agg: _DomainAgg) -> str:
    if not agg.relationship_counter:
        return "account"
    most_common = agg.relationship_counter.most_common()
    top_count = most_common[0][1]
    tied = [rel for rel, count in most_common if count == top_count]
    return tied[0] if len(tied) == 1 else "mixed"


def aggregate(stream: Iterator[tuple[Classification, datetime.datetime]]) -> dict[str, _DomainAgg]:
    result: dict[str, _DomainAgg] = {}

    for classification, msg_date in stream:
        agg = result.get(classification.domain)
        if agg is None:
            agg = _DomainAgg(domain=classification.domain, name=classification.company_name)
            result[classification.domain] = agg

        agg.evidence_counter[classification.evidence_type] += 1
        agg.relationship_counter[classification.relationship_type] += 1

        subject = classification.subject.strip()[:SUBJECT_TRUNCATE_LEN]
        if subject and subject not in agg.example_subjects and len(agg.example_subjects) < MAX_EXAMPLES:
            agg.example_subjects.append(subject)

        for reason in classification.reasons:
            if reason not in agg.reasons and len(agg.reasons) < MAX_REASONS:
                agg.reasons.append(reason)

        if agg.first_seen is None or msg_date < agg.first_seen:
            agg.first_seen = msg_date
        if agg.last_seen is None or msg_date > agg.last_seen:
            agg.last_seen = msg_date

    return result


def store(db: Session, aggregated: dict[str, _DomainAgg]) -> dict[str, int]:
    """Upserts aggregated per-domain evidence into the companies table.
    Never overwrites a company the user has already confirmed/rejected/edited."""
    created, updated = 0, 0
    touched_companies: list[Company] = []
    for domain, agg in aggregated.items():
        existing = db.query(Company).filter(Company.domain == domain).one_or_none()
        total_count = sum(agg.evidence_counter.values())
        confidence = _score_confidence(agg)
        relationship_type = _dominant_relationship(agg)

        if existing is None:
            new_company = Company(
                name=agg.name,
                domain=domain,
                relationship_type=relationship_type,
                status="pending",
                confidence=confidence,
                evidence_count=total_count,
                evidence_types=sorted(agg.evidence_counter),
                example_subjects=agg.example_subjects,
                detection_reasons=agg.reasons,
                first_seen=agg.first_seen,
                last_seen=agg.last_seen,
            )
            db.add(new_company)
            touched_companies.append(new_company)
            created += 1
        else:
            existing.evidence_count += total_count
            existing.evidence_types = sorted(set(existing.evidence_types) | set(agg.evidence_counter))
            merged_examples = existing.example_subjects + [
                s for s in agg.example_subjects if s not in existing.example_subjects
            ]
            existing.example_subjects = merged_examples[:MAX_EXAMPLES]
            merged_reasons = existing.detection_reasons + [
                r for r in agg.reasons if r not in existing.detection_reasons
            ]
            existing.detection_reasons = merged_reasons[:MAX_REASONS]
            existing.first_seen = min(existing.first_seen, agg.first_seen)
            existing.last_seen = max(existing.last_seen, agg.last_seen)
            # Recompute confidence from the cumulative (not just this scan's) evidence,
            # but leave relationship_type/name/status alone if the user already corrected them.
            if not existing.user_corrected:
                existing.confidence = _score_confidence_from(existing.evidence_count, set(existing.evidence_types))
                existing.relationship_type = relationship_type
            touched_companies.append(existing)
            updated += 1

    # DB-only, no network calls - safe to run inline without slowing the scan
    # down. This only applies whatever DeletionRecipe currently exists
    # (possibly a bare not-yet-researched stub); actual research happens in
    # the background - see deletion_queue.py / app.main's startup hook.
    enqueue_pending(db, touched_companies)

    db.commit()
    return {"created": created, "updated": updated}
