from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple

# Repo root -- daylight_sentiment/ sits directly under it.
BASE = Path(__file__).resolve().parent.parent


class LLMProvider(Enum):
    """Supported LLM providers -- the extension point for adding another one.
    Each member's value is also the name of its settings field on Config, and
    the key it is registered under in infra/analyzers/factory.py."""
    CLAUDE = "claude"


@dataclass
class ClaudeSettings:
    api_key: Optional[str] = None
    # Overridable per environment with CLAUDE_MODEL, and per run with --model.
    model: str = "claude-sonnet-4-6"

    @classmethod
    def from_env(cls) -> "ClaudeSettings":
        return cls(
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            model=os.getenv("CLAUDE_MODEL") or cls.model,
        )


@dataclass
class SharePointSettings:
    """SharePoint source for the review exports. When configured (and sync
    isn't skipped via --skip-sharepoint-sync), the pipeline downloads fresh
    copies into Reviews/ before loading, instead of trusting whatever's
    already on disk."""
    share_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    client_id: str = "14d82eec-204b-4c2f-b7e8-296a70dab67e"  # MS Graph PowerShell public client
    tenant: str = "organizations"

    @property
    def configured(self) -> bool:
        return bool(self.share_url and self.username and self.password)

    @classmethod
    def from_env(cls) -> "SharePointSettings":
        return cls(
            share_url=os.getenv("SP_SHARE_URL"),
            username=os.getenv("SP_USERNAME"),
            password=os.getenv("SP_PASSWORD"),
            client_id=os.getenv("SP_CLIENT_ID", cls.client_id),
            tenant=os.getenv("SP_TENANT_ID", cls.tenant),
        )


@dataclass
class DatabaseSettings:
    """SQL Server target for loading the two JSON outputs. When configured
    (and not skipped via --skip-db-load), the pipeline upserts into it after
    writing the JSON files."""
    server: Optional[str] = None
    name: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None
    driver: str = "ODBC Driver 18 for SQL Server"
    port: int = 1433

    @property
    def configured(self) -> bool:
        return bool(self.server and self.user and self.password)

    @classmethod
    def from_env(cls) -> "DatabaseSettings":
        return cls(
            server=os.getenv("DB_SERVER"),
            name=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            driver=os.getenv("DB_DRIVER", cls.driver),
            port=int(os.getenv("DB_PORT", str(cls.port))),
        )


# Every review source may be exported as Excel or CSV; the loaders pick the
# reader from the file's own extension (see review_sources/base.read_table).
REVIEW_FILE_EXTENSIONS = (".xlsx", ".csv")

# Excel writes "~$<name>.xlsx" next to a workbook while it is open: a tiny
# owner file, not an export, that would otherwise match and (being newest) win.
OFFICE_LOCK_FILE_PREFIX = "~$"


@dataclass(frozen=True)
class FilePattern:
    """Loose filename match for a review export (ISSUE-01): every token must
    appear in the name (case-insensitive) and the name must end in one of the
    extensions. This is what survives a fresh export being renamed ("(2)"
    suffixes, new row counts, date stamps) where an exact-filename match
    hard-fails. A single extension may be passed as a plain string. Office
    lock files ("~$..." names) never match, so neither the local lookup nor
    the SharePoint pick-up can choose one."""
    tokens: tuple
    extensions: tuple = REVIEW_FILE_EXTENSIONS

    def __post_init__(self):
        exts = (self.extensions,) if isinstance(self.extensions, str) else self.extensions
        object.__setattr__(self, "extensions", tuple(e.lower() for e in exts))

    def matches(self, name: str) -> bool:
        if name.startswith(OFFICE_LOCK_FILE_PREFIX):
            return False
        low = name.lower()
        return low.endswith(self.extensions) and all(t in low for t in self.tokens)

    def describe(self) -> str:
        exts = [e.lstrip(".") for e in self.extensions]
        suffix = exts[0] if len(exts) == 1 else "{" + ",".join(exts) + "}"
        return f"*{'*'.join(self.tokens)}*.{suffix}"


@dataclass
class SourceFiles:
    """Local paths the three review exports are read from (and SharePoint
    syncs into).

    There is no fixed expected filename for any source -- every export
    (locally and on SharePoint) is resolved purely by FilePattern: the
    newest file in the directory whose name contains all of the pattern's
    tokens and ends in .xlsx or .csv (either format, for every source). A
    rename, a "(2)" suffix, a fresh row count, a date stamp -- none of it
    matters, because nothing ever compares against a specific name. See resolve_source_file() (infra/review_sources/base.py) and
    select_items_by_patterns() (infra/sharepoint_sync.py)."""
    reviews_dir: Path = BASE / "Reviews"
    trustpilot_product_pattern: FilePattern = FilePattern(("trustpilot", "product"))
    trustpilot_service_pattern: FilePattern = FilePattern(("trustpilot", "service"))
    amazon_product_pattern: FilePattern = FilePattern(("amazon", "product"))
    # SharePoint subfolders, relative to whatever SP_SHARE_URL resolves to -- scopes
    # the sync to files staged in "In Progress" only (never a sibling "Processed").
    sp_trustpilot_product_dir: str = "TrustPilot/Product/In Progress"
    sp_trustpilot_service_dir: str = "TrustPilot/Service/In Progress"
    sp_amazon_product_dir: str = "Amazon/In Progress"
    # Where each source's file is moved once its reviews are confirmed durable
    # in the database (never before -- see cli.move_synced_files_to_processed).
    sp_trustpilot_product_processed_dir: str = "TrustPilot/Product/Processed"
    sp_trustpilot_service_processed_dir: str = "TrustPilot/Service/Processed"
    sp_amazon_product_processed_dir: str = "Amazon/Processed"

    @classmethod
    def from_env(cls) -> "SourceFiles":
        defaults = cls()
        return cls(
            sp_trustpilot_product_dir=os.getenv(
                "SP_TRUSTPILOT_PRODUCT_DIR", defaults.sp_trustpilot_product_dir),
            sp_trustpilot_service_dir=os.getenv(
                "SP_TRUSTPILOT_SERVICE_DIR", defaults.sp_trustpilot_service_dir),
            sp_amazon_product_dir=os.getenv(
                "SP_AMAZON_PRODUCT_DIR", defaults.sp_amazon_product_dir),
            sp_trustpilot_product_processed_dir=os.getenv(
                "SP_TRUSTPILOT_PRODUCT_PROCESSED_DIR",
                defaults.sp_trustpilot_product_processed_dir),
            sp_trustpilot_service_processed_dir=os.getenv(
                "SP_TRUSTPILOT_SERVICE_PROCESSED_DIR",
                defaults.sp_trustpilot_service_processed_dir),
            sp_amazon_product_processed_dir=os.getenv(
                "SP_AMAZON_PRODUCT_PROCESSED_DIR",
                defaults.sp_amazon_product_processed_dir),
        )


@dataclass
class OutputSettings:
    dir: Path = BASE / "Trustpilot_Result"
    summary_file: str = "multi_source_sentiment_analysis.json"
    detail_file: str = "multi_source_sentiment_per_review.json"
    checkpoint_file: str = "multi_source_checkpoint.jsonl"


@dataclass
class Tuning:
    max_workers: int = 8
    # Reviews per unit of work handed to a worker thread (and to
    # SentimentAnalyzer.analyze_batch). The Claude analyzer still makes one
    # call per review; a provider that overrides analyze_batch can send the
    # whole batch in one prompt.
    batch_size: int = 10
    request_timeout: int = 30
    max_retries: int = 4
    max_review_chars: int = 6000  # guards against a runaway review body


# A window's length is stored in a TINYINT column (window_months).
MAX_WINDOW_MONTHS = 255


def parse_window_months(raw: str, name: str = "ACTION_ITEMS_WINDOWS_MONTHS") -> Tuple[int, ...]:
    """Comma-separated window lengths in months ("3,6,12", or a single
    number), returned sorted with repeats removed. Raises ValueError, naming
    ``name``, for an empty list or any value that is not a whole number from
    1 to MAX_WINDOW_MONTHS."""
    parts = [p.strip() for p in str(raw).split(",")]
    months = set()
    for part in parts:
        try:
            value = int(part)
        except ValueError:
            value = 0
        if not 1 <= value <= MAX_WINDOW_MONTHS:
            raise ValueError(
                f"{name} must be one or more whole numbers of months from 1 to "
                f"{MAX_WINDOW_MONTHS}, separated by commas (e.g. 3,6,12); got {raw!r}")
        months.add(value)
    return tuple(sorted(months))


@dataclass
class ActionItemSettings:
    """The business to-do items generated after a run (see
    application/action_items.py): one set per review window. Each window is
    the last N months ending on the newest review date in the data (or
    today, if that is earlier); windows_months is overridable with
    ACTION_ITEMS_WINDOWS_MONTHS and, per run, --action-items-months."""
    windows_months: Tuple[int, ...] = (3, 6, 12)
    top_themes: int = 15
    max_excerpts: int = 10
    excerpt_chars: int = 300

    @classmethod
    def from_env(cls) -> "ActionItemSettings":
        raw = os.getenv("ACTION_ITEMS_WINDOWS_MONTHS")
        if not raw:
            return cls()
        return cls(windows_months=parse_window_months(raw))


# Legacy flat attribute -> (sub-config field, attribute). The compat layer for
# every call site written against the old single flat dataclass.
_FLAT_ALIASES = {
    "claude_api_key": ("claude", "api_key"),
    "claude_model": ("claude", "model"),
    "sp_share_url": ("sharepoint", "share_url"),
    "sp_username": ("sharepoint", "username"),
    "sp_password": ("sharepoint", "password"),
    "sp_client_id": ("sharepoint", "client_id"),
    "sp_tenant": ("sharepoint", "tenant"),
    "db_server": ("db", "server"),
    "db_name": ("db", "name"),
    "db_user": ("db", "user"),
    "db_password": ("db", "password"),
    "db_driver": ("db", "driver"),
    "db_port": ("db", "port"),
    "base_path": ("sources", "reviews_dir"),
    "output_dir": ("output", "dir"),
    "output_file": ("output", "summary_file"),
    "detail_file": ("output", "detail_file"),
    "checkpoint_file": ("output", "checkpoint_file"),
    "max_workers": ("tuning", "max_workers"),
    "batch_size": ("tuning", "batch_size"),
    "request_timeout": ("tuning", "request_timeout"),
    "max_retries": ("tuning", "max_retries"),
    "max_review_chars": ("tuning", "max_review_chars"),
}


@dataclass
class Config:
    """Composed pipeline configuration. ``Config()`` is pure defaults with no
    environment access; use ``Config.from_env()`` for the real thing."""
    llm_provider: LLMProvider = LLMProvider.CLAUDE
    claude: ClaudeSettings = field(default_factory=ClaudeSettings)
    sharepoint: SharePointSettings = field(default_factory=SharePointSettings)
    db: DatabaseSettings = field(default_factory=DatabaseSettings)
    sources: SourceFiles = field(default_factory=SourceFiles)
    output: OutputSettings = field(default_factory=OutputSettings)
    tuning: Tuning = field(default_factory=Tuning)
    action_items: ActionItemSettings = field(default_factory=ActionItemSettings)

    @property
    def model_name(self) -> str:
        """Model of the active provider (its settings field is named after
        the LLMProvider value)."""
        return getattr(self, self.llm_provider.value).model

    @classmethod
    def from_env(cls, llm_provider: LLMProvider = LLMProvider.CLAUDE,
                 dotenv: bool = True) -> "Config":
        """Build a Config from .env + os.environ -- the one explicit place
        environment lookup happens. ``dotenv=False`` skips loading .env
        (tests; callers that manage the environment themselves)."""
        if dotenv:
            from dotenv import load_dotenv
            load_dotenv()
        return cls(
            llm_provider=llm_provider,
            claude=ClaudeSettings.from_env(),
            sharepoint=SharePointSettings.from_env(),
            db=DatabaseSettings.from_env(),
            sources=SourceFiles.from_env(),
            action_items=ActionItemSettings.from_env(),
        )

    # -- legacy flat-attribute compatibility (reads AND writes) --------------
    def __getattr__(self, name: str):
        try:
            sub, attr = _FLAT_ALIASES[name]
        except KeyError:
            raise AttributeError(
                f"{type(self).__name__!s} has no attribute {name!r}") from None
        return getattr(getattr(self, sub), attr)

    def __setattr__(self, name: str, value) -> None:
        alias = _FLAT_ALIASES.get(name)
        if alias is not None:
            setattr(getattr(self, alias[0]), alias[1], value)
        else:
            object.__setattr__(self, name, value)
