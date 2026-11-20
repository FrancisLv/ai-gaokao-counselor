import type {
  FormalEvidenceCitation,
  FormalReportSection,
  RunView,
} from "../api/types";

export type CandidateSection = FormalReportSection & {
  probability: string;
  tier: "冲" | "稳" | "保" | "方案";
  planCount: string;
};

export function candidateSections(run: RunView): CandidateSection[] {
  return (
    run.formalResult?.report.sections
      .filter((section) => section.sectionKey.startsWith("candidate:"))
      .map((section) => ({
        ...section,
        probability:
          section.body.match(/录取概率约\s*(\d+)%/)?.[1] ?? "—",
        tier:
          (section.body.match(/策略分档：(冲|稳|保)/)?.[1] as
            | "冲"
            | "稳"
            | "保"
            | undefined) ?? "方案",
        planCount:
          section.body.match(/招生计划数：(\d+)/)?.[1] ?? "待核验",
      })) ?? []
  );
}

export function findReportSection(
  run: RunView,
  sectionKey: string,
): FormalReportSection | undefined {
  return run.formalResult?.report.sections.find(
    (section) => section.sectionKey === sectionKey,
  );
}

export function safeSourceUrl(
  citation: FormalEvidenceCitation,
): string | null {
  if (!citation.sourceUri) return null;
  try {
    const parsed = new URL(citation.sourceUri);
    return parsed.protocol === "http:" || parsed.protocol === "https:"
      ? parsed.toString()
      : null;
  } catch {
    return null;
  }
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

export function schoolDisplayTitle(section: FormalReportSection): string {
  return section.title.replace(/^\d+\.\s*/, "");
}
