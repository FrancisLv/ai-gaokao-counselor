package gateway

import (
	"context"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"

	gatewayv1 "github.com/choucisan/ai-gaokao-counselor/api/gateway/v1"
	orchestratorv1 "github.com/choucisan/ai-gaokao-counselor/api/orchestrator/v1"
)

type fakeRunClient struct {
	submitReq  *orchestratorv1.SubmitRunRequest
	resumeReq  *orchestratorv1.ResumeRunRequest
	submitMeta metadata.MD
	resumeMeta metadata.MD
}

func (f *fakeRunClient) SubmitRun(
	ctx context.Context,
	req *orchestratorv1.SubmitRunRequest,
	_ ...grpc.CallOption,
) (*orchestratorv1.RunReceiptReply, error) {
	f.submitReq = req
	f.submitMeta, _ = metadata.FromOutgoingContext(ctx)
	return &orchestratorv1.RunReceiptReply{
		Accepted:  true,
		RunId:     "00000000-0000-0000-0000-000000000001",
		SessionId: "00000000-0000-0000-0000-000000000002",
		State:     orchestratorv1.RunState_QUEUED,
		Stage:     "INTAKE",
	}, nil
}

func (f *fakeRunClient) ResumeRun(
	ctx context.Context,
	req *orchestratorv1.ResumeRunRequest,
	_ ...grpc.CallOption,
) (*orchestratorv1.RunReceiptReply, error) {
	f.resumeReq = req
	f.resumeMeta, _ = metadata.FromOutgoingContext(ctx)
	return &orchestratorv1.RunReceiptReply{
		Accepted:  true,
		RunId:     req.RunId,
		SessionId: "00000000-0000-0000-0000-000000000002",
		State:     orchestratorv1.RunState_QUEUED,
		Stage:     "PROFILE_CONFIRMED",
	}, nil
}

func trustedContext(principal string) context.Context {
	return metadata.NewIncomingContext(
		context.Background(),
		metadata.Pairs("x-principal-id", principal),
	)
}

func TestCreatePlanUsesRunLifecycleSubmit(t *testing.T) {
	client := &fakeRunClient{}
	svc := NewService(client)
	ctx := trustedContext("principal-a")

	reply, err := svc.CreatePlan(ctx, &gatewayv1.CreatePlanRequest{
		UserText: `{
			"task_kind":"GENERATE_PLAN",
			"profile_input":{
				"province":"湖南",
				"application_year":2025,
				"category":"物理类",
				"batch":"本科批",
				"score":600,
				"subjects":["物理","化学","生物"]
			},
			"idempotency_key":"gateway-submit"
		}`,
	})
	if err != nil {
		t.Fatal(err)
	}
	if client.submitReq == nil {
		t.Fatal("CreatePlan 未调用提交命令")
	}
	if client.submitReq.GetProfileInput().GetCategory() != "物理类" {
		t.Fatalf("category = %q", client.submitReq.GetProfileInput().GetCategory())
	}
	if got := client.submitMeta.Get("x-principal-id"); len(got) != 1 || got[0] != "principal-a" {
		t.Fatalf("principal metadata = %v", got)
	}
	if reply.GetSessionId() != client.submitReq.GetTrace().GetSessionId() &&
		reply.GetSessionId() != "00000000-0000-0000-0000-000000000002" {
		t.Fatalf("session_id = %q", reply.GetSessionId())
	}
	if reply.GetStatus() == "" {
		t.Fatal("CreatePlan 未返回状态")
	}
}

func TestConfirmProfileUsesRunLifecycleResume(t *testing.T) {
	client := &fakeRunClient{}
	svc := NewService(client)
	ctx := trustedContext("principal-a")

	_, err := svc.ConfirmProfile(ctx, &gatewayv1.ConfirmProfileRequest{
		SessionId: "00000000-0000-0000-0000-000000000002",
		ProfileJson: `{
			"run_id":"00000000-0000-0000-0000-000000000001",
			"interrupt_id":"00000000-0000-0000-0000-000000000003",
			"expected_state_version":4,
			"profile_version":"00000000-0000-0000-0000-000000000004",
			"idempotency_key":"gateway-confirm",
			"answer":{"type":"CONFIRM_PROFILE"}
		}`,
	})
	if err != nil {
		t.Fatal(err)
	}
	if client.resumeReq == nil {
		t.Fatal("ConfirmProfile 未调用 ResumeRun")
	}
	if client.resumeReq.GetProfileVersion() != "00000000-0000-0000-0000-000000000004" {
		t.Fatalf("profile_version = %q", client.resumeReq.GetProfileVersion())
	}
	if client.resumeReq.GetConfirmProfile() == nil {
		t.Fatal("ConfirmProfile 未传递强类型确认回答")
	}
	if got := client.resumeMeta.Get("x-principal-id"); len(got) != 1 || got[0] != "principal-a" {
		t.Fatalf("principal metadata = %v", got)
	}
}
