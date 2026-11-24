-- Release-bound province presets for resolving macro-region expressions.

CREATE TABLE region_preset_catalog (
    release_id       TEXT PRIMARY KEY
                     REFERENCES release(release_id) ON DELETE CASCADE,
    catalog_version  TEXT NOT NULL,
    provinces        JSONB NOT NULL
                     CHECK (jsonb_typeof(provinces) = 'array'),
    presets          JSONB NOT NULL
                     CHECK (jsonb_typeof(presets) = 'array'),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION ensure_region_preset_catalog(p_release_id TEXT)
RETURNS VOID
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO region_preset_catalog (
        release_id, catalog_version, provinces, presets
    )
    VALUES (
        p_release_id,
        'region-seven-v1',
        '[
          "北京","天津","河北","山西","内蒙古","辽宁","吉林","黑龙江",
          "上海","江苏","浙江","安徽","福建","江西","山东","河南",
          "湖北","湖南","广东","广西","海南","重庆","四川","贵州",
          "云南","西藏","陕西","甘肃","青海","宁夏","新疆"
        ]'::jsonb,
        '[
          {"id":"northeast","name":"东北","provinces":["辽宁","吉林","黑龙江"]},
          {"id":"north_china","name":"华北","provinces":["北京","天津","河北","山西","内蒙古"]},
          {"id":"east_china","name":"华东","provinces":["上海","江苏","浙江","安徽","福建","江西","山东"]},
          {"id":"central_china","name":"华中","provinces":["河南","湖北","湖南"]},
          {"id":"south_china","name":"华南","provinces":["广东","广西","海南"]},
          {"id":"southwest_china","name":"西南","provinces":["重庆","四川","贵州","云南","西藏"]},
          {"id":"northwest_china","name":"西北","provinces":["陕西","甘肃","青海","宁夏","新疆"]}
        ]'::jsonb
    )
    ON CONFLICT (release_id) DO NOTHING;
END;
$$;

SELECT ensure_region_preset_catalog(release_id)
FROM release;

CREATE OR REPLACE FUNCTION reject_region_preset_catalog_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'region_preset_catalog is immutable';
END;
$$;

CREATE TRIGGER region_preset_catalog_immutable
BEFORE UPDATE ON region_preset_catalog
FOR EACH ROW EXECUTE FUNCTION reject_region_preset_catalog_mutation();
