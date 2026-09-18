-- Stage 1 schema. MariaDB 12.3, InnoDB, utf8mb4 throughout.
--
-- No embeddings table here -- that belongs to stage 3. This file holds exactly
-- what the GitHub ingestion produces and nothing speculative.

CREATE TABLE IF NOT EXISTS issues (
  -- The GitHub issue number is the natural primary key: stable, unique within
  -- the repo, and already the identifier that eval.py and fusion.py pass around
  -- as list[int]. A surrogate id would force a join on every single lookup.
  number            INT UNSIGNED  NOT NULL,
  title             TEXT          NOT NULL,
  body              MEDIUMTEXT    NULL,          -- p90 is 4.5KB, but the tail is long
  state             VARCHAR(16)   NOT NULL,      -- OPEN | CLOSED
  state_reason      VARCHAR(32)   NULL,          -- COMPLETED | NOT_PLANNED | DUPLICATE | REOPENED
  author            VARCHAR(255)  NULL,          -- NULL when the account was deleted
  url               VARCHAR(255)  NOT NULL,
  created_at        DATETIME      NOT NULL,
  updated_at        DATETIME      NOT NULL,
  closed_at         DATETIME      NULL,
  -- comment_count is the API's true total; comments_fetched is how many we
  -- actually stored. Keeping both means stage 7 can tell a complete thread from
  -- a truncated one instead of assuming it has everything.
  comment_count     INT UNSIGNED  NOT NULL DEFAULT 0,
  comments_fetched  INT UNSIGNED  NOT NULL DEFAULT 0,
  fetched_at        TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                  ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (number),
  -- Load-bearing, not an optimisation: the eval is time-aware and filters on
  -- created_at for every single retrieval call.
  KEY idx_created (created_at),
  -- Drives the --since incremental refresh.
  KEY idx_updated (updated_at),
  -- Stage 2 selects the duplicate set off this.
  KEY idx_state_reason (state_reason)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


CREATE TABLE IF NOT EXISTS comments (
  -- databaseId, not the GraphQL node id: the former is a real integer, the
  -- latter an opaque base64 string that would bloat the index for no gain.
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
  -- The label name is the key. There is no separate labels table because we
  -- never store label metadata (colour, description), so one would add a join
  -- and buy nothing. GitHub caps label names at 50 chars.
  issue_number  INT UNSIGNED  NOT NULL,
  label         VARCHAR(100)  NOT NULL,
  PRIMARY KEY (issue_number, label),
  KEY idx_label (label),
  CONSTRAINT fk_label_issue FOREIGN KEY (issue_number)
    REFERENCES issues (number) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


CREATE TABLE IF NOT EXISTS fetch_state (
  -- Deliberately key/value rather than typed columns: this holds a handful of
  -- crawl bookkeeping values (cursor, completion flags, last-fetch timestamp)
  -- and adding one more should never be a schema migration.
  k           VARCHAR(64) NOT NULL,
  v           TEXT        NULL,
  updated_at  TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP
                          ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (k)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- Stage 3. Added after the ingestion tables because embeddings are derived data:
-- dropping this table costs GPU time, never source data.
CREATE TABLE IF NOT EXISTS embeddings (
  issue_number INT UNSIGNED NOT NULL,
  -- Keyed with the issue number so vectors from the base model and the stage 5b
  -- fine-tune coexist: the ablation table needs both rows, and a fine-tune that
  -- turns out worse has to be revertible without re-encoding 85k issues.
  --
  -- CHARACTER SET ascii is load-bearing, not tidiness. MariaDB caps the primary
  -- key of a vector-indexed table at 256 bytes, and VARCHAR(64) in utf8mb4 is
  -- 256 bytes on its own -- the composite key fails with error 1071 before the
  -- INT is even counted. Model names are ASCII, so 48 ascii chars is honest and
  -- leaves room.
  model_name   VARCHAR(48) CHARACTER SET ascii NOT NULL,
  vec          VECTOR(384) NOT NULL,
  created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (issue_number, model_name),
  -- DISTANCE=cosine must match the query function exactly. A mismatch does not
  -- error, it silently falls back to a full scan -- verify with EXPLAIN.
  VECTOR INDEX (vec) DISTANCE=cosine,
  CONSTRAINT fk_emb_issue FOREIGN KEY (issue_number)
    REFERENCES issues (number) ON DELETE CASCADE
) ENGINE=InnoDB;
