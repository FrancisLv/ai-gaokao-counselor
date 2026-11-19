-- M4a review fix：release 的检索融合身份。
-- RRF(k=60) 是 runtime contract 的一部分；缺失或偏离契约时不得检索。

ALTER TABLE release
    ADD COLUMN IF NOT EXISTS rrf_k INTEGER NOT NULL DEFAULT 60;

ALTER TABLE release
    ADD CONSTRAINT release_rrf_k_contract_check
        CHECK (rrf_k = 60);

COMMENT ON COLUMN release.rrf_k IS
    'release 绑定的 RRF 平滑常数；当前 runtime contract 固定为 60';
