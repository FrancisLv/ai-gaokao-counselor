// Package obs 提供 OpenTelemetry 埋点。
//
// 目标：trace_id 贯穿 Go ↔ Python，合并到同一棵调用树（见 api/common/v1 的 Trace）。
package obs

import "context"

// Init 初始化 TracerProvider / MeterProvider，返回用于优雅退出的 shutdown。
func Init(ctx context.Context, serviceName string, otlpEndpoint string) (shutdown func(context.Context) error, err error) {
	// TODO(M5): OTel SDK + OTLP exporter；从入站请求的 Trace 字段恢复父 span。
	return func(context.Context) error { return nil }, nil
}
