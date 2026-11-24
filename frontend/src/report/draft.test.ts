import { describe, expect, it } from "vitest";

import type { FormalReportSection, WishlistDraft } from "../api/types";
import {
  canSaveDraft,
  defaultMajorCodes,
  mergeDraftWithDefaults,
} from "./draft";

const section: FormalReportSection = {
  sectionKey: "candidate:1001|03",
  title: "1. 中南大学 03专业组",
  body: "",
  citations: [],
  universityCode: "1001",
  majorGroup: "03",
  majors: [
    {
      majorCode: "02",
      majorName: "软件工程",
      subjectReq: "首选物理，再选不限",
      duration: "四年",
      probabilityDataYears: 2,
      recommended: true,
      recommendationRank: 2,
      selectable: true,
      unavailableReason: "",
    },
    {
      majorCode: "01",
      majorName: "计算机科学与技术",
      subjectReq: "首选物理，再选不限",
      duration: "四年",
      probabilityDataYears: 2,
      recommended: true,
      recommendationRank: 1,
      selectable: true,
      unavailableReason: "",
    },
    {
      majorCode: "03",
      majorName: "受限专业",
      subjectReq: "首选物理，再选化学",
      duration: "四年",
      probabilityDataYears: 0,
      recommended: false,
      recommendationRank: 0,
      selectable: false,
      unavailableReason: "FAIL",
    },
  ],
};

const secondSection: FormalReportSection = {
  ...section,
  sectionKey: "candidate:1002|04",
  title: "2. 第二大学 04专业组",
  universityCode: "1002",
  majorGroup: "04",
  majors: [
    {
      ...section.majors![0],
      majorCode: "09",
      majorName: "第二专业",
      recommendationRank: 1,
    },
  ],
};

describe("wishlist draft defaults", () => {
  it("uses recommendation order and excludes ineligible majors", () => {
    expect(defaultMajorCodes(section)).toEqual(["01", "02"]);
  });

  it("restores only selectable saved codes and keeps default fallback", () => {
    const draft: WishlistDraft = {
      draftVersionId: "draft-1",
      resultVersion: "result-1",
      runId: "run-1",
      releaseId: "release-1",
      draftVersion: 1,
      createdAt: "2026-11-22T00:00:00Z",
      groups: [
        {
          universityCode: "1001",
          majorGroup: "03",
          majorCodes: ["03", "02", "02"],
        },
      ],
    };
    expect(mergeDraftWithDefaults([section], draft)).toEqual([
      {
        universityCode: "1001",
        majorGroup: "03",
        majorCodes: ["02"],
      },
    ]);
  });

  it("rejects empty groups, duplicates and more than six majors", () => {
    expect(canSaveDraft([])).toBe(false);
    expect(canSaveDraft([{
      universityCode: "1001",
      majorGroup: "03",
      majorCodes: [],
    }])).toBe(false);
    expect(canSaveDraft([{
      universityCode: "1001",
      majorGroup: "03",
      majorCodes: ["01", "01"],
    }])).toBe(false);
    expect(canSaveDraft([{
      universityCode: "1001",
      majorGroup: "03",
      majorCodes: ["1", "2", "3", "4", "5", "6", "7"],
    }])).toBe(false);
  });

  it("keeps only saved groups in user order and fills their defaults", () => {
    const draft: WishlistDraft = {
      draftVersionId: "draft-2",
      resultVersion: "result-1",
      runId: "run-1",
      releaseId: "release-1",
      draftVersion: 2,
      createdAt: "2026-11-23T00:00:00Z",
      groups: [
        {
          universityCode: "1002",
          majorGroup: "04",
          majorCodes: ["09"],
        },
      ],
    };
    expect(
      mergeDraftWithDefaults([section, secondSection], draft),
    ).toEqual([
      {
        universityCode: "1002",
        majorGroup: "04",
        majorCodes: ["09"],
      },
    ]);
  });
});
