-- 合规复核表结构参考（PostgreSQL 语法，运行时不执行；建表由 SQLAlchemy create_all 完成）

CREATE TABLE consent_events (
    id              SERIAL PRIMARY KEY,
    event_id        VARCHAR(64)  NOT NULL UNIQUE,        -- 上游事件键，重复事件幂等忽略
    batch_no        VARCHAR(40)  NOT NULL,               -- 到达批次（每天分批）
    member_id       INTEGER      NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    channel         VARCHAR(8)   NOT NULL,               -- sms / app / all
    action          VARCHAR(16)  NOT NULL,               -- unsubscribed / opted_out / complained / subscribed
    event_time      TIMESTAMP    NOT NULL,               -- 事件实际发生时间
    received_at     TIMESTAMP    NOT NULL,               -- 到达本系统时间
    disposition     VARCHAR(16)  NOT NULL DEFAULT 'applied',  -- applied / late_ignored
    source          VARCHAR(24)  NOT NULL DEFAULT 'batch',
    note            TEXT,
    created_at      TIMESTAMP    NOT NULL
);
CREATE INDEX ix_consent_events_member_channel ON consent_events (member_id, channel);
CREATE INDEX ix_consent_events_batch ON consent_events (batch_no);

CREATE TABLE member_consents (
    id               SERIAL PRIMARY KEY,
    member_id        INTEGER      NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    channel          VARCHAR(8)   NOT NULL,
    status           VARCHAR(12)  NOT NULL DEFAULT 'sendable',  -- sendable / blocked
    last_action      VARCHAR(16)  NOT NULL,
    last_event_id    VARCHAR(64)  NOT NULL,
    last_event_time  TIMESTAMP    NOT NULL,              -- 只有更晚的事件才能改写（迟到不恢复已退订）
    updated_at       TIMESTAMP    NOT NULL,
    CONSTRAINT uq_member_consents_member_channel UNIQUE (member_id, channel)
);

CREATE TABLE audience_members (
    id               SERIAL PRIMARY KEY,
    audience_id      INTEGER      NOT NULL REFERENCES audiences(id) ON DELETE CASCADE,
    member_id        INTEGER      NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    segment_code     VARCHAR(20)  NOT NULL DEFAULT '',
    source           VARCHAR(12)  NOT NULL DEFAULT 'base',      -- base / backfill
    status           VARCHAR(12)  NOT NULL DEFAULT 'sendable',  -- sendable / excluded
    exclude_reason   VARCHAR(40),
    blocked_channels JSON,
    round_no         INTEGER      NOT NULL DEFAULT 1,
    created_at       TIMESTAMP    NOT NULL,
    updated_at       TIMESTAMP    NOT NULL,
    CONSTRAINT uq_audience_members_audience_member UNIQUE (audience_id, member_id)  -- 重试不重复补位
);
CREATE INDEX ix_audience_members_audience_status ON audience_members (audience_id, status);

CREATE TABLE audience_reviews (
    id              SERIAL PRIMARY KEY,
    audience_id     INTEGER      NOT NULL REFERENCES audiences(id) ON DELETE CASCADE,
    round_no        INTEGER      NOT NULL,
    send_channel    VARCHAR(8)   NOT NULL DEFAULT 'all',
    target_count    INTEGER      NOT NULL DEFAULT 0,
    sendable_count  INTEGER      NOT NULL DEFAULT 0,
    excluded_count  INTEGER      NOT NULL DEFAULT 0,
    backfill_count  INTEGER      NOT NULL DEFAULT 0,
    shortage        INTEGER      NOT NULL DEFAULT 0,
    reimport        BOOLEAN      NOT NULL DEFAULT FALSE,
    operator_email  VARCHAR(160) NOT NULL DEFAULT 'system',
    created_at      TIMESTAMP    NOT NULL,
    CONSTRAINT uq_audience_reviews_audience_round UNIQUE (audience_id, round_no)
);

CREATE TABLE audience_publishes (
    id              SERIAL PRIMARY KEY,
    audience_id     INTEGER      NOT NULL REFERENCES audiences(id) ON DELETE CASCADE,
    version_no      INTEGER      NOT NULL,
    is_current      BOOLEAN      NOT NULL DEFAULT FALSE,
    send_channel    VARCHAR(8)   NOT NULL DEFAULT 'all',
    sendable_count  INTEGER      NOT NULL DEFAULT 0,
    excluded_count  INTEGER      NOT NULL DEFAULT 0,
    backfill_count  INTEGER      NOT NULL DEFAULT 0,
    snapshot        JSON,                                  -- 固定可发名单与剔除原因
    note            TEXT,
    operator_email  VARCHAR(160) NOT NULL DEFAULT 'system',
    created_at      TIMESTAMP    NOT NULL,
    CONSTRAINT uq_audience_publishes_audience_version UNIQUE (audience_id, version_no)
);
CREATE INDEX ix_audience_publishes_current ON audience_publishes (audience_id, is_current);
