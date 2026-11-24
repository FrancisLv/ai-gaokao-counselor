-- 专业组级志愿草稿的确定性风险摘要；历史草稿允许为空并在读取时按需重算。

ALTER TABLE wishlist_draft_version
    ADD COLUMN risk_summary JSONB
    CHECK (
        risk_summary IS NULL
        OR jsonb_typeof(risk_summary) = 'object'
    );

COMMENT ON COLUMN wishlist_draft_version.risk_summary IS
    '由 svc-algo 基于当前拟填院校专业组集合计算的草稿层风险摘要；不改变正式结果';
