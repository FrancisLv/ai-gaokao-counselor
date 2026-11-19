-- M4a frontend：允许 POLICY_QA 发布无画像的不可变文档证据结果。

ALTER TABLE run_result
    ALTER COLUMN confirmed_profile_version DROP NOT NULL;

ALTER TABLE run_result
    DROP CONSTRAINT run_result_result_kind_check;

ALTER TABLE run_result
    ADD CONSTRAINT run_result_result_kind_check
    CHECK (result_kind IN ('ALGORITHM_PREVIEW', 'POLICY_EVIDENCE'));
