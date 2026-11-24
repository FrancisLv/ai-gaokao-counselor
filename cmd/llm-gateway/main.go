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
	"net/http"
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
			Mode        string `yaml:"mode"`
			BaseURLEnv  string `yaml:"base_url_env"`
			APIKeyEnv   string `yaml:"api_key_env"`
			HTTPTimeout string `yaml:"http_timeout"`
		} `yaml:"provider"`
	} `yaml:"llm"`
}

func main() {
	configPath := flag.String("conf", "configs/llm-gateway.yaml", "服务配置文件路径")
	flag.Parse()
	config := loadConfig(*configPath)
	if mode := os.Getenv("LLM_PROVIDER_MODE"); mode != "" {
		config.LLM.Provider.Mode = mode
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
	var provider llmgw.Provider
	switch config.LLM.Provider.Mode {
	case "fail_closed":
		provider = nil
	case "deepseek":
		apiKeyEnv := config.LLM.Provider.APIKeyEnv
		if apiKeyEnv == "" {
			apiKeyEnv = "DEEPSEEK_API_KEY"
		}
		baseURLEnv := config.LLM.Provider.BaseURLEnv
		if baseURLEnv == "" {
			baseURLEnv = "DEEPSEEK_BASE_URL"
		}
		apiKey := os.Getenv(apiKeyEnv)
		if apiKey == "" {
			log.Fatalf("%s 未配置", apiKeyEnv)
		}
		baseURL := os.Getenv(baseURLEnv)
		if baseURL == "" {
			baseURL = "https://api.deepseek.com"
		}
		httpTimeout := 60 * time.Second
		if parsed, err := time.ParseDuration(config.LLM.Provider.HTTPTimeout); err == nil && parsed > 0 {
			httpTimeout = parsed
		}
		provider, err = llmgw.NewDeepSeekProvider(
			baseURL,
			apiKey,
			&http.Client{Timeout: httpTimeout},
		)
		if err != nil {
			log.Fatal("DeepSeek provider 配置不可用")
		}
	default:
		log.Fatalf("不支持的 LLM provider mode: %s", config.LLM.Provider.Mode)
	}
	llmgwv1.RegisterLLMGatewayServer(gs, llmgw.NewService(pool, provider))

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
	config.LLM.Provider.BaseURLEnv = "DEEPSEEK_BASE_URL"
	config.LLM.Provider.APIKeyEnv = "DEEPSEEK_API_KEY"
	config.LLM.Provider.HTTPTimeout = "60s"

	body, err := os.ReadFile(path)
	if err != nil {
		log.Fatalf("读取配置失败: %v", err)
	}
	if err := yaml.Unmarshal(body, &config); err != nil {
		log.Fatalf("解析配置失败: %v", err)
	}
	return config
}
