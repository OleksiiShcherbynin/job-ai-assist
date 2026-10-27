"""Two resumes for one person: the profile they add up to, and which one a
finalist is judged against."""

from core.models import CandidateProfile, LanguageSkill, Vacancy
from core.resumes import Resume, merge_profiles, pick_resume

SWE = CandidateProfile(
    title="Junior Software Engineer",
    seniority="junior",
    location="Bratislava",
    tech_stack=["Python", "Java", "Docker", "SQL"],
    languages=[LanguageSkill(language_code="en", level="B2")],
    summary="Builds backend services.",
)
ANALYST = CandidateProfile(
    title="Junior Data Analyst",
    seniority="junior",
    tech_stack=["Python", "SQL", "pandas", "Power BI"],
    languages=[LanguageSkill(language_code="en", level="B2"), LanguageSkill(language_code="sk", level="B1")],
)


def test_the_merged_profile_names_both_titles():
    merged = merge_profiles([SWE, ANALYST])

    assert merged.title == "Junior Software Engineer / Junior Data Analyst"


def test_the_merged_stack_is_the_union_without_repeats():
    merged = merge_profiles([SWE, ANALYST])

    assert merged.tech_stack == ["Python", "Java", "Docker", "SQL", "pandas", "Power BI"]


def test_stack_entries_differing_only_in_case_count_once():
    merged = merge_profiles([
        CandidateProfile(tech_stack=["python", "SQL"]),
        CandidateProfile(tech_stack=["Python", "sql", "Git"]),
    ])

    assert merged.tech_stack == ["python", "SQL", "Git"]


def test_a_field_empty_in_the_first_resume_is_taken_from_the_second():
    merged = merge_profiles([ANALYST, SWE])

    assert merged.location == "Bratislava"
    assert merged.summary == "Builds backend services."


def test_languages_are_joined_by_code():
    merged = merge_profiles([SWE, ANALYST])

    assert [lang.language_code for lang in merged.languages] == ["en", "sk"]


def test_a_single_profile_merges_to_itself():
    assert merge_profiles([SWE]) == SWE


def test_an_identical_title_is_not_repeated():
    merged = merge_profiles([SWE, SWE.model_copy(update={"tech_stack": ["Go"]})])

    assert merged.title == "Junior Software Engineer"


RESUMES = [
    Resume(name="resume.pdf", text="swe text", profile=SWE),
    Resume(name="resume_data_analyst.pdf", text="analyst text", profile=ANALYST),
]


def test_a_vacancy_about_dashboards_and_pandas_picks_the_analyst_resume():
    vacancy = Vacancy(role="Data Analyst", tech_stack=["SQL", "Power BI", "pandas"],
                      raw_text="Reporting in Power BI, SQL queries, pandas notebooks.")

    assert pick_resume(vacancy, RESUMES).name == "resume_data_analyst.pdf"


def test_a_vacancy_about_java_services_in_docker_picks_the_engineering_resume():
    vacancy = Vacancy(role="Backend Developer", tech_stack=["Java", "Docker"],
                      raw_text="Spring services deployed with Docker.")

    assert pick_resume(vacancy, RESUMES).name == "resume.pdf"


def test_the_title_words_count_even_when_the_posting_lists_no_stack():
    vacancy = Vacancy(role="Junior analytik dát", raw_text="Analytik dát pre reporting.")

    assert pick_resume(vacancy, RESUMES).name == "resume_data_analyst.pdf"


def test_a_tie_goes_to_the_first_resume_listed():
    vacancy = Vacancy(role="IT trainee", raw_text="Python and SQL, anything goes.")

    assert pick_resume(vacancy, RESUMES).name == "resume.pdf"


def test_a_single_resume_is_always_picked():
    vacancy = Vacancy(role="Data Analyst", tech_stack=["Power BI"])

    assert pick_resume(vacancy, RESUMES[:1]).name == "resume.pdf"


def test_generic_words_in_the_posting_body_do_not_pull_towards_the_analyst():
    """Seen on real postings: 'databáza' and 'analyzovať' appear in almost any
    Slovak IT ad, and once sent a Java developer role to the analyst resume."""
    vacancy = Vacancy(role="Junior Java Developer",
                      raw_text="Vývoj v Jave, práca s databázou, analyzovať požiadavky, Docker.")

    assert pick_resume(vacancy, RESUMES).name == "resume.pdf"


def test_skills_both_resumes_claim_decide_nothing():
    """Python and SQL sit on both resumes; counting them only adds noise."""
    vacancy = Vacancy(role="Junior Data Analyst", raw_text="Python, SQL, Docker, Java, Docker, Java.")

    assert pick_resume(vacancy, RESUMES).name == "resume_data_analyst.pdf"


def test_a_version_in_the_resume_does_not_hide_the_skill():
    resumes = [
        Resume("resume.pdf", "", CandidateProfile(title="Junior Software Engineer", tech_stack=["Java 21", "C (C99)"])),
        Resume("analyst.pdf", "", CandidateProfile(title="Junior Data Analyst", tech_stack=["pandas"])),
    ]
    vacancy = Vacancy(role="Junior Developer", raw_text="Java and C for embedded tools, some pandas.")

    assert pick_resume(vacancy, resumes).name == "resume.pdf"


def test_a_short_skill_is_matched_as_a_whole_word():
    """'C' must not match every word that starts with a c."""
    resumes = [
        Resume("resume.pdf", "", CandidateProfile(title="Junior Software Engineer", tech_stack=["C"])),
        Resume("analyst.pdf", "", CandidateProfile(title="Junior Data Analyst", tech_stack=["pandas"])),
    ]
    vacancy = Vacancy(role="Junior IT", raw_text="Clean code, continuous delivery, pandas.")

    assert pick_resume(vacancy, resumes).name == "analyst.pdf"
