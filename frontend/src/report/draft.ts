import type {
  FormalReportSection,
  WishlistDraft,
  WishlistDraftGroup,
} from "../api/types";

export function defaultMajorCodes(
  section: Pick<FormalReportSection, "majors">,
): string[] {
  const majors = section.majors ?? [];
  const recommended = majors
    .filter((major) => major.selectable && major.recommended)
    .sort(
      (left, right) =>
        (left.recommendationRank || Number.MAX_SAFE_INTEGER) -
          (right.recommendationRank || Number.MAX_SAFE_INTEGER) ||
        left.majorCode.localeCompare(right.majorCode),
    );
  const fallback = majors
    .filter((major) => major.selectable)
    .sort((left, right) => left.majorCode.localeCompare(right.majorCode));
  return (recommended.length ? recommended : fallback)
    .slice(0, 6)
    .map((major) => major.majorCode);
}

export function mergeDraftWithDefaults(
  sections: FormalReportSection[],
  draft: WishlistDraft | null,
): WishlistDraftGroup[] {
  const sectionsByIdentity = new Map(
    sections.map((section) => [
      `${section.universityCode ?? ""}|${section.majorGroup ?? ""}`,
      section,
    ]),
  );
  const savedGroups = draft?.groups ?? [];
  const sourceGroups = savedGroups.length
    ? savedGroups
        .map((group) => sectionsByIdentity.get(groupKey(group)))
        .filter(
          (section): section is FormalReportSection => Boolean(section),
        )
    : sections;
  const saved = new Map(
    savedGroups.map((group) => [
      `${group.universityCode}|${group.majorGroup}`,
      group,
    ]),
  );
  return sourceGroups.map((section) => {
    const universityCode = section.universityCode ?? "";
    const majorGroup = section.majorGroup ?? "";
    const allowed = new Set(
      (section.majors ?? [])
        .filter((major) => major.selectable)
        .map((major) => major.majorCode),
    );
    const candidate = saved.get(`${universityCode}|${majorGroup}`);
    const savedCodes = (candidate?.majorCodes ?? []).filter(
      (code, index, values) =>
        allowed.has(code) && values.indexOf(code) === index,
    );
    const majorCodes = savedCodes.length
      ? savedCodes.slice(0, 6)
      : defaultMajorCodes(section);
    return { universityCode, majorGroup, majorCodes };
  });
}

export function groupKey(group: WishlistDraftGroup): string {
  return `${group.universityCode}|${group.majorGroup}`;
}

export function canSaveDraft(groups: WishlistDraftGroup[]): boolean {
  if (groups.length < 1 || groups.length > 45) return false;
  const identities = new Set<string>();
  return groups.every((group) => {
    const identity = groupKey(group);
    if (identities.has(identity)) return false;
    identities.add(identity);
    return (
      group.universityCode.trim() !== "" &&
      group.majorGroup.trim() !== "" &&
      group.majorCodes.length >= 1 &&
      group.majorCodes.length <= 6 &&
      new Set(group.majorCodes).size === group.majorCodes.length
    );
  });
}
