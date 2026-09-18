-- Admin lists: publish history + platform account bindings
CREATE TABLE IF NOT EXISTS publish_events (
  id VARCHAR(32) NOT NULL PRIMARY KEY,
  user_id BIGINT UNSIGNED NULL,
  source VARCHAR(32) NOT NULL DEFAULT '',
  source_label VARCHAR(64) NOT NULL DEFAULT '',
  title VARCHAR(200) NOT NULL DEFAULT '',
  source_ref VARCHAR(300) NOT NULL DEFAULT '',
  status VARCHAR(16) NOT NULL DEFAULT 'failed',
  detail VARCHAR(500) NOT NULL DEFAULT '',
  created_at DATETIME(3) NOT NULL,
  KEY idx_publish_events_created (created_at),
  KEY idx_publish_events_user_created (user_id, created_at),
  KEY idx_publish_events_status_created (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS publish_event_targets (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  event_id VARCHAR(32) NOT NULL,
  platform VARCHAR(32) NOT NULL DEFAULT '',
  account VARCHAR(128) NOT NULL DEFAULT '',
  ok TINYINT(1) NOT NULL DEFAULT 0,
  error VARCHAR(500) NOT NULL DEFAULT '',
  KEY idx_publish_targets_event (event_id),
  KEY idx_publish_targets_platform (platform),
  CONSTRAINT fk_publish_targets_event
    FOREIGN KEY (event_id) REFERENCES publish_events(id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS platform_accounts (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_id BIGINT UNSIGNED NULL,
  platform VARCHAR(32) NOT NULL,
  account_key VARCHAR(128) NOT NULL,
  display_name VARCHAR(128) NOT NULL DEFAULT '',
  platform_id VARCHAR(128) NOT NULL DEFAULT '',
  last_valid TINYINT(1) NULL,
  last_checked_at DATETIME(3) NULL,
  last_error VARCHAR(300) NOT NULL DEFAULT '',
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_platform_account (platform, account_key),
  KEY idx_platform_accounts_user (user_id),
  KEY idx_platform_accounts_platform (platform)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
