// svc-gateway：唯一对外的入口（Kratos HTTP + SSE）。
//
// 职责：HTTP/SSE 接入、鉴权、限流、会话、HITL 门控与推送。
// 它是对 svc-orchestrator 的「上游调用方」；绝不直接连接模型或算法。
package main

import (
	"log"
	"os"

	"github.com/go-kratos/kratos/v2"
	"github.com/go-kratos/kratos/v2/transport/http"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	gatewayv1 "github.com/choucisan/ai-gaokao-counselor/api/gateway/v1"
	orchestratorv1 "github.com/choucisan/ai-gaokao-counselor/api/orchestrator/v1"
	"github.com/choucisan/ai-gaokao-counselor/internal/gateway"
)

func main() {
	orchestratorAddr := os.Getenv("ORCHESTRATOR_ENDPOINT")
	if orchestratorAddr == "" {
		orchestratorAddr = "localhost:9100"
	}
	conn, err := grpc.NewClient(
		orchestratorAddr,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		log.Fatal(err)
	}
	defer conn.Close()

	hs := http.NewServer(http.Address(":8000"))
	gatewayv1.RegisterGatewayHTTPServer(
		hs,
		gateway.NewService(orchestratorv1.NewOrchestratorClient(conn)),
	)

	app := kratos.New(
		kratos.Name("svc-gateway"),
		kratos.Server(hs),
	)
	if err := app.Run(); err != nil {
		log.Fatal(err)
	}
}
