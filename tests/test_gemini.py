from app.gemini import GeminiJudge
from app.config import ModelRoles, RunConfig
from core.models import CandidateProfile, MatchResult, SearchPreferences, Vacancy, VacancyCard
from core.resumes import Resume


def test_the_judge_reads_the_resume_it_is_given(tmp_path, monkeypatch):
    seen = {}

    def fake_judge_match(vacancy_raw, resume_text, prefs_summary, model, max_attempts):
        seen["resume_text"] = resume_text
        return MatchResult(score=70)

    monkeypatch.setattr("app.gemini.judge_match", fake_judge_match)
    config = RunConfig(
        resume_paths=(tmp_path / "a.pdf",), state_path=tmp_path / "s.db", report_dir=tmp_path,
        list_url="x", start_page=1, stop_page=1, call_budget=1, min_score=0, final_judge_limit=1,
        preferences=SearchPreferences(desired_roles=[]), models=ModelRoles(),
    )
    judge = GeminiJudge(config, "profile", "prefs")
    resume = Resume("b.pdf", "analyst resume text", CandidateProfile())

    judge.final_judge(Vacancy(raw_text="posting"), VacancyCard(offer_id="O1", title="t", url="u"), resume)

    assert seen["resume_text"] == "analyst resume text"
