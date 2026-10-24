"""Connection construction + schema migrations for the sentiment database.

connect() is the public replacement for DatabaseWriter._connect() -- every
script that needs a raw pyodbc connection should call this instead of
reaching into a "private" method (ISSUE-07).

ensure_schema() applies MIGRATIONS, a small ordered registry of idempotent
statements (each guards itself with IF OBJECT_ID / IF NOT EXISTS, so
re-running the whole list on every startup is always safe). New schema
changes are appended to the registry, never edited in place.
"""
from __future__ import annotations

from daylight_sentiment.config import DatabaseSettings

REVIEWS_TABLE = "sentiment_analysis_reviews"
RUNS_TABLE = "sentiment_analysis_runs"
ACTION_ITEMS_TABLE = "sentiment_action_items"


def connect(settings: DatabaseSettings):
    """Open a pyodbc connection to the sentiment database."""
    if not settings.configured or not settings.name:
        raise RuntimeError(
            "DB_SERVER/DB_NAME/DB_USER/DB_PASSWORD must all be set (add them to .env) "
            "to connect to the database.")
    import pyodbc
    conn_str = (
        f"DRIVER={{{settings.driver}}};"
        f"SERVER={settings.server},{settings.port};"
        f"DATABASE={settings.name};"
        f"UID={settings.user};PWD={settings.password};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
        # Larger negotiated TDS packet sizes appear to hang indefinitely on
        # this network path (probably a middlebox mishandling reassembly of
        # bigger encrypted packets) -- capping it avoids that.
        "Packet Size=4096;"
    )
    conn = pyodbc.connect(conn_str, timeout=30)
    # Without this, a dropped connection mid-query hangs the driver forever
    # instead of raising -- this bounds every statement to 120s.
    conn.timeout = 120
    return conn


# Ordered, idempotent, append-only. The reviews table already exists in
# production (2,933 rows, from before model_number/review_country existed) --
# the CREATE TABLE only fires on a brand-new install, so columns added since
# get their own guarded ALTERs (checked per-column via sys.columns).
MIGRATIONS = [
    ("create_reviews_table", f"""
IF OBJECT_ID('dbo.{REVIEWS_TABLE}', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.{REVIEWS_TABLE} (
        uid               NVARCHAR(255)   NOT NULL PRIMARY KEY,
        review_id         NVARCHAR(255)   NULL,
        review_type       NVARCHAR(50)    NULL,
        review_source     NVARCHAR(50)    NULL,
        rating            INT             NULL,
        product_name      NVARCHAR(500)   NULL,
        product_sku       NVARCHAR(100)   NULL,
        product_asin      NVARCHAR(100)   NULL,
        model_number      NVARCHAR(50)    NULL,
        review_country    NVARCHAR(100)   NULL,
        title             NVARCHAR(1000)  NULL,
        review_text       NVARCHAR(MAX)   NULL,
        reviewer_name     NVARCHAR(255)   NULL,
        review_date       NVARCHAR(100)   NULL,
        verified_purchase BIT             NULL,
        sentiment         NVARCHAR(20)    NULL,
        confidence        FLOAT           NULL,
        primary_sector    NVARCHAR(50)    NULL,
        primary_aspect    NVARCHAR(50)    NULL,
        key_themes        NVARCHAR(MAX)   NULL,
        analysis_error    NVARCHAR(500)   NULL,
        loaded_at         DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME()
    );
END"""),
    ("add_model_number", f"""
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('dbo.{REVIEWS_TABLE}') AND name = 'model_number')
    ALTER TABLE dbo.{REVIEWS_TABLE} ADD model_number NVARCHAR(50) NULL;
"""),
    ("add_review_country", f"""
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('dbo.{REVIEWS_TABLE}') AND name = 'review_country')
    ALTER TABLE dbo.{REVIEWS_TABLE} ADD review_country NVARCHAR(100) NULL;
"""),
    ("create_runs_table", f"""
IF OBJECT_ID('dbo.{RUNS_TABLE}', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.{RUNS_TABLE} (
        run_id                 INT IDENTITY PRIMARY KEY,
        run_timestamp           DATETIME2      NOT NULL,
        llm_provider             NVARCHAR(50)   NULL,
        llm_model                NVARCHAR(100)  NULL,
        total_reviews             INT           NULL,
        total_reviews_analyzed    INT           NULL,
        failed_reviews            INT           NULL,
        positive_pct              FLOAT         NULL,
        negative_pct              FLOAT         NULL,
        neutral_pct               FLOAT         NULL,
        elapsed_seconds           FLOAT         NULL,
        full_summary_json         NVARCHAR(MAX) NULL,
        loaded_at                 DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_{RUNS_TABLE}_timestamp UNIQUE (run_timestamp)
    );
END"""),
    ("create_action_items_table", f"""
IF OBJECT_ID('dbo.{ACTION_ITEMS_TABLE}', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.{ACTION_ITEMS_TABLE} (
        item_id            INT IDENTITY PRIMARY KEY,
        generation_id      UNIQUEIDENTIFIER NOT NULL,
        generated_at       DATETIME2        NOT NULL DEFAULT SYSUTCDATETIME(),
        run_timestamp      DATETIME2        NULL,
        [rank]             TINYINT          NOT NULL,
        title              NVARCHAR(200)    NOT NULL,
        description        NVARCHAR(MAX)    NOT NULL,
        rationale          NVARCHAR(MAX)    NOT NULL,
        related_area       NVARCHAR(100)    NOT NULL,
        evidence           NVARCHAR(MAX)    NOT NULL,
        window_start       DATE             NOT NULL,
        window_end         DATE             NOT NULL,
        reviews_analysed   INT              NOT NULL,
        llm_provider       NVARCHAR(50)     NULL,
        llm_model          NVARCHAR(100)    NULL,
        CONSTRAINT UQ_{ACTION_ITEMS_TABLE}_generation_rank UNIQUE (generation_id, [rank])
    );
END"""),
    ("add_action_items_evidence_strength", f"""
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('dbo.{ACTION_ITEMS_TABLE}')
                 AND name = 'evidence_strength')
    ALTER TABLE dbo.{ACTION_ITEMS_TABLE} ADD evidence_strength NVARCHAR(20) NULL;
"""),
    ("add_action_items_window_label", f"""
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('dbo.{ACTION_ITEMS_TABLE}')
                 AND name = 'window_label')
    ALTER TABLE dbo.{ACTION_ITEMS_TABLE} ADD window_label NVARCHAR(30) NULL;
"""),
    ("add_action_items_window_months", f"""
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('dbo.{ACTION_ITEMS_TABLE}')
                 AND name = 'window_months')
    ALTER TABLE dbo.{ACTION_ITEMS_TABLE} ADD window_months TINYINT NULL;
"""),
]


def ensure_schema(conn) -> None:
    """Apply every migration in order; all are idempotent, so this is safe to
    run on every startup."""
    cur = conn.cursor()
    for _name, sql in MIGRATIONS:
        cur.execute(sql)
    conn.commit()
