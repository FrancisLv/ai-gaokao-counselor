import type {
  FormalReportSection,
  FormalResult,
  HistoricalAdmissionReference,
  RunView,
} from "../api/types";

export type CandidateHistoryRow = {
  year: number;
  minScore: number | null;
  minRank: number | null;
  totalCount: number | null;
  percentile: number | null;
};

export type CandidateSection = Omit<
  FormalReportSection,
  "history" | "probability" | "planCount"
> & {
  probability: string;
  probabilityValue: number;
  tier: "冲" | "稳" | "保" | "方案";
  planCount: string;
  history: CandidateHistoryRow[];
};

export type CandidateTier = "冲" | "稳" | "保";

export type CandidateTierGroup = {
  tier: CandidateTier;
  schools: CandidateSection[];
};

export function reportMajorRows(
  section: FormalReportSection,
): NonNullable<FormalReportSection["majors"]> {
  return [...(section.majors ?? [])].sort((left, right) => {
    const leftRank = left.recommendationRank || Number.MAX_SAFE_INTEGER;
    const rightRank = right.recommendationRank || Number.MAX_SAFE_INTEGER;
    return (
      leftRank - rightRank ||
      left.majorCode.localeCompare(right.majorCode)
    );
  });
}

function historyRows(
  history: HistoricalAdmissionReference[] | undefined,
): CandidateHistoryRow[] {
  return [...(history ?? [])]
    .sort((left, right) => left.year - right.year)
    .map((item) => ({
      year: item.year,
      minScore: item.minScore ?? null,
      minRank: item.minRank ?? null,
      totalCount: item.totalCount ?? null,
      percentile: rankPercentile(item.minRank, item.totalCount),
    }));
}

export function rankPercentile(
  rank: number | null | undefined,
  totalCount: number | null | undefined,
): number | null {
  if (
    rank === null ||
    rank === undefined ||
    totalCount === null ||
    totalCount === undefined ||
    rank <= 0 ||
    totalCount <= 0 ||
    rank > totalCount
  ) {
    return null;
  }
  return rank / totalCount;
}

export function candidateIntroduction(section: { body: string }): string {
  const major = section.body.match(/专业：(.+?)。/)?.[1]?.trim();
  const planCount = section.body.match(/招生计划数：(\d+)/)?.[1];
  const narrative =
    section.body
      .match(/概率为历史估计，不构成录取保证。\s*(.+)$/s)?.[1]
      ?.trim() ?? "";
  const parts = [
    major ? `专业：${major}。` : "",
    planCount ? `招生计划数：${planCount} 人。` : "",
    narrative,
  ].filter(Boolean);
  if (parts.length) return parts.join("");

  return section.body
    .replace(/^院校：[^。]*。/u, "")
    .replace(/录取概率约\s*\d+%/gu, "")
    .replace(/策略分档：(?:冲|稳|保)[，,]?/gu, "")
    .replace(/专业组历史录取参考：[^。]*。/gu, "")
    .replace(/有效历史年份\s*\d+\/\d+。/gu, "")
    .replace(/样本年份为[^。]*。/gu, "")
    .replace(/概率为历史估计，不构成录取保证。/gu, "")
    .replace(/[，；]\s*。/gu, "。")
    .trim();
}

export function candidateSections(run: RunView): CandidateSection[] {
  return (
    run.formalResult?.report.sections
      .filter((section) => section.sectionKey.startsWith("candidate:"))
      .map((section) => ({
        ...section,
        probability:
          section.body.match(/录取概率约\s*(\d+)%/)?.[1] ?? "—",
        probabilityValue: Number(
          section.body.match(/录取概率约\s*(\d+)%/)?.[1] ?? 101,
        ),
        tier:
          (section.body.match(/策略分档：(冲|稳|保)/)?.[1] as
            | "冲"
            | "稳"
            | "保"
            | undefined) ?? "方案",
        planCount:
          section.body.match(/招生计划数：(\d+)/)?.[1] ?? "待核验",
        history: historyRows(section.history),
      })) ?? []
  );
}

export function candidateTierGroups(
  candidates: CandidateSection[],
): CandidateTierGroup[] {
  return (["冲", "稳", "保"] as const).map((tier) => ({
    tier,
    schools: candidates
      .filter((candidate) => candidate.tier === tier)
      .sort(
        (left, right) =>
          left.probabilityValue - right.probabilityValue ||
          (left.finalRank ?? Number.MAX_SAFE_INTEGER) -
            (right.finalRank ?? Number.MAX_SAFE_INTEGER) ||
          left.sectionKey.localeCompare(right.sectionKey),
      ),
  }));
}

export function findReportSection(
  run: RunView,
  sectionKey: string,
): FormalReportSection | undefined {
  return run.formalResult?.report.sections.find(
    (section) => section.sectionKey === sectionKey,
  );
}

export function escapeHtml(value: string): string {
  return value.replace(
    /[&<>"']/g,
    (character) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[character] ?? character,
  );
}

export function schoolDisplayTitle(section: { title: string }): string {
  return section.title.replace(/^\d+\.\s*/, "");
}

export function canExportPdf(
  formal: Pick<FormalResult, "resultState" | "staleReason">,
): boolean {
  return formal.resultState === "ACTIVE" && !formal.staleReason;
}

export function pdfExportLabel(
  hasSavedDraft: boolean,
  dirty: boolean,
): string {
  return !hasSavedDraft || dirty ? "保存并导出 PDF" : "导出 PDF";
}

export function pdfFilename(draftVersion: number, now = new Date()): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((item) => item.type === type)?.value ?? "";
  return `知愿-湖南2025-志愿报告-draft${draftVersion}-${part("year")}-${part("month")}-${part("day")}`;
}
