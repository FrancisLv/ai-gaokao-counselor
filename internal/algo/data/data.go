// Package data 实现 biz.Repo：queries 保存 SQL，dbgen 生成查询类型，仓储负责调用与领域映射。
package data

import (
	"context"
	"errors"
	"os"
	"time"

	"github.com/google/wire"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/choucisan/ai-gaokao-counselor/conf"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/biz"
	"github.com/choucisan/ai-gaokao-counselor/internal/algo/data/dbgen"
)

// ProviderSet 是 data 层的 wire provider 集。
var ProviderSet = wire.NewSet(NewData, NewAdmissionRepo, NewRankRepo, NewEligibilityRepo)

// Data 提供绑定到 pgx 连接池的类型化查询；连接池由 cleanup 统一关闭。
type Data struct {
	queries *dbgen.Queries
}

// NewData 读 bc.Data.Database.Dsn 建 pgxpool（wire 注入；返回 cleanup 关池）。
// dsn 优先级：环境变量 ALGO_DATA_DSN（若设）> yaml 配置——便于 docker/不同环境覆盖而不改仓库默认值。
// 启动即 Ping：pgxpool.New 本身惰性连接，这里显式 Ping 一次，让 dsn 不可达/配错在启动阶段
// 立即失败（fail-fast），而不是拖到首次 RPC 才报错。
func NewData(bc *conf.Bootstrap) (*Data, func(), error) {
	if err := biz.ValidateConfig(bc); err != nil {
		return nil, nil, err
	}
	dsn := bc.GetData().GetDatabase().GetDsn()
	if override := os.Getenv("ALGO_DATA_DSN"); override != "" {
		dsn = override
	}
	pool, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		return nil, nil, errors.New("数据库连接配置无效，请检查连接参数")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, nil, errors.New("数据库连接失败，请检查服务状态、网络与凭据")
	}
	cleanup := func() { pool.Close() }
	return &Data{queries: dbgen.New(pool)}, cleanup, nil
}
