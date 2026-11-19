package biz

import (
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"sort"
	"strconv"
	"strings"
)

// EligibilityProfileSnapshotSHA256 binds a confirmed profile version to a
// deterministic, cross-language representation of its eligibility facts.
func EligibilityProfileSnapshotSHA256(version string, facts []ProfileFact) string {
	lines := make([]string, 0, len(facts)+1)
	for _, fact := range facts {
		encodedValues := make([]string, 0, len(fact.StringValues))
		for _, value := range fact.StringValues {
			encodedValues = append(encodedValues, encodeProfileDigestString(value))
		}
		lines = append(lines, strings.Join([]string{
			encodeProfileDigestString(fact.FieldKey),
			fact.State,
			encodeProfileDigestString(fact.StringValue),
			strconv.Itoa(fact.IntValue),
			strings.Join(encodedValues, ","),
		}, "|"))
	}
	sort.Strings(lines)
	payload := encodeProfileDigestString(version) + "\n" + strings.Join(lines, "\n") + "\n"
	sum := sha256.Sum256([]byte(payload))
	return hex.EncodeToString(sum[:])
}

func profileSnapshotMatches(version, expected string, facts []ProfileFact) bool {
	actual := EligibilityProfileSnapshotSHA256(version, facts)
	return len(expected) == len(actual) &&
		subtle.ConstantTimeCompare([]byte(expected), []byte(actual)) == 1
}

func encodeProfileDigestString(value string) string {
	return base64.RawURLEncoding.EncodeToString([]byte(value))
}

func duplicateProfileFactField(facts []ProfileFact) bool {
	seen := make(map[string]struct{}, len(facts))
	for _, fact := range facts {
		if _, ok := seen[fact.FieldKey]; ok {
			return true
		}
		seen[fact.FieldKey] = struct{}{}
	}
	return false
}
