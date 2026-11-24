-- 服务端对话记录：一个 session 内含完整消息时间线；run 继续作为执行与幂等单位。

ALTER TABLE chat_session
    ADD COLUMN IF NOT EXISTS title TEXT;

CREATE TABLE chat_message (
    message_id        UUID PRIMARY KEY,
    message_seq       BIGSERIAL NOT NULL UNIQUE,
    session_id        UUID NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE,
    run_id            UUID REFERENCES run(run_id) ON DELETE SET NULL,
    client_message_id TEXT NOT NULL
                      CHECK (btrim(client_message_id) <> ''),
    role              TEXT NOT NULL
                      CHECK (role IN ('USER', 'ASSISTANT', 'SYSTEM')),
    kind              TEXT NOT NULL DEFAULT 'TEXT'
                      CHECK (kind IN ('TEXT', 'PROGRESS', 'RESULT', 'ERROR')),
    content           TEXT NOT NULL
                      CHECK (btrim(content) <> '' AND length(content) <= 8000),
    metadata          JSONB NOT NULL DEFAULT '{}'
                      CHECK (jsonb_typeof(metadata) = 'object'),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (session_id, client_message_id)
);

CREATE INDEX idx_chat_message_session_seq
    ON chat_message(session_id, message_seq);

-- 已有联调记录没有消息正文，使用最近一次生成画像重建可识别的标题。
UPDATE chat_session AS session
SET title = latest.title
FROM (
    SELECT DISTINCT ON (r.session_id)
           r.session_id,
           left(
               concat(
                   r.task_input ->> 'category',
                   ' ',
                   r.task_input ->> 'score',
                   ' 分志愿方案'
               ),
               200
           ) AS title
    FROM run r
    WHERE r.task_kind IN ('GENERATE_PLAN', 'REVISE_PLAN')
      AND r.task_input ->> 'score' IS NOT NULL
    ORDER BY r.session_id, r.created_at DESC
) AS latest
WHERE session.id = latest.session_id
  AND (session.title IS NULL OR btrim(session.title) = '');

ALTER TABLE run_result
    DROP CONSTRAINT IF EXISTS run_result_result_kind_check;
ALTER TABLE run_result
    ADD CONSTRAINT run_result_result_kind_check
    CHECK (
        result_kind IN (
            'ALGORITHM_PREVIEW',
            'POLICY_EVIDENCE',
            'V1_FORMAL',
            'EXPLAIN_RESULT'
        )
    );

ALTER TABLE run_result
    DROP CONSTRAINT IF EXISTS run_result_kind_profile_check;
ALTER TABLE run_result
    ADD CONSTRAINT run_result_kind_profile_check
    CHECK (
        (
            result_kind IN (
                'ALGORITHM_PREVIEW',
                'V1_FORMAL',
                'EXPLAIN_RESULT'
            )
            AND confirmed_profile_version IS NOT NULL
        )
        OR (
            result_kind = 'POLICY_EVIDENCE'
            AND confirmed_profile_version IS NULL
        )
    );
