import pytest

from app.config import load_config

MINIMAL = """
[run]
resume_path = "data/CV.pdf"
"""

FULL = """
[search]
list_url = "https://www.profesia.sk/praca/bratislava/?count_days=1"
start_page = 1
stop_page = 6

[preferences]
desired_roles = ["junior", "student"]
deal_breakers = ["senior", "medior"]
require_title_keywords = ["junior"]
locations = ["Bratislava"]
min_salary_month = 900
min_salary_hour = 6

[models]
extract = "gemini-3.1-flash-lite"
rough_score = "gemini-3.5-flash-lite"
final_judge = "gemini-3.5-flash"

[run]
resume_path = "data/CV.pdf"
state_path = "state/seen.db"
report_dir = "reports"
call_budget = 150
min_score = 40
final_judge_limit = 12

[quota."gemini-3.5-flash"]
rpm = 10
rpd = 1500

[quota."gemini-3.1-flash-lite"]
rpm = 15
rpd = 1000
"""


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_reads_search_preferences(tmp_path):
    config = load_config(write(tmp_path, FULL))

    assert config.preferences.deal_breakers == ["senior", "medior"]
    assert config.preferences.min_salary_month == 900
    assert config.preferences.require_title_keywords == ["junior"]


def test_reads_run_settings(tmp_path):
    config = load_config(write(tmp_path, FULL))

    assert config.call_budget == 150
    assert config.min_score == 40
    assert config.stop_page == 6


def test_paths_are_resolved_against_the_config_file(tmp_path):
    """The container mounts directories; a relative path in the config must not
    depend on the working directory the run happens to start in."""
    config = load_config(write(tmp_path, FULL))

    assert config.state_path == tmp_path / "state" / "seen.db"
    assert config.report_dir == tmp_path / "reports"


def test_quota_limits_are_read_per_model(tmp_path):
    config = load_config(write(tmp_path, FULL))

    assert config.quota_for("gemini-3.5-flash").rpd == 1500
    assert config.quota_for("gemini-3.1-flash-lite").rpm == 15


def test_an_unlisted_model_gets_a_cautious_default(tmp_path):
    """Google does not publish free-tier limits, so an unknown model must not
    be assumed generous."""
    config = load_config(write(tmp_path, FULL))

    fallback = config.quota_for("gemini-2.5-flash-lite")

    assert fallback.rpm <= 15
    assert fallback.rpd <= 250


def test_each_stage_names_its_own_model(tmp_path):
    """Three stages hit three separate daily quotas; which model does what has
    to be tunable, because the quotas are."""
    config = load_config(write(tmp_path, FULL))

    assert config.models.extract == "gemini-3.1-flash-lite"
    assert config.models.rough_score == "gemini-3.5-flash-lite"
    assert config.models.final_judge == "gemini-3.5-flash"


def test_the_finalist_cap_leaves_room_under_the_judges_daily_quota(tmp_path):
    config = load_config(write(tmp_path, FULL))

    assert config.final_judge_limit == 12
    assert config.final_judge_limit < config.quota_for(config.models.final_judge).rpd


def test_omitted_settings_fall_back_to_defaults(tmp_path):
    config = load_config(write(tmp_path, MINIMAL))

    assert config.preferences.deal_breakers == []
    assert config.call_budget > 0
    assert config.stop_page >= 1


def test_a_missing_config_file_says_which_path_was_tried(tmp_path):
    missing = tmp_path / "nope.toml"

    with pytest.raises(FileNotFoundError) as error:
        load_config(missing)

    assert "nope.toml" in str(error.value)
