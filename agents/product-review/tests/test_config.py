"""ISSUE-11: Config split -- pure defaults vs explicit from_env(), plus the
flat-alias compatibility layer every existing call site depends on."""
import pytest

from daylight_sentiment.config import (
    Config,
    DatabaseSettings,
    LLMProvider,
    SharePointSettings,
    parse_window_months,
)

ENV_KEYS = {
    "ANTHROPIC_API_KEY": "ak",
    "SP_SHARE_URL": "https://sp", "SP_USERNAME": "u", "SP_PASSWORD": "p",
    "DB_SERVER": "srv", "DB_NAME": "db", "DB_USER": "dbu", "DB_PASSWORD": "dbp",
    "DB_PORT": "1533",
}


def _set_env(monkeypatch):
    for k, v in ENV_KEYS.items():
        monkeypatch.setenv(k, v)


def test_plain_construction_never_reads_environment(monkeypatch):
    _set_env(monkeypatch)
    cfg = Config()
    assert cfg.claude.api_key is None
    assert cfg.db.server is None
    assert cfg.sharepoint.share_url is None
    assert cfg.db.port == 1433  # default, not the env's 1533


def test_from_env_reads_environment(monkeypatch):
    _set_env(monkeypatch)
    cfg = Config.from_env(llm_provider=LLMProvider.CLAUDE, dotenv=False)
    assert cfg.llm_provider is LLMProvider.CLAUDE
    assert cfg.claude.api_key == "ak"
    assert cfg.db.server == "srv"
    assert cfg.db.port == 1533
    assert cfg.sharepoint.username == "u"


def test_flat_aliases_read(monkeypatch):
    _set_env(monkeypatch)
    cfg = Config.from_env(dotenv=False)
    # The exact names every pre-refactor call site uses.
    assert cfg.claude_api_key == "ak"
    assert cfg.db_server == "srv"
    assert cfg.db_port == 1533
    assert cfg.sp_share_url == "https://sp"
    assert cfg.output_file == "multi_source_sentiment_analysis.json"
    assert cfg.detail_file == "multi_source_sentiment_per_review.json"
    assert cfg.checkpoint_file == "multi_source_checkpoint.jsonl"
    assert cfg.max_review_chars == 6000
    assert cfg.base_path == cfg.sources.reviews_dir


def test_flat_aliases_write_through():
    cfg = Config()
    cfg.max_workers = 4          # main() does exactly this
    cfg.batch_size = 5
    cfg.claude_model = "other-model"
    assert cfg.tuning.max_workers == 4
    assert cfg.tuning.batch_size == 5
    assert cfg.claude.model == "other-model"


def test_unknown_attribute_still_raises():
    with pytest.raises(AttributeError):
        Config().not_a_setting


def test_claude_is_the_default_provider(monkeypatch):
    _set_env(monkeypatch)
    assert Config().llm_provider is LLMProvider.CLAUDE
    assert Config.from_env(dotenv=False).llm_provider is LLMProvider.CLAUDE


def test_model_name_follows_provider():
    cfg = Config(llm_provider=LLMProvider.CLAUDE)
    assert cfg.model_name == "claude-sonnet-4-6"
    cfg.claude.model = "claude-x"
    assert cfg.model_name == "claude-x"


def test_claude_model_defaults_without_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    assert Config.from_env(dotenv=False).claude.model == "claude-sonnet-4-6"
    monkeypatch.setenv("CLAUDE_MODEL", "")  # set but empty -> still the default
    assert Config.from_env(dotenv=False).claude.model == "claude-sonnet-4-6"


def test_claude_model_from_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_MODEL", "claude-env-model")
    cfg = Config.from_env(dotenv=False)
    assert cfg.claude.model == "claude-env-model"
    assert cfg.model_name == "claude-env-model"
    assert Config().claude.model == "claude-sonnet-4-6"  # plain Config ignores env


def test_configured_properties():
    assert not DatabaseSettings().configured
    assert DatabaseSettings(server="s", user="u", password="p").configured
    assert not SharePointSettings().configured
    assert SharePointSettings(share_url="s", username="u", password="p").configured


def test_action_item_defaults():
    settings = Config().action_items
    assert settings.windows_months == (3, 6, 12)
    assert (settings.top_themes, settings.max_excerpts, settings.excerpt_chars) == (15, 10, 300)
    assert not hasattr(settings, "window_months")


@pytest.mark.parametrize("raw, expected", [
    ("3,6,12", (3, 6, 12)),
    (" 12 , 3,6 ", (3, 6, 12)),
    ("6", (6,)),
    ("6,6,3", (3, 6)),
])
def test_parse_window_months(raw, expected):
    assert parse_window_months(raw) == expected


def test_action_items_windows_months_from_env(monkeypatch):
    monkeypatch.delenv("ACTION_ITEMS_WINDOWS_MONTHS", raising=False)
    assert Config.from_env(dotenv=False).action_items.windows_months == (3, 6, 12)
    monkeypatch.setenv("ACTION_ITEMS_WINDOWS_MONTHS", "1,24")
    assert Config.from_env(dotenv=False).action_items.windows_months == (1, 24)
    monkeypatch.setenv("ACTION_ITEMS_WINDOWS_MONTHS", "6")
    assert Config.from_env(dotenv=False).action_items.windows_months == (6,)
    assert Config().action_items.windows_months == (3, 6, 12)  # plain Config ignores env


def test_old_singular_setting_is_not_read(monkeypatch):
    monkeypatch.delenv("ACTION_ITEMS_WINDOWS_MONTHS", raising=False)
    monkeypatch.setenv("ACTION_ITEMS_WINDOW_MONTHS", "18")
    assert Config.from_env(dotenv=False).action_items.windows_months == (3, 6, 12)


@pytest.mark.parametrize("raw", ["0", "-1", "twelve", "3,,12", "3;6", "3,0", "1.5", "256",
                                 ","])
def test_action_items_windows_months_rejects_bad_values(monkeypatch, raw):
    monkeypatch.setenv("ACTION_ITEMS_WINDOWS_MONTHS", raw)
    with pytest.raises(ValueError, match="ACTION_ITEMS_WINDOWS_MONTHS must be one or more "
                                         "whole numbers of months from 1 to 255"):
        Config.from_env(dotenv=False)
