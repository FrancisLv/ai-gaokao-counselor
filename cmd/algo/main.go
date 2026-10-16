// svc-algo：领域算法服务（Kratos gRPC）。
//
// 职责：位次换算 / 概率估计 / 规则引擎 / 志愿表优化 / 投档模拟。
// 全是确定性计算：可复现、可审计、可独立单测。**不含 LLM 调用。**
package main

import (
	"log"

	"github.com/go-kratos/kratos/v2"
	"github.com/go-kratos/kratos/v2/transport/grpc"

	algov1 "github.com/choucisan/ai-gaokao-counselor/api/algo/v1"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo"
)

func main() {
	gs := grpc.NewServer(grpc.Address(":9002"))
	algov1.RegisterAlgoServer(gs, algo.NewService())

	app := kratos.New(
		kratos.Name("svc-algo"),
		kratos.Server(gs),
	)
	if err := app.Run(); err != nil {
		log.Fatal(err)
	}
}
