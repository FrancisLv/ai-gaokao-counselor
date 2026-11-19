package biz

import "github.com/google/wire"

// ProviderSet 是 biz 层的 wire provider 集。
// wire 依赖链：data.New{Rank,Admission}Repo → biz.New*Usecase → service.NewAlgo → newApp。
var ProviderSet = wire.NewSet(NewRankUsecase, NewRecommendUsecase, NewOptimizeUsecase, NewEligibilityUsecase)
