// svc-llm-gateway：LLM 治理服务（Kratos gRPC）。
//
// 职责：按 role 路由模型、缓存、预算闸门、重试/降级、成本记账。
// 关键约束：
//   - 模型 key 只存在于此服务；
//   - 直连模型厂商，**绝不回调 svc-orchestrator**（否则破坏单向 DAG）。
package main

import (
	"log"

	"github.com/go-kratos/kratos/v2"
	"github.com/go-kratos/kratos/v2/transport/grpc"

	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
	"github.com/choucisan/ai-gaokao-counselor/internal/llmgw"
)

func main() {
	gs := grpc.NewServer(grpc.Address(":9001"))
	llmgwv1.RegisterLLMGatewayServer(gs, llmgw.NewService())

	app := kratos.New(
		kratos.Name("svc-llm-gateway"),
		kratos.Server(gs),
	)
	if err := app.Run(); err != nil {
		log.Fatal(err)
	}
}
