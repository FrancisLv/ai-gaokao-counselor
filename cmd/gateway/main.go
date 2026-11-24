// svc-gateway：唯一对外的入口（Kratos HTTP + SSE）。
//
// 职责：HTTP/SSE 接入、鉴权、限流、会话、HITL 门控与推送。
// 它是对 svc-orchestrator 的「上游调用方」；绝不直接连接模型或算法。
package main

import (
	"context"
	"log"
	"os"
	"time"

	"github.com/go-kratos/kratos/v2"
	"github.com/go-kratos/kratos/v2/transport/http"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	gatewayv1 "github.com/choucisan/ai-gaokao-counselor/api/gateway/v1"
	orchestratorv1 "github.com/choucisan/ai-gaokao-counselor/api/orchestrator/v1"
	"github.com/choucisan/ai-gaokao-counselor/internal/gateway"
)

// 正式报告同时携带候选、专业、证据与校验详情，可能超过 gRPC 默认 4 MiB。
const maxOrchestratorResponseBytes = 16 << 20

func main() {
	orchestratorAddr := os.Getenv("ORCHESTRATOR_ENDPOINT")
	if orchestratorAddr == "" {
		orchestratorAddr = "localhost:9100"
	}
	conn, err := newOrchestratorClient(orchestratorAddr)
	if err != nil {
		log.Fatal(err)
	}
	defer conn.Close()

	dsn := os.Getenv("GATEWAY_DSN")
	var pool *pgxpool.Pool
	var identity *gateway.PostgresIdentity
	if dsn != "" {
		pool, err = pgxpool.New(context.Background(), dsn)
		if err != nil {
			log.Fatal(err)
		}
		defer pool.Close()
		identity, err = gateway.NewPostgresIdentity(pool)
		if err != nil {
			log.Fatal(err)
		}
	}

	gatewayAddr := os.Getenv("GATEWAY_ADDR")
	if gatewayAddr == "" {
		gatewayAddr = ":8000"
	}
	hs := http.NewServer(
		http.Address(gatewayAddr),
		http.Timeout(60*time.Second),
	)
	orchestratorClient := orchestratorv1.NewOrchestratorClient(conn)
	var service *gateway.Service
	if identity == nil {
		log.Print("GATEWAY_DSN 未设置：账号与私有接口不可用")
		service = gateway.NewServiceForOrchestrator(orchestratorClient)
	} else {
		service = gateway.NewServiceForOrchestratorWithIdentity(
			orchestratorClient,
			identity,
			gateway.AuthOptions{
				CookieSecure: os.Getenv("GATEWAY_COOKIE_SECURE") != "false",
			},
		)
	}
	gatewayv1.RegisterGatewayHTTPServer(
		hs,
		service,
	)
	gateway.RegisterRunEventStream(
		hs,
		service,
		gateway.EventStreamOptions{},
	)
	if identity != nil {
		go func() {
			ticker := time.NewTicker(24 * time.Hour)
			defer ticker.Stop()
			for {
				if err := service.CleanupDormantAccounts(
					context.Background(),
				); err != nil {
					log.Printf("dormant account cleanup failed: %v", err)
				}
				<-ticker.C
			}
		}()
	}

	app := kratos.New(
		kratos.Name("svc-gateway"),
		kratos.Server(hs),
	)
	if err := app.Run(); err != nil {
		log.Fatal(err)
	}
}

func newOrchestratorClient(
	target string,
	opts ...grpc.DialOption,
) (*grpc.ClientConn, error) {
	dialOptions := []grpc.DialOption{
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithDefaultCallOptions(
			grpc.MaxCallRecvMsgSize(maxOrchestratorResponseBytes),
		),
	}
	dialOptions = append(dialOptions, opts...)
	return grpc.NewClient(target, dialOptions...)
}
