-- M4b: 规则表达式契约版本与存储资源上限。

ALTER TABLE rule_version
    ADD COLUMN IF NOT EXISTS expression_version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE rule_version
    ADD CONSTRAINT rule_version_expression_version_check
    CHECK (expression_version = 1);

ALTER TABLE rule_version
    ADD CONSTRAINT rule_version_expression_size_check
    CHECK (octet_length(expression::text) <= 32768);

COMMENT ON COLUMN rule_version.expression_version IS
    '受限 typed AST 契约版本；不允许调用方逐次覆盖';
