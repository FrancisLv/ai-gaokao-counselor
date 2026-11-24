import { describe, expect, it } from "vitest";

import {
  canExportPdf,
  candidateTierGroups,
  candidateIntroduction,
  pdfExportLabel,
  pdfFilename,
  rankPercentile,
} from "./model";
import type { CandidateSection } from "./model";

describe("candidateIntroduction", () => {
  it("keeps the current-year majors and plan without repeating history facts", () => {
    const introduction = candidateIntroduction({
      body:
        "院校：中南大学 03专业组，院校代码 1001。" +
        "专业：计算机科学（080901）。" +
        "录取概率约 34%，策略分档：冲，招生计划数：12，" +
        "专业组历史录取参考：2022年：最低录取分 630 分，最低位次 6100 名；" +
        "2023年：暂无数据；2024年：最低录取分 624 分，最低位次 6798 名；" +
        "有效历史年份 2/3。样本年份为 2022、2023、2024。" +
        "概率为历史估计，不构成录取保证。" +
        "该专业组适合继续核对组内专业限制。",
    });

    expect(introduction).toBe(
      "专业：计算机科学（080901）。招生计划数：12 人。" +
        "该专业组适合继续核对组内专业限制。",
    );
    expect(introduction).not.toMatch(/2022|2023|2024|630|6100|624|6798/);
  });
});

describe("candidate tier groups", () => {
  it("shows every candidate group in ascending admission probability order", () => {
    const candidate = (
      universityCode: string,
      majorGroup: string,
      probability: number,
      finalRank: number,
    ): CandidateSection => ({
      sectionKey: `candidate:${universityCode}|${majorGroup}`,
      title: `${finalRank}. ${universityCode} ${majorGroup}专业组`,
      body: `录取概率约 ${probability}%，策略分档：冲。`,
      citations: [],
      probability: String(probability),
      probabilityValue: probability,
      tier: "冲",
      planCount: "10",
      history: [],
      universityCode,
      majorGroup,
      finalRank,
    });
    const candidates = [
      candidate("1109", "204", 41, 34),
      candidate("3702", "113", 29, 35),
      candidate("3702", "208", 29, 36),
      candidate("3702", "210", 21, 37),
      candidate("4308", "104", 22, 38),
    ];

    const rushSchools =
      candidateTierGroups(candidates).find((group) => group.tier === "冲")
        ?.schools ?? [];

    expect(rushSchools.map((school) => school.sectionKey)).toEqual([
      "candidate:3702|210",
      "candidate:4308|104",
      "candidate:3702|113",
      "candidate:3702|208",
      "candidate:1109|204",
    ]);
  });
});

describe("PDF export", () => {
  it("only allows active, non-stale formal results", () => {
    expect(canExportPdf({ resultState: "ACTIVE" })).toBe(true);
    expect(
      canExportPdf({
        resultState: "STALE",
        staleReason: "RELEASE_REVOKED",
      }),
    ).toBe(false);
    expect(
      canExportPdf({
        resultState: "ACTIVE",
        staleReason: "RELEASE_REVOKED",
      }),
    ).toBe(false);
  });

  it("saves first when the draft is missing or dirty", () => {
    expect(pdfExportLabel(false, false)).toBe("保存并导出 PDF");
    expect(pdfExportLabel(true, true)).toBe("保存并导出 PDF");
    expect(pdfExportLabel(true, false)).toBe("导出 PDF");
  });

  it("uses a stable report and draft identity in the filename", () => {
    expect(
      pdfFilename(7, new Date("2026-11-22T15:30:00Z")),
    ).toBe("知愿-湖南2025-志愿报告-draft7-2026-11-22");
  });
});

describe("rankPercentile", () => {
  it("normalizes ranks against each year's own cohort", () => {
    expect(rankPercentile(16978, 313685)).toBeCloseTo(0.054124, 6);
    expect(rankPercentile(18020, 277310)).toBeCloseTo(0.064981, 6);
    expect(rankPercentile(20503, 320192)).toBeCloseTo(0.064033, 6);
  });

  it("fails closed when rank or cohort size is invalid", () => {
    expect(rankPercentile(null, 100)).toBeNull();
    expect(rankPercentile(10, 0)).toBeNull();
    expect(rankPercentile(101, 100)).toBeNull();
  });
});
