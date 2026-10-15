-- ============================================================================
-- 006  Migration of the 2026-10-11 tables: BACKUP ONLY
--      (data-ingestion-spec.md section 7.6, task T11)
--
-- Copies
--     dbo.Daylight_sales_report      -> dbo.Daylight_sales_report_bak_20261015
--     dbo.Daylight_inventory_report  -> dbo.Daylight_inventory_report_bak_20261015
-- and checks that the row counts match. Nothing is dropped, altered or
-- deleted. The source tables are left exactly as they are.
--
-- Order: section 7.6 says to take the copy before anything else. This script
-- does not depend on 001-005 and is safe to run first.
--
-- Re-runnable: an existing backup is NEVER overwritten (it is reported and
-- its row count re-checked). To take a fresh copy, rename or drop the old
-- backup by hand first.
-- Each copy runs in its own transaction with a shared table lock held on the
-- source until commit, so the count check compares like with like; a
-- mismatch rolls the copy back and raises an error.
--
-- Run BY HAND by someone with database rights (section 7.2).
-- ============================================================================
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
SET XACT_ABORT ON;
SET NOCOUNT ON;
GO

DECLARE @pairs TABLE (seq INT IDENTITY(1,1), src SYSNAME, bak SYSNAME);
INSERT @pairs (src, bak) VALUES
    ('Daylight_sales_report',     'Daylight_sales_report_bak_20261015'),
    ('Daylight_inventory_report', 'Daylight_inventory_report_bak_20261015');

DECLARE @result TABLE (source_table SYSNAME, backup_table SYSNAME, outcome VARCHAR(10),
                       source_rows BIGINT NULL, backup_rows BIGINT NULL);

DECLARE @i INT = 1, @n INT = (SELECT COUNT(*) FROM @pairs);
DECLARE @src SYSNAME, @bak SYSNAME, @src_q NVARCHAR(300), @bak_q NVARCHAR(300),
        @sql NVARCHAR(MAX), @src_rows BIGINT, @bak_rows BIGINT, @msg NVARCHAR(400);

WHILE @i <= @n
BEGIN
    SELECT @src = src, @bak = bak FROM @pairs WHERE seq = @i;
    SELECT @src_q = N'dbo.' + QUOTENAME(@src), @bak_q = N'dbo.' + QUOTENAME(@bak),
           @src_rows = NULL, @bak_rows = NULL;

    IF OBJECT_ID(@src_q, 'U') IS NULL
    BEGIN
        PRINT CONCAT('SKIP   ', @src_q, ' does not exist; nothing to back up.');
        INSERT @result VALUES (@src, @bak, 'SKIP', NULL, NULL);
    END
    ELSE IF OBJECT_ID(@bak_q, 'U') IS NOT NULL
    BEGIN
        SET @sql = N'SELECT @c = COUNT_BIG(*) FROM ' + @src_q + N';';
        EXEC sys.sp_executesql @sql, N'@c BIGINT OUTPUT', @c = @src_rows OUTPUT;
        SET @sql = N'SELECT @c = COUNT_BIG(*) FROM ' + @bak_q + N';';
        EXEC sys.sp_executesql @sql, N'@c BIGINT OUTPUT', @c = @bak_rows OUTPUT;
        PRINT CONCAT('EXISTS ', @bak_q, ' already exists; not overwritten.');
        INSERT @result VALUES (@src, @bak, 'EXISTS', @src_rows, @bak_rows);
    END
    ELSE
    BEGIN
        BEGIN TRANSACTION;
            -- The shared table lock on the source is held until COMMIT, so no
            -- writer can change it between the copy and the count.
            SET @sql = N'SELECT * INTO ' + @bak_q + N' FROM ' + @src_q + N' WITH (TABLOCK, HOLDLOCK);';
            EXEC sys.sp_executesql @sql;

            SET @sql = N'SELECT @c = COUNT_BIG(*) FROM ' + @src_q + N' WITH (TABLOCK, HOLDLOCK);';
            EXEC sys.sp_executesql @sql, N'@c BIGINT OUTPUT', @c = @src_rows OUTPUT;
            SET @sql = N'SELECT @c = COUNT_BIG(*) FROM ' + @bak_q + N';';
            EXEC sys.sp_executesql @sql, N'@c BIGINT OUTPUT', @c = @bak_rows OUTPUT;

            IF @src_rows <> @bak_rows
            BEGIN
                ROLLBACK TRANSACTION;
                SET @msg = CONCAT('Row count mismatch copying ', @src_q, ': source ', @src_rows,
                                  ', backup ', @bak_rows, '. Backup rolled back.');
                THROW 50006, @msg, 1;
            END
        COMMIT TRANSACTION;
        PRINT CONCAT('COPIED ', @src_q, ' -> ', @bak_q, ' (', @bak_rows, ' rows)');
        INSERT @result VALUES (@src, @bak, 'COPIED', @src_rows, @bak_rows);
    END
    SET @i += 1;
END

-- Expect counts_match = 1 on both rows before going further with section 7.6.
-- (On an EXISTS row a mismatch means the source changed after the backup.)
SELECT source_table, backup_table, outcome, source_rows, backup_rows,
       CASE WHEN source_rows = backup_rows THEN 1 ELSE 0 END AS counts_match
FROM @result;
GO

-- ----------------------------------------------------------------------------
-- DROP OF THE OLD TABLES: DELIBERATELY COMMENTED OUT. Do not uncomment until
-- ALL of the following are done and recorded in the PR (section 7.6):
--   1. No readers of the old names (Power BI datasets, other agents, saved
--      queries) - step 1.
--   2. Backup row counts above match - step 2.
--   3. Spec tables created (001-005) and loaded from the validated CSVs by
--      load_csv_to_db.py (T5), not copied from the old tables - steps 3 and 4.
--   4. Coverage check run on old and new tables, differences explained
--      (007_checks.sql) - step 5.
--   5. Samir has signed off - step 6.
-- Keep the _bak_20261015 copies until then (and after, until agreed).
--
-- DROP TABLE dbo.Daylight_sales_report;
-- DROP TABLE dbo.Daylight_inventory_report;
-- ----------------------------------------------------------------------------
