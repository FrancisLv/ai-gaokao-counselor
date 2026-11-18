package data

import (
	"errors"
	"strings"
	"testing"

	errs "github.com/choucisan/ai-gaokao-counselor/internal/pkg/errors"

	"github.com/choucisan/ai-gaokao-counselor/conf"
)

func FuzzParseRange(f *testing.F) {
	for _, s := range []string{"1-51", "693-750", "450", "", "2-1", "-1", "1-x", "1-2-3"} {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, s string) {
		lo, hi := parseRange(s, 1, 10)
		if lo < 0 || hi < lo {
			t.Fatalf("非法区间未完整回退: %q => %d,%d", s, lo, hi)
		}
		lo2, hi2 := parseRange(s, 1, 10)
		if lo != lo2 || hi != hi2 {
			t.Fatal("解析不确定")
		}
	})
}

func TestNewData_InvalidConfigBeforeConnect(t *testing.T) {
	t.Setenv("ALGO_DATA_DSN", "invalid")
	_, cleanup, err := NewData(&conf.Bootstrap{Algo: &conf.Algo{DecayFactor: -1}})
	if cleanup != nil {
		cleanup()
	}
	if !errors.Is(err, errs.ErrInvalidConfig) {
		t.Fatalf("应先拒绝配置再连接数据库，got %v", err)
	}
}

func TestNewData_RedactsDSN(t *testing.T) {
	for _, dsn := range []string{
		"postgres://nobody:TEST_PRIVATE_MARKER@127.0.0.1:1/no_database?connect_timeout=1",
		"postgres://nobody:TEST_PRIVATE_MARKER@localhost:invalid/no_database",
	} {
		t.Run(dsn[len(dsn)-8:], func(t *testing.T) {
			t.Setenv("ALGO_DATA_DSN", dsn)
			_, cleanup, err := NewData(&conf.Bootstrap{})
			if cleanup != nil {
				cleanup()
			}
			if err == nil {
				t.Fatal("非法连接意外成功")
			}
			if strings.Contains(err.Error(), "TEST_PRIVATE_MARKER") {
				t.Fatal("错误包含连接凭据")
			}
		})
	}
}
