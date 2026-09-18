-- MariaDB 12.3, InnoDB, utf8mb4 throughout. Safe to re-run.

CREATE TABLE IF NOT EXISTS issues (
  -- Issue number is the natural key: stable, and already what eval.py and
  -- fusion.py pass around. A surrogate id would add a join to every lookup.
  number            INT UNSIGNED  NOT NULL,
  title             TEXT          NOT NULL,
  body              MEDIUMTEXT    NULL,          -- p90 4.5KB, long tail
  state             VARCHAR(16)   NOT NULL,      -- OPEN | CLOSED
  state_reason      VARCHAR(32)   NULL,          -- COMPLETED | NOT_PLANNED | DUPLICATE | REOPENED
  author            VARCHAR(255)  NULL,          -- NULL if the account is gone
  url               VARCHAR(255)  NOT NULL,
  created_at        DATETIME      NOT NULL,
  updated_at        DATETIME      NOT NULL,
  closed_at         DATETIME      NULL,
  -- comment_count is the API total, comments_fetched is what we stored. Both,
  -- so stage 7 can tell a full thread from a truncated one.
  comment_count     INT UNSIGNED  NOT NULL DEFAULT 0,
  comments_fetched  INT UNSIGNED  NOT NULL DEFAULT 0,
  fetched_at        TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                  ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (number),
  -- Required, not an optimisation: every retrieval filters on created_at.
  KEY idx_created (created_at),
  KEY idx_updated (updated_at),      -- drives --since refresh
  KEY idx_state_reason (state_reason) -- stage 2 selects duplicates on this
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


CREATE TABLE IF NOT EXISTS comments (
  -- GraphQL databaseId, not the node id -- an integer rather than base64.
  id            BIGINT UNSIGNED NOT NULL,
  issue_number  INT UNSIGNED    NOT NULL,
  author        VARCHAR(255)    NULL,
  body          MEDIUMTEXT      NULL,
  created_at    DATETIME        NOT NULL,
  PRIMARY KEY (id),
  KEY idx_issue (issue_number),
  CONSTRAINT fk_comment_issue FOREIGN KEY (issue_number)
    REFERENCES issues (number) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


CREATE TABLE IF NOT EXISTS issue_labels (
  -- The label name is the key. No separate labels table: we never store label
  -- colour or description, so it would add a join and buy nothing.
  issue_number  INT UNSIGNED  NOT NULL,
  label         VARCHAR(100)  NOT NULL,
  PRIMARY KEY (issue_number, label),
  KEY idx_label (label),
  CONSTRAINT fk_label_issue FOREIGN KEY (issue_number)
    REFERENCES issues (number) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


CREATE TABLE IF NOT EXISTS fetch_state (
  -- Key/value on purpose: a handful of crawl bookkeeping values, and adding
  -- one more should not need a migration.
  k           VARCHAR(64) NOT NULL,
  v           TEXT        NULL,
  updated_at  TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP
                          ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (k)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- Stage 3. Derived data -- dropping this costs GPU time, never source data.
CREATE TABLE IF NOT EXISTS embeddings (
  issue_number INT UNSIGNED NOT NULL,
  -- In the key so base and fine-tuned vectors can coexist for the ablation.
  --
  -- ascii is required, not tidiness: MariaDB caps the primary key of a
  -- vector-indexed table at 256 bytes, and VARCHAR(64) utf8mb4 is 256 bytes on
  -- its own -- error 1071 before the INT is counted.
  model_name   VARCHAR(48) CHARACTER SET ascii NOT NULL,
  vec          VECTOR(384) NOT NULL,
  created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (issue_number, model_name),
  -- Must match the query function. A mismatch silently full-scans instead of
  -- erroring -- check with EXPLAIN.
  VECTOR INDEX (vec) DISTANCE=cosine,
  CONSTRAINT fk_emb_issue FOREIGN KEY (issue_number)
    REFERENCES issues (number) ON DELETE CASCADE
) ENGINE=InnoDB;
