// svc-algo：领域算法服务（Kratos gRPC）。
//
// 职责：位次换算 / 概率估计 / 规则引擎 / 志愿表优化。
// 全是确定性计算：可复现、可审计、可独立单测。**不含 LLM 调用。**
//
// 架构（kratos 三层 + wire）：service（proto 对接）→ biz（领域算法 + Repo 接口）→ data（实现 Repo、跑 SQL）。
// 启动：加载 conf.Bootstrap（config.New + Scan）→ wireApp(&bc) 组装依赖图 → app.Run()。
package main

import (
	"flag"
	"os"
	"time"

	"github.com/go-kratos/kratos/v2"
	"github.com/go-kratos/kratos/v2/config"
	"github.com/go-kratos/kratos/v2/config/file"
	"github.com/go-kratos/kratos/v2/log"
	"github.com/go-kratos/kratos/v2/transport/grpc"

	algov1 "github.com/choucisan/ai-gaokao-counselor/api/algo/v1"
	"github.com/choucisan/ai-gaokao-counselor/conf"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/service"
)

func main() {
	confPath := flag.String("conf", "configs/algo.yaml", "服务配置文件路径")
	flag.Parse()
	// 加载配置：只读本服务自己的 algo.yaml（⚠️ 不能用 file.NewSource("configs/")——
	// 那会加载并合并目录下全部 yaml（gateway/llm-gateway），其 server.grpc.addr 会覆盖本服务端口）。
	c := config.New(config.WithSource(file.NewSource(*confPath)))
	defer c.Close()
	if err := c.Load(); err != nil {
		panic(err)
	}
	var bc conf.Bootstrap
	if err := c.Scan(&bc); err != nil {
		panic(err)
	}

	app, cleanup, err := wireApp(&bc)
	if err != nil {
		panic(err)
	}
	defer cleanup()

	if err := app.Run(); err != nil {
		panic(err)
	}
}

// newLogger 是 wire 的 log.Logger provider（main.go 内提供，见 §4.1③）。
func newLogger() log.Logger {
	return log.With(
		log.NewStdLogger(os.Stdout),
		"service", "svc-algo",
		"caller", log.Caller(4),
		"time", log.DefaultTimestamp,
	)
}

// newApp 是 kratos 启动 glue（wire.Build 的最后一个 provider，G3/Q26）。
// 职责：读 bc.Server.Grpc 建 gRPC server → 注册 AlgoServer → 组装 *kratos.App。
// 旧骨架（硬编码 :9002、调作废的 algo.NewService()）已弃用，改为从 bc 读 addr/timeout。
func newApp(bc *conf.Bootstrap, svc *service.Service, logger log.Logger) *kratos.App {
	grpcCfg := bc.GetServer().GetGrpc()

	addr := grpcCfg.GetAddr()
	if addr == "" {
		addr = "0.0.0.0:9002"
	}
	// grpc.Timeout 需 time.Duration：yaml 的 "30s" 字符串在此解析（缺省/非法兜底 30s）
	timeout, err := time.ParseDuration(grpcCfg.GetTimeout())
	if err != nil || timeout <= 0 {
		timeout = 30 * time.Second
	}

	gs := grpc.NewServer(
		grpc.Address(addr),
		grpc.Timeout(timeout),
		grpc.Logger(logger),
	)
	algov1.RegisterAlgoServer(gs, svc) // svc 实现 algov1.AlgoServer（4 个 RPC）

	return kratos.New(
		kratos.Name("svc-algo"),
		kratos.Server(gs),
		kratos.Logger(logger),
	)
}
