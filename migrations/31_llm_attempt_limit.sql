-- M5a-03：逻辑调用最多两次供应商请求，包含备用模型切换。
-- 网关不得依赖供应商 SDK 自动重试扩展该上限。

ALTER TABLE llm_role_route
    DROP CONSTRAINT IF EXISTS llm_role_route_max_attempts_check;

ALTER TABLE llm_role_route
    ADD CONSTRAINT llm_role_route_max_attempts_check
    CHECK (max_attempts BETWEEN 1 AND 2);

ALTER TABLE llm_provider_attempt
    ADD CONSTRAINT llm_provider_attempt_max_attempt_no
    CHECK (attempt_no BETWEEN 1 AND 2);

COMMENT ON CONSTRAINT llm_role_route_max_attempts_check ON llm_role_route IS
    '单次 logical call 最多两次供应商请求；供应商 SDK 自动重试不得绕过';
