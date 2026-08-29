import datetime

from pydantic import BaseModel, ConfigDict


class CompanyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    domain: str
    relationship_type: str
    status: str
    confidence: str
    evidence_count: int
    evidence_types: list[str]
    example_subjects: list[str]
    detection_reasons: list[str]
    first_seen: datetime.datetime
    last_seen: datetime.datetime
    user_corrected: bool


class CorrectionIn(BaseModel):
    name: str | None = None
    relationship_type: str | None = None


class MergeIn(BaseModel):
    keep_id: int
    merge_id: int


class ScanSummary(BaseModel):
    messages_matched: int
    companies_created: int
    companies_updated: int
