from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class WorkFormat(str, Enum):
    REMOTE = "remote"
    ONSITE = "onsite"
    HYBRID = "hybrid"


class LanguageSkill(BaseModel):
    language_code: str | None = Field(None, description="ISO 639-1 code, e.g., 'en' for English, 'sk' for Slovak, 'uk' for Ukrainian, etc.")
    level: str | None = Field(None, description="Certificate level if specified, e.g., 'A1', 'A2', 'B1', 'B2', 'C1', 'C2', 'Native'.")


class ExperienceEntry(BaseModel):
    company: str | None = None
    role: str | None = None
    years: float | None = Field(None, description="Number of years of experience in this role.")
    highlights: list[str] = Field(default_factory=list, description="List of key achievements or responsibilities in this role.")


class Education(BaseModel):
    degree: str | None = None
    field: str | None = None
    institution: str | None = None


class CandidateProfile(BaseModel):
    full_name: str | None = None
    title: str | None = None
    location: str | None = None
    years_experience: float | None = None
    seniority: str | None = None
    tech_stack: list[str] = Field(default_factory=list)
    languages: list[LanguageSkill] = Field(default_factory=list)
    experience: list[ExperienceEntry] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    summary: str | None = None
    additional_info: str | None = Field(None, description="Any additional information that doesn't fit into the other fields.")


class SearchPreferences(BaseModel):
    desired_roles: list[str]
    min_salary: float | None = None
    work_formats: list[WorkFormat] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    must_have: list[str] = Field(default_factory=list)
    deal_breakers: list[str] = Field(default_factory=list)

    # Card-level filtering. Pay floors are per period because listings mix
    # hourly student rates with monthly salaries; one number cannot judge both.
    min_salary_month: float | None = None
    min_salary_hour: float | None = None
    require_title_keywords: list[str] = Field(
        default_factory=list,
        description="If set, a title must contain one of these. Off by default: a "
        "weak match only scores low, while a wrongly dropped one is never seen.",
    )


class VacancyCard(BaseModel):
    """What a search-listing row shows, before the posting itself is fetched.

    Enough to reject a vacancy without spending a page load or an LLM call:
    Profesia states salary on every card, since Slovak law requires it.
    """

    offer_id: str
    title: str
    url: str
    company: str | None = None
    location: str | None = None
    salary_text: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_period: str | None = Field(None, description="'hour' or 'month'; the two are not comparable.")
    allows_home_office: bool = False
    posted_label: str | None = Field(None, description="Freshness as shown, e.g. 'Včera'.")


class Vacancy(BaseModel):
    company: str | None = None
    role: str | None = None
    seniority: str | None = None
    location: str | None = None
    work_format: WorkFormat | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    tech_stack: list[str] = Field(default_factory=list)
    languages: list[LanguageSkill] = Field(default_factory=list)
    posting_language: str | None = Field(None, description="ISO 639-1 code of the language in which the vacancy is posted, e.g., 'en' for English, 'sk' for Slovak, 'uk' for Ukrainian, etc.")
    additional_info: str | None = Field(None, description="Any additional information that doesn't fit into the other fields.")
    apply_url: str | None = None
    source_url: str | None = None
    raw_text: str = ""


class MatchResult(BaseModel):
    score: int = Field(ge=0, le=100)
    reasons: list[str] = Field(default_factory=list, description="Reasons in English.")
    reasons_ru: list[str] = Field(
        default_factory=list,
        description="The same reasons in Russian. Produced in the same call as the "
        "English ones, so a second report costs output tokens rather than another "
        "request against the judge's small daily quota.",
    )


class Attachment(BaseModel):
    filename: str
    path: str
    mime_type: str = "application/octet-stream"


class IncomingMessage(BaseModel):
    sender: str
    subject: str = ""
    body: str = ""
    attachments: list[Attachment] = Field(default_factory=list)


