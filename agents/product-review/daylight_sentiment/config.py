from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple

from daylight_sentiment.domain.action_items import (
    ALL_SCOPE,
    DEFAULT_SCOPE_KEYS,
    MIN_SCOPED_WINDOW_REVIEWS,
    MIN_WINDOW_REVIEWS,
    SCOPES_BY_KEY,
)

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


SHAREPOINT_AUTH_MODES = ("user", "managed_identity")


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
    # How the pipeline proves who it is to SharePoint:
    #   "user"             -- signs in as a person (saved sign-in, password,
    #                         then device code). The original behaviour, and
    #                         the right choice on a person's own computer.
    #   "managed_identity" -- an Azure managed identity attached to the VM
    #                         (no password, no sign-in prompt). Only works on
    #                         that Azure VM; anywhere else it stops at once
    #                         with a clear message.
    auth: str = "user"
    # SharePoint site (e.g. https://tenant.sharepoint.com/sites/NAME) and the
    # folder in its default document library that holds the review exports.
    # When site_url is set the files are read from there; when it is not, the
    # sharing link in share_url is used, as before.
    site_url: Optional[str] = None
    root_folder: str = "Reviews"
    # Client ID of a USER-assigned managed identity (not needed for a
    # system-assigned one).
    managed_identity_client_id: Optional[str] = None

    def __post_init__(self):
        if self.auth not in SHAREPOINT_AUTH_MODES:
            raise ValueError(
                f"SP_AUTH must be one of {', '.join(SHAREPOINT_AUTH_MODES)}; "
                f"got {self.auth!r}")

    @property
    def configured(self) -> bool:
        if self.auth == "managed_identity":
            return bool(self.site_url)
        return bool((self.share_url or self.site_url) and self.username and self.password)

    @classmethod
    def from_env(cls) -> "SharePointSettings":
        return cls(
            share_url=os.getenv("SP_SHARE_URL"),
            username=os.getenv("SP_USERNAME"),
            password=os.getenv("SP_PASSWORD"),
            client_id=os.getenv("SP_CLIENT_ID", cls.client_id),
            tenant=os.getenv("SP_TENANT_ID", cls.tenant),
            auth=(os.getenv("SP_AUTH") or cls.auth).strip().lower(),
            site_url=os.getenv("SP_SITE_URL") or None,
            root_folder=(os.getenv("SP_ROOT_FOLDER") or cls.root_folder).strip().strip("/"),
            managed_identity_client_id=os.getenv("SP_MI_CLIENT_ID") or None,
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


# Below this, a window's to-do items would rest on a handful of reviews.
MIN_REVIEWS_FLOOR = 10


def parse_min_reviews(raw: str, name: str = "ACTION_ITEMS_MIN_REVIEWS",
                      example: int = MIN_WINDOW_REVIEWS) -> int:
    """The minimum number of analysed reviews a window needs, as a whole
    number. Raises ValueError, naming ``name``, for anything that is not a
    whole number of at least MIN_REVIEWS_FLOOR."""
    try:
        value = int(str(raw).strip())
    except ValueError:
        value = 0
    if value < MIN_REVIEWS_FLOOR:
        raise ValueError(
            f"{name} must be a whole number of reviews, {MIN_REVIEWS_FLOOR} or more "
            f"(e.g. {example}); got {raw!r}")
    return value


def parse_min_reviews_scoped(raw: str,
                             name: str = "ACTION_ITEMS_MIN_REVIEWS_SCOPED") -> int:
    """parse_min_reviews() for the minimum of every scope other than "all":
    the same rule and the same floor."""
    return parse_min_reviews(raw, name=name, example=MIN_SCOPED_WINDOW_REVIEWS)


def parse_scopes(raw: str, name: str = "ACTION_ITEMS_SCOPES") -> Tuple[str, ...]:
    """Comma-separated review scope keys ("all,amazon", or a single key),
    in any letter case, returned in the order the scopes are defined with
    repeats removed. Raises ValueError, naming ``name`` and the keys that
    exist, for an empty list or any key that is not a scope."""
    keys = {p.strip().lower() for p in str(raw).split(",")}
    unknown = sorted(k for k in keys if k not in SCOPES_BY_KEY)
    if unknown:
        raise ValueError(
            f"{name} must be one or more of {', '.join(DEFAULT_SCOPE_KEYS)}, separated by "
            f"commas (e.g. all,amazon); unknown: {', '.join(repr(k) for k in unknown)} "
            f"in {raw!r}")
    return tuple(k for k in DEFAULT_SCOPE_KEYS if k in keys)


@dataclass
class ActionItemSettings:
    """The business to-do items generated after a run (see
    application/action_items.py): one set per review scope and review
    window.

    scopes are the keys of the review scopes to generate (see REVIEW_SCOPES
    in domain/action_items.py; default all six), overridable with
    ACTION_ITEMS_SCOPES and, per run, --action-items-scopes. Each window is
    the last N months ending on the newest review date of its scope (or
    today, if that is earlier); windows_months is overridable with
    ACTION_ITEMS_WINDOWS_MONTHS and, per run, --action-items-months. A
    window with too few analysed reviews is skipped: fewer than min_reviews
    for the "all" scope (ACTION_ITEMS_MIN_REVIEWS,
    --action-items-min-reviews), fewer than min_reviews_scoped for every
    other scope (ACTION_ITEMS_MIN_REVIEWS_SCOPED,
    --action-items-min-reviews-scoped)."""
    windows_months: Tuple[int, ...] = (3, 6, 12)
    scopes: Tuple[str, ...] = DEFAULT_SCOPE_KEYS
    min_reviews: int = MIN_WINDOW_REVIEWS
    min_reviews_scoped: int = MIN_SCOPED_WINDOW_REVIEWS
    top_themes: int = 15
    max_excerpts: int = 10
    excerpt_chars: int = 300

    def min_reviews_for(self, scope_key: str) -> int:
        """The minimum number of analysed reviews a window of that scope
        needs."""
        return self.min_reviews if scope_key == ALL_SCOPE.key else self.min_reviews_scoped

    @classmethod
    def from_env(cls) -> "ActionItemSettings":
        settings = cls()
        raw = os.getenv("ACTION_ITEMS_WINDOWS_MONTHS")
        if raw:
            settings.windows_months = parse_window_months(raw)
        raw = os.getenv("ACTION_ITEMS_SCOPES")
        if raw:
            settings.scopes = parse_scopes(raw)
        raw = os.getenv("ACTION_ITEMS_MIN_REVIEWS")
        if raw:
            settings.min_reviews = parse_min_reviews(raw)
        raw = os.getenv("ACTION_ITEMS_MIN_REVIEWS_SCOPED")
        if raw:
            settings.min_reviews_scoped = parse_min_reviews_scoped(raw)
        return settings


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
    "sp_auth": ("sharepoint", "auth"),
    "sp_site_url": ("sharepoint", "site_url"),
    "sp_root_folder": ("sharepoint", "root_folder"),
    "sp_mi_client_id": ("sharepoint", "managed_identity_client_id"),
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
