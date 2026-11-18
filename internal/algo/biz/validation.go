package biz

import (
	"fmt"
	"math"
	"strings"

	"github.com/choucisan/ai-gaokao-counselor/conf"
	errs "github.com/choucisan/ai-gaokao-counselor/internal/pkg/errors"
)

func finite(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }

func isReselect(s string) bool {
	return s == "化学" || s == "生物" || s == "思想政治" || s == "地理"
}

func validateSubjects(subjects []string, category string) error {
	if category != "物理类" && category != "历史类" {
		return errs.ErrInvalidSubjects
	}
	if len(subjects) != 3 {
		return errs.ErrInvalidSubjects
	}
	seen := make(map[string]bool, 3)
	first := ""
	for _, subject := range subjects {
		s := normalizeSubject(subject)
		if seen[s] {
			return errs.ErrInvalidSubjects
		}
		seen[s] = true
		if s == firstPhysics || s == firstHistory {
			if first != "" {
				return errs.ErrInvalidSubjects
			}
			first = s
		} else if !isReselect(s) {
			return errs.ErrInvalidSubjects
		}
	}
	if first+"类" != category {
		return errs.ErrInvalidSubjects
	}
	return nil
}

func validateQuery(province, category string, year int) error {
	if province != "湖南" || (category != "物理类" && category != "历史类") || year < 1 || year > 9999 {
		return errs.ErrInvalidQuery
	}
	return nil
}

func validateCandidates(candidates []CandidateWithProb, requireProb bool) error {
	seen := make(map[GroupKey]bool, len(candidates))
	for _, c := range candidates {
		key := GroupKey{c.UniversityCode, c.MajorGroup}
		if strings.TrimSpace(key.UniversityCode) == "" || strings.TrimSpace(key.MajorGroup) == "" {
			return errs.ErrInvalidCandidate
		}
		if seen[key] {
			return errs.ErrDuplicateCandidate
		}
		seen[key] = true
		if !requireProb {
			continue
		}
		if !finite(c.Probability) {
			return errs.ErrInvalidProbability
		}
		if c.Probability <= 0 {
			return errs.ErrCandidateWithoutProb
		}
		if c.Probability < .01 || c.Probability > .99 {
			return errs.ErrInvalidProbability
		}
		if c.Category != CategoryRush && c.Category != CategoryStable && c.Category != CategorySafe {
			return errs.ErrInvalidCandidate
		}
	}
	return nil
}

// ValidateConfig 校验缺省合并后的有效配置；不在请求间修改配置或悄悄修正非法值。
func ValidateConfig(bc *conf.Bootstrap) error {
	r := defaultRecommendConfig(bc)
	o := defaultOptimizeParams(bc)
	u := defaultUtilityConfig(bc)
	bad := func(field string) error { return fmt.Errorf("%w: %s", errs.ErrInvalidConfig, field) }
	if r.WindowYears < 1 || r.WindowYears > 100 {
		return bad("window_years")
	}
	if !finite(r.DecayFactor) || r.DecayFactor <= 0 || r.DecayFactor > 1 {
		return bad("decay_factor")
	}
	if !finite(r.PlanAdjust) || r.PlanAdjust < 0 || r.PlanAdjust > 1 {
		return bad("plan_adjust")
	}
	if !finite(r.RushThreshold) || !finite(r.StableThreshold) || !finite(r.SafeThreshold) || r.RushThreshold < .01 || r.RushThreshold >= r.StableThreshold || r.StableThreshold >= r.SafeThreshold || r.SafeThreshold > .99 {
		return bad("thresholds")
	}
	if r.PerCategoryLimit != 0 {
		return bad("per_category_limit: 当前仅支持全量")
	}
	if err := validateOptimizeParams(o); err != nil {
		return err
	}
	if o.MinSafeCount > o.MaxWishlistSize || o.SafeGuardThreshold < r.SafeThreshold {
		return bad("safe_guard")
	}
	for _, dim := range utilityDims {
		if w := u.DefaultWeights[dim]; !finite(w) || w < 0 {
			return bad("utility_weights")
		}
	}
	return nil
}

func validateOptimizeParams(p OptimizeParams) error {
	if p.MaxWishlistSize < 0 || p.MaxWishlistSize > 1000 || p.MinSafeCount < 0 {
		return errs.ErrInvalidConfig
	}
	if !finite(p.SafeGuardThreshold) || p.SafeGuardThreshold < 0 || p.SafeGuardThreshold > .99 {
		return errs.ErrInvalidConfig
	}
	r := p.TargetRatio
	if !finite(r.Rush) || !finite(r.Stable) || !finite(r.Safe) || r.Rush < 0 || r.Stable < 0 || r.Safe < 0 || r.Rush > 1 || r.Stable > 1 || r.Safe > 1 {
		return errs.ErrInvalidConfig
	}
	sum := r.Rush + r.Stable + r.Safe
	if sum != 0 && math.Abs(sum-1) > 1e-9 {
		return errs.ErrInvalidConfig
	}
	return nil
}
