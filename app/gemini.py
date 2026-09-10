"""The model-backed steps, bound to the Gemini connector.

Thin on purpose: the pipeline holds the policy, this holds the wiring, and the
tests exercise the policy without either.
"""

from app.config import RunConfig
from core.models import MatchResult, Vacancy, VacancyCard
from local_connectors.llm import extract, judge_match, score_match

VACANCY_INSTRUCTION = (
    "Extract the vacancy's structured data from the posting text. "
    "Record the language it is written in as an ISO 639-1 code in posting_language "
    "('sk', 'en', ...). Leave raw_text empty; the caller fills it. "
    "Use None or an empty list for anything the posting does not state."
)


class GeminiJudge:
    def __init__(self, config: RunConfig, profile_summary: str, prefs_summary: str, resume_text: str) -> None:
        self._config = config
        self._profile_summary = profile_summary
        self._prefs_summary = prefs_summary
        self._resume_text = resume_text

    def extract_vacancy(self, text: str) -> Vacancy:
        return extract(text, Vacancy, VACANCY_INSTRUCTION, model=self._config.models.extract)

    def rough_score(self, vacancy: Vacancy, card: VacancyCard) -> MatchResult:
        return score_match(
            vacancy.raw_text or card.title,
            self._profile_summary,
            self._prefs_summary,
            model=self._config.models.rough_score,
        )

    def final_judge(self, vacancy: Vacancy, card: VacancyCard) -> MatchResult:
        return judge_match(
            vacancy.raw_text or card.title,
            self._resume_text,
            self._prefs_summary,
            model=self._config.models.final_judge,
        )
