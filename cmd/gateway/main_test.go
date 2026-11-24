package main

import (
	"context"
	"net"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/test/bufconn"

	orchestratorv1 "github.com/choucisan/ai-gaokao-counselor/api/orchestrator/v1"
)

const oversizedFormalReportBytes = 4_579_788

type oversizedFormalReportServer struct {
	orchestratorv1.UnimplementedOrchestratorServer
}

func (oversizedFormalReportServer) GetRun(
	context.Context,
	*orchestratorv1.GetRunRequest,
) (*orchestratorv1.RunViewReply, error) {
	return &orchestratorv1.RunViewReply{
		RunId:        "run-oversized-report",
		State:        orchestratorv1.RunState_SUCCEEDED,
		Stage:        "FORMAL_READY",
		StateVersion: 5,
		FormalResult: &orchestratorv1.FormalResultSummary{
			ResultVersion: "result-oversized-report",
			ResultKind:    "V1_FORMAL",
			Report: &orchestratorv1.FormalReport{
				ReportVersion: "m5b-report-v1",
				Sections: []*orchestratorv1.FormalReportSection{
					{
						SectionKey: "plan",
						Title:      "方案概况",
						Body:       strings.Repeat("x", oversizedFormalReportBytes),
					},
				},
			},
		},
	}, nil
}

func TestGetRunAcceptsReportLargerThanDefaultGRPCLimit(t *testing.T) {
	listener := bufconn.Listen(8 << 20)
	server := grpc.NewServer()
	orchestratorv1.RegisterOrchestratorServer(
		server,
		oversizedFormalReportServer{},
	)
	go func() {
		_ = server.Serve(listener)
	}()
	t.Cleanup(server.Stop)

	conn, err := newOrchestratorClient(
		"passthrough:///bufconn",
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) {
			return listener.Dial()
		}),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = conn.Close()
	})

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	reply, err := orchestratorv1.NewOrchestratorClient(conn).GetRun(
		ctx,
		&orchestratorv1.GetRunRequest{RunId: "run-oversized-report"},
	)
	if err != nil {
		t.Fatalf("GetRun oversized report: %v", err)
	}
	if got := len(reply.GetFormalResult().GetReport().GetSections()[0].GetBody()); got != oversizedFormalReportBytes {
		t.Fatalf("report body bytes = %d", got)
	}
}
