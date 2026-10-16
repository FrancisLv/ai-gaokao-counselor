// Package store 提供 PG / Redis 访问层（三个 Go 角色共享的基建）。
//
// 注意：本包只放「基础设施」——不含任何跨服务的业务逻辑。
package store

import "context"

// Store 汇总数据访问入口。
type Store struct {
	// TODO: PG（pgxpool，含 pgvector / tsvector）、Redis（go-redis）
}

// New 建立连接并做健康检查。
func New(ctx context.Context, pgDSN, redisAddr string) (*Store, error) {
	// TODO(M1): 连接 PostgreSQL（pgvector + tsvector）与 Redis。
	return &Store{}, nil
}

// Close 释放连接。
func (s *Store) Close() error {
	// TODO
	return nil
}
