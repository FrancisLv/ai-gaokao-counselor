package biz

import "strings"

// FilterCandidatesByProfile applies explicit, user-requested hard filters.
// Preferences such as PreferredCities remain soft utility inputs; only the
// Required*/Excluded* sets change candidate identities or visible majors.
func FilterCandidatesByProfile(candidates []Candidate, profile UserProfile) []Candidate {
	if len(candidates) == 0 {
		return nil
	}
	filtered := make([]Candidate, 0, len(candidates))
	for _, candidate := range candidates {
		if len(profile.RequiredProvinces) > 0 &&
			!matchesAny(candidate.Province, profile.RequiredProvinces) {
			continue
		}
		if matchesAny(candidate.Province, profile.ExcludedProvinces) {
			continue
		}
		if !matchesAny(candidate.Location, profile.RequiredCities) &&
			len(profile.RequiredCities) > 0 {
			continue
		}
		if matchesAny(candidate.Location, profile.ExcludedCities) {
			continue
		}
		if len(profile.RequiredTiers) > 0 &&
			!containsNormalized(profile.RequiredTiers, candidate.SchoolTier) {
			continue
		}
		if matchesAny(candidate.UniversityName, profile.ExcludedUniversities) {
			continue
		}

		if len(profile.RequiredMajorNames) > 0 ||
			len(profile.RequiredMajorKeywords) > 0 ||
			len(profile.ExcludedMajorKeywords) > 0 ||
			len(profile.ExcludedMajorNames) > 0 {
			majors := filterMajors(candidate.Majors, profile)
			if len(majors) == 0 {
				continue
			}
			candidate.Majors = majors
		}
		filtered = append(filtered, candidate)
	}
	return filtered
}

func filterMajors(
	majors []MajorDetail,
	profile UserProfile,
) []MajorDetail {
	filtered := make([]MajorDetail, 0, len(majors))
	for _, major := range majors {
		if len(profile.RequiredMajorNames) > 0 &&
			!containsNormalized(profile.RequiredMajorNames, major.MajorName) {
			continue
		}
		if len(profile.RequiredMajorKeywords) > 0 &&
			!matchesAny(major.MajorName, profile.RequiredMajorKeywords) {
			continue
		}
		if matchesAny(major.MajorName, profile.ExcludedMajorKeywords) {
			continue
		}
		if containsNormalized(profile.ExcludedMajorNames, major.MajorName) {
			continue
		}
		filtered = append(filtered, major)
	}
	return filtered
}

func matchesAny(value string, needles []string) bool {
	for _, needle := range needles {
		needle = strings.TrimSpace(needle)
		if needle == "" {
			continue
		}
		if strings.Contains(value, needle) || strings.Contains(needle, value) {
			return true
		}
	}
	return false
}

func containsNormalized(values []string, target string) bool {
	target = strings.TrimSpace(target)
	for _, value := range values {
		if strings.TrimSpace(value) == target {
			return true
		}
	}
	return false
}
