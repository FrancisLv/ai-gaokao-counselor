//go:build !wireinject
// +build !wireinject

// 依赖注入装配（等价于 `wire ./cmd/algo` 的产物）。
// ⚠️ 手写维护：wire CLI v0.6.0 的 golang.org/x/tools 依赖无法在 Go 1.27 下编译（tokeninternal 数组长度错误），
// 故本文件按 wire.go 的 wire.Build 集手写，语义与 wire 生成一致；若未来 wire CLI 可用可 `wire ./cmd/algo` 覆盖。

package main

import (
	"github.com/go-kratos/kratos/v2"

	"github.com/choucisan/ai-gaokao-counselor/conf"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/data"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/service"
)

// wireApp 组装完整依赖图并返回 *kratos.App + cleanup（关连接池）。
func wireApp(bc *conf.Bootstrap) (*kratos.App, func(), error) {
	// —— data 层 ——
	dataData, cleanup, err := data.NewData(bc)
	if err != nil {
		return nil, nil, err
	}
	rankRepo := data.NewRankRepo(dataData)
	admissionRepo := data.NewAdmissionRepo(dataData)

	// —— biz 层 ——
	rankUsecase := biz.NewRankUsecase(rankRepo)
	recommendUsecase := biz.NewRecommendUsecase(admissionRepo, rankUsecase, bc)
	optimizeUsecase := biz.NewOptimizeUsecase(bc)

	// —— service 层 ——
	svc := service.NewAlgo(rankUsecase, recommendUsecase, optimizeUsecase)

	// —— app（kratos 启动 glue）——
	logger := newLogger()
	app := newApp(bc, svc, logger)

	return app, func() {
		cleanup()
	}, nil
}
