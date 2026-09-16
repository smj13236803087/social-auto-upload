-- If you already created the old username-only schema, run this once:
--   mysql -u root -p autoself < docs/autoself_schema_email_migrate.sql

USE autoself;

CREATE TABLE IF NOT EXISTS pending_users (
  email VARCHAR(255) NOT NULL PRIMARY KEY,
  username VARCHAR(64) NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  verification_code CHAR(6) NOT NULL,
  expires_at DATETIME(3) NOT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  KEY idx_pending_expires_at (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Add email column if missing (safe to re-run checks manually if it already exists)
SET @has_email := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'email'
);
SET @sql := IF(
  @has_email = 0,
  'ALTER TABLE users ADD COLUMN email VARCHAR(255) NULL AFTER id, ADD UNIQUE KEY uk_users_email (email)',
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
