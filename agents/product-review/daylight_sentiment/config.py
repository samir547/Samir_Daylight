from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

# Repo root -- daylight_sentiment/ sits directly under it.
BASE = Path(__file__).resolve().parent.parent


class LLMProvider(Enum):
    GROQ = "groq"
    CLAUDE = "claude"


@dataclass
class GroqSettings:
    api_key: Optional[str] = None
    # llama-3.1-8b-instant from the original spec is decommissioned; this
    # account serves gpt-oss-120b, which supports response_format=json_object.
    model: str = "openai/gpt-oss-120b"
    # gpt-oss models emit reasoning tokens that count against the TPM budget.
    # "low" is ample for a fixed-vocabulary classification and cuts spend ~35%.
    reasoning_effort: str = "low"
    # Org-level tokens-per-minute ceiling (in + out). This, not latency, sets the
    # floor on wall-clock for the full corpus -- see TokenRateLimiter.
    tpm_limit: int = 8000

    @classmethod
    def from_env(cls) -> "GroqSettings":
        return cls(api_key=os.getenv("GROQ_API_KEY"))


@dataclass
class ClaudeSettings:
    api_key: Optional[str] = None
    model: str = "claude-sonnet-4-6"

    @classmethod
    def from_env(cls) -> "ClaudeSettings":
        return cls(api_key=os.getenv("ANTHROPIC_API_KEY"))


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


@dataclass(frozen=True)
class FilePattern:
    """Loose filename match for a review export (ISSUE-01): every token must
    appear in the name (case-insensitive) and the extension must match. This
    is what survives a fresh export being renamed ("(2)" suffixes, new row
    counts, date stamps) where an exact-filename match hard-fails."""
    tokens: tuple
    extension: str

    def matches(self, name: str) -> bool:
        low = name.lower()
        return low.endswith(self.extension) and all(t in low for t in self.tokens)

    def describe(self) -> str:
        return f"*{'*'.join(self.tokens)}*{self.extension}"


@dataclass
class SourceFiles:
    """Local paths the three review exports are read from (and SharePoint
    syncs into).

    There is no fixed expected filename for any source -- every export
    (locally and on SharePoint) is resolved purely by FilePattern: the
    newest file in the directory whose name contains all of the pattern's
    tokens. A rename, a "(2)" suffix, a fresh row count, a date stamp --
    none of it matters, because nothing ever compares against a specific
    name. See resolve_source_file() (infra/review_sources/base.py) and
    select_items_by_patterns() (infra/sharepoint_sync.py)."""
    reviews_dir: Path = BASE / "Reviews"
    trustpilot_product_pattern: FilePattern = FilePattern(("trustpilot", "product"), ".xlsx")
    trustpilot_service_pattern: FilePattern = FilePattern(("trustpilot", "service"), ".xlsx")
    amazon_product_pattern: FilePattern = FilePattern(("amazon", "product"), ".csv")
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
    # Reviews per LLM call. The instruction block is ~250 tokens, so batching
    # amortises it: ~440 tok/review one-at-a-time vs ~175 tok/review at 10.
    # Under a fixed TPM ceiling that is a direct ~2.5x cut in wall-clock.
    batch_size: int = 10
    request_timeout: int = 30
    max_retries: int = 4
    # Groq also caps tokens-per-day (200k on the free tier, per model) on a
    # rolling window. Waiting it out is the correct response, so these bound how
    # patient a single call may be rather than failing the batch outright.
    max_tpd_wait: float = 900.0   # never sleep more than 15 min in one go
    max_tpd_waits: int = 400      # ~roughly a day of waiting, worst case
    max_review_chars: int = 6000  # guards against a runaway review body


# Legacy flat attribute -> (sub-config field, attribute). The compat layer for
# every call site written against the old single flat dataclass.
_FLAT_ALIASES = {
    "groq_api_key": ("groq", "api_key"),
    "groq_model": ("groq", "model"),
    "groq_reasoning_effort": ("groq", "reasoning_effort"),
    "groq_tpm_limit": ("groq", "tpm_limit"),
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
    "max_tpd_wait": ("tuning", "max_tpd_wait"),
    "max_tpd_waits": ("tuning", "max_tpd_waits"),
    "max_review_chars": ("tuning", "max_review_chars"),
}


@dataclass
class Config:
    """Composed pipeline configuration. ``Config()`` is pure defaults with no
    environment access; use ``Config.from_env()`` for the real thing."""
    llm_provider: LLMProvider = LLMProvider.GROQ
    groq: GroqSettings = field(default_factory=GroqSettings)
    claude: ClaudeSettings = field(default_factory=ClaudeSettings)
    sharepoint: SharePointSettings = field(default_factory=SharePointSettings)
    db: DatabaseSettings = field(default_factory=DatabaseSettings)
    sources: SourceFiles = field(default_factory=SourceFiles)
    output: OutputSettings = field(default_factory=OutputSettings)
    tuning: Tuning = field(default_factory=Tuning)

    @property
    def model_name(self) -> str:
        return self.groq.model if self.llm_provider is LLMProvider.GROQ else self.claude.model

    @classmethod
    def from_env(cls, llm_provider: LLMProvider = LLMProvider.GROQ,
                 dotenv: bool = True) -> "Config":
        """Build a Config from .env + os.environ -- the one explicit place
        environment lookup happens. ``dotenv=False`` skips loading .env
        (tests; callers that manage the environment themselves)."""
        if dotenv:
            from dotenv import load_dotenv
            load_dotenv()
        return cls(
            llm_provider=llm_provider,
            groq=GroqSettings.from_env(),
            claude=ClaudeSettings.from_env(),
            sharepoint=SharePointSettings.from_env(),
            db=DatabaseSettings.from_env(),
            sources=SourceFiles.from_env(),
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
