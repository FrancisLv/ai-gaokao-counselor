//go:build wireinject
// +build wireinject

package main

import (
	"github.com/go-kratos/kratos/v2"
	"github.com/google/wire"

	"github.com/choucisan/ai-gaokao-counselor/conf"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/data"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/service"
)

// wireApp 声明依赖图（入参 *conf.Bootstrap）。正常运行 `wire ./cmd/algo` 生成 wire_gen.go；
// 本仓 wire_gen.go 因 wire CLI v0.6.0 无法在 Go 1.27 下编译而按 wire 语义手写（见 wire_gen.go 头注）。
// 依赖链：data.New{Data,RankRepo,AdmissionRepo} → biz.New{Rank,Recommend,Optimize}Usecase → service.NewAlgo → newApp。
func wireApp(*conf.Bootstrap) (*kratos.App, func(), error) {
	panic(wire.Build(
		data.ProviderSet,
		biz.ProviderSet,
		service.ProviderSet,
		newLogger,
		newApp,
	))
}
