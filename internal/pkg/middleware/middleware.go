// Package middleware 提供 Kratos 中间件：recovery / tracing / ratelimit / auth。
package middleware

import "github.com/go-kratos/kratos/v2/middleware"

// Default 返回一组默认中间件（顺序即执行顺序）。
func Default() []middleware.Middleware {
	// TODO(M4): recovery → tracing → ratelimit → auth（按需）。
	return nil
}
