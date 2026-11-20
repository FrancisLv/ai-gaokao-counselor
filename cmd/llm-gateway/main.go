// svc-llm-gateway：LLM 治理服务（Kratos gRPC）。
//
// 职责：按 role 路由模型、缓存、预算闸门、重试/降级、成本记账。
// 关键约束：
//   - 模型 key 只存在于此服务；
//   - 直连模型厂商，**绝不回调 svc-orchestrator**（否则破坏单向 DAG）。
package main

import (
	"context"
	"flag"
	"log"
	"os"
	"time"

	"github.com/go-kratos/kratos/v2"
	"github.com/go-kratos/kratos/v2/transport/grpc"
	"github.com/jackc/pgx/v5/pgxpool"
	"gopkg.in/yaml.v3"

	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
	"github.com/choucisan/ai-gaokao-counselor/internal/llmgw"
)

type bootstrap struct {
	Server struct {
		GRPC struct {
			Addr    string `yaml:"addr"`
			Timeout string `yaml:"timeout"`
		} `yaml:"grpc"`
	} `yaml:"server"`
	LLM struct {
		Database struct {
			DSNEnv string `yaml:"dsn_env"`
		} `yaml:"database"`
		Provider struct {
			Mode string `yaml:"mode"`
		} `yaml:"provider"`
	} `yaml:"llm"`
}

func main() {
	configPath := flag.String("conf", "configs/llm-gateway.yaml", "服务配置文件路径")
	flag.Parse()
	config := loadConfig(*configPath)
	if config.LLM.Provider.Mode != "fail_closed" {
		log.Fatal("M5a-01 仅允许 fail_closed 测试 provider")
	}
	dsnEnv := config.LLM.Database.DSNEnv
	if dsnEnv == "" {
		dsnEnv = "LLMGW_DSN"
	}
	dsn := os.Getenv(dsnEnv)
	if dsn == "" {
		log.Fatalf("%s 未配置", dsnEnv)
	}
	pool, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		log.Fatal("模型网关数据库配置不可用")
	}
	defer pool.Close()
	if err := pool.Ping(context.Background()); err != nil {
		log.Fatal("模型网关数据库连接不可用")
	}

	addr := config.Server.GRPC.Addr
	if addr == "" {
		addr = ":9001"
	}
	timeout := 120 * time.Second
	if parsed, err := time.ParseDuration(config.Server.GRPC.Timeout); err == nil && parsed > 0 {
		timeout = parsed
	}
	gs := grpc.NewServer(grpc.Address(addr), grpc.Timeout(timeout))
	// M5a-01 只验收受控 fake provider；未接线真实 provider 时保持 fail closed。
	llmgwv1.RegisterLLMGatewayServer(gs, llmgw.NewService(pool, nil))

	app := kratos.New(
		kratos.Name("svc-llm-gateway"),
		kratos.Server(gs),
	)
	if err := app.Run(); err != nil {
		log.Fatal(err)
	}
}

func loadConfig(path string) bootstrap {
	config := bootstrap{}
	config.Server.GRPC.Addr = ":9001"
	config.Server.GRPC.Timeout = "120s"
	config.LLM.Database.DSNEnv = "LLMGW_DSN"
	config.LLM.Provider.Mode = "fail_closed"

	body, err := os.ReadFile(path)
	if err != nil {
		log.Fatalf("读取配置失败: %v", err)
	}
	if err := yaml.Unmarshal(body, &config); err != nil {
		log.Fatalf("解析配置失败: %v", err)
	}
	return config
}
