-- 用户删除志愿表的无内容审计；草稿版本移除后即解除父结果长期保留保护。

CREATE TABLE wishlist_deletion (
    principal_id    TEXT NOT NULL CHECK (btrim(principal_id) <> ''),
    result_version  UUID NOT NULL,
    run_id          UUID NOT NULL,
    request_id      TEXT NOT NULL CHECK (btrim(request_id) <> ''),
    deleted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (principal_id, result_version)
);

CREATE INDEX idx_wishlist_deletion_deleted_at
    ON wishlist_deletion(deleted_at DESC);

COMMENT ON TABLE wishlist_deletion IS
    '用户删除志愿表后的最小审计记录；不保存拟填专业或其他私有正文';
