-- M5a-06：六类数据分类与 route 云端准入边界。
-- 敏感资格数据与 SECRET 不在 route 的云端允许集合中，只能由上游转换为脱敏结论。

ALTER TABLE llm_role_route
    ADD COLUMN allowed_data_classes TEXT[] NOT NULL
        DEFAULT ARRAY['PUBLIC_DATA']::TEXT[];

ALTER TABLE llm_role_route
    ADD CONSTRAINT llm_role_route_allowed_data_classes_nonempty
        CHECK (cardinality(allowed_data_classes) > 0),
    ADD CONSTRAINT llm_role_route_allowed_data_classes_no_nulls
        CHECK (array_position(allowed_data_classes, NULL) IS NULL),
    ADD CONSTRAINT llm_role_route_allowed_data_classes_cloud_safe
        CHECK (allowed_data_classes <@ ARRAY[
            'PUBLIC_DATA',
            'PRINCIPAL_DATA',
            'INQUIRY_DATA',
            'OPERATIONAL_DATA'
        ]::TEXT[]);

ALTER TABLE llm_logical_call
    ADD COLUMN data_classification TEXT NOT NULL DEFAULT 'PUBLIC_DATA';

ALTER TABLE llm_logical_call
    ADD CONSTRAINT llm_logical_call_data_classification_check
        CHECK (data_classification IN (
            'PUBLIC_DATA',
            'PRINCIPAL_DATA',
            'INQUIRY_DATA',
            'SENSITIVE_ELIGIBILITY_DATA',
            'OPERATIONAL_DATA',
            'SECRET'
        ));

COMMENT ON COLUMN llm_role_route.allowed_data_classes IS
    'route 可送往云模型的数据类别子集；SENSITIVE_ELIGIBILITY_DATA 与 SECRET 禁止出现';
COMMENT ON COLUMN llm_logical_call.data_classification IS
    '规范化后的六类数据分类；旧 PUBLIC/INTERNAL/SENSITIVE 在入口显式映射';
