//go:build integration && !wireinject

package main

import (
	"context"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	algov1 "github.com/choucisan/ai-gaokao-counselor/api/algo/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

// 仅启动并清理本测试构建的子进程，不碰现有服务或数据库内容。
func TestIntegration_TCP(t *testing.T) {
	dsn := os.Getenv("ALGO_TEST_DSN")
	if dsn == "" {
		t.Skip("ALGO_TEST_DSN 未设置")
	}
	root, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	binary := filepath.Join(dir, "algo")
	build := exec.Command("go", "build", "-o", binary, "./cmd/algo")
	build.Dir = root
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build: %v %s", err, out)
	}
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := lis.Addr().String()
	lis.Close()
	config := filepath.Join(dir, "algo.yaml")
	// DSN 只经环境变量传入，不写入日志或临时配置。
	yaml := fmt.Sprintf("server:\n  grpc:\n    addr: %s\n    timeout: 30s\nalgo:\n  max_wishlist_size: 12\n  min_safe_count: 3\n", strconv.Quote(addr))
	if err := os.WriteFile(config, []byte(yaml), 0600); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(binary, "-conf", config)
	for _, env := range os.Environ() {
		if !strings.HasPrefix(env, "ALGO_DATA_DSN=") {
			cmd.Env = append(cmd.Env, env)
		}
	}
	cmd.Env = append(cmd.Env, "ALGO_DATA_DSN="+dsn)
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	t.Cleanup(func() {
		_ = cmd.Process.Signal(os.Interrupt)
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			_ = cmd.Process.Kill()
			<-done
			t.Error("子进程未优雅退出")
		}
	})
	conn, err := grpc.NewClient(addr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	client := algov1.NewAlgoClient(conn)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	rank, err := client.RankConvert(ctx, &algov1.RankConvertRequest{Province: "湖南", Category: "物理类", Year: "2025", Score: 600}, grpc.WaitForReady(true))
	if err != nil || rank.GetRank() != 15860 {
		t.Fatalf("TCP RankConvert: %v %v", rank, err)
	}
	started := time.Now()
	estimated, err := client.EstimateProb(ctx, &algov1.EstimateProbRequest{Province: "湖南", Category: "物理类", Year: "2025", Score: 600, Subjects: []string{"物理", "化学", "生物"}})
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("首次应用 EstimateProb TCP 耗时=%s（不代表数据库冷缓存）", time.Since(started))
	all := append(append(append([]*algov1.CandidateWithProb{}, estimated.RushCandidates...), estimated.StableCandidates...), estimated.SafeCandidates...)
	checked, err := client.CheckConstraints(ctx, &algov1.CheckConstraintsRequest{Candidates: all, Subjects: []string{"物理", "化学", "生物"}, Category: "物理类"})
	if err != nil || len(checked.GetKept()) != len(all) {
		t.Fatalf("TCP CheckConstraints: %v", err)
	}
	optimized, err := client.OptimizeWishlist(ctx, &algov1.OptimizeWishlistRequest{Candidates: checked.GetKept()})
	if err != nil || len(optimized.GetWishlist().GetCandidates()) != 12 {
		t.Fatalf("TCP OptimizeWishlist/配置未生效: %v", err)
	}
	t.Logf("Go TCP 四 RPC 通过，候选=%d；YAML 上限12已生效", len(all))
	if python := os.Getenv("ALGO_PYTHON"); python != "" {
		pyCtx, pyCancel := context.WithTimeout(context.Background(), 180*time.Second)
		defer pyCancel()
		run := exec.CommandContext(pyCtx, python, filepath.Join(root, "services/orchestrator/tests/smoke_algo.py"), addr)
		run.Env = append(os.Environ(), "PYTHONPATH="+filepath.Join(root, "services/orchestrator")+string(os.PathListSeparator)+filepath.Join(root, "services/orchestrator/pb"))
		if out, err := run.CombinedOutput(); err != nil {
			t.Fatalf("Python TCP: %v\n%s", err, out)
		} else {
			t.Log(string(out))
		}
	} else {
		t.Log("未设置 ALGO_PYTHON，本次仅验证 Go TCP；make tcp-smoke 强制跨语言")
	}
}
