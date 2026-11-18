package biz

import "github.com/choucisan/ai-gaokao-counselor/conf"

// 本文件把 conf.Bootstrap.Algo（yaml 外部化的算法常量）映射为 biz 层配置 struct，
// 由各 usecase 构造函数在 wire 注入时调用（§4.1②）。proto3 标量缺省为 0，
// 对"0 即非法"的参数做兜底默认；per_category_limit 的 0=全量为合法值，不兜底。

func i32Or(v int32, def int) int {
	if v == 0 {
		return def
	}
	return int(v)
}

func f64Or(v float64, def float64) float64 {
	if v == 0 {
		return def
	}
	return v
}

// defaultRecommendConfig 从 bc.Algo 构造概率/分档默认配置（RecommendUsecase 持有）。
func defaultRecommendConfig(bc *conf.Bootstrap) RecommendConfig {
	a := bc.GetAlgo()
	return RecommendConfig{
		WindowYears:      i32Or(a.GetWindowYears(), 3),
		DecayFactor:      f64Or(a.GetDecayFactor(), 0.7),
		PlanAdjust:       f64Or(a.GetPlanAdjust(), 0.5),
		RushThreshold:    f64Or(a.GetRushThreshold(), 0.2),
		StableThreshold:  f64Or(a.GetStableThreshold(), 0.5),
		SafeThreshold:    f64Or(a.GetSafeThreshold(), 0.8),
		PerCategoryLimit: int(a.GetPerCategoryLimit()), // 0=全量（合法值，不兜底）
		// PreferCities 是每请求用户偏好（UserProfile），不在此静态构造。
	}
}

// defaultOptimizeParams 从 bc.Algo 构造志愿表优化默认参数（OptimizeUsecase 持有）。
func defaultOptimizeParams(bc *conf.Bootstrap) OptimizeParams {
	a := bc.GetAlgo()
	r := a.GetTargetRatio()
	return OptimizeParams{
		MaxWishlistSize:    i32Or(a.GetMaxWishlistSize(), 45),
		MinSafeCount:       i32Or(a.GetMinSafeCount(), 9),
		SafeGuardThreshold: f64Or(a.GetSafeGuardThreshold(), 0.9),
		TargetRatio: RatioConfig{
			Rush:   f64Or(r.GetRush(), 0.3),
			Stable: f64Or(r.GetStable(), 0.4),
			Safe:   f64Or(r.GetSafe(), 0.3),
		},
	}
}

// defaultUtilityConfig 从 bc.Algo.utility_weights 构造效用打分默认权重（OptimizeUsecase 持有，Q19）。
// 缺失某维兜底 0.25，不强制 Σ=1。
func defaultUtilityConfig(bc *conf.Bootstrap) UtilityConfig {
	w := bc.GetAlgo().GetUtilityWeights()
	return UtilityConfig{
		DefaultWeights: map[string]float64{
			"tier":     f64Or(w.GetTier(), 0.25),
			"major":    f64Or(w.GetMajor(), 0.25),
			"location": f64Or(w.GetLocation(), 0.25),
			"strength": f64Or(w.GetStrength(), 0.25),
		},
	}
}
