-- 默认 legacy release 的可用范围。
-- 历史数据导入后统一归入 rel_legacy_v1；V1 仅支持湖南本科批，
-- 物理类和历史类都需要在 fresh schema 中可直接完成画像确认。

INSERT INTO release (
    release_id,
    status,
    manifest,
    manifest_sha256,
    published_at
)
VALUES (
    'rel_legacy_v1',
    'PUBLISHED',
    '{"legacy": true}'::jsonb,
    'legacy-v1',
    NOW()
)
ON CONFLICT (release_id) DO NOTHING;

INSERT INTO release_scope (
    release_id,
    province,
    application_year,
    category,
    batch
)
VALUES
    ('rel_legacy_v1', '湖南', 2025, '物理类', '本科批'),
    ('rel_legacy_v1', '湖南', 2025, '历史类', '本科批')
ON CONFLICT (release_id, province, application_year, category, batch)
DO NOTHING;
