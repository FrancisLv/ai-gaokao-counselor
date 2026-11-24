package gateway

import (
	"context"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/types/known/timestamppb"

	gatewayv1 "github.com/choucisan/ai-gaokao-counselor/api/gateway/v1"
	orchestratorv1 "github.com/choucisan/ai-gaokao-counselor/api/orchestrator/v1"
)

type fakeRunClient struct {
	submitReq            *orchestratorv1.SubmitRunRequest
	resumeReq            *orchestratorv1.ResumeRunRequest
	resumeKeys           []string
	getReq               *orchestratorv1.GetRunRequest
	cancelReq            *orchestratorv1.CancelRunRequest
	listReq              *orchestratorv1.ListSessionsRequest
	sessionReq           *orchestratorv1.GetSessionRequest
	messageReq           *orchestratorv1.SubmitConversationMessageRequest
	draftReq             *orchestratorv1.SaveWishlistDraftRequest
	listWishlistsReq     *orchestratorv1.ListWishlistsRequest
	historyReq           *orchestratorv1.GetWishlistDraftRequest
	restoreReq           *orchestratorv1.RestoreWishlistDraftVersionRequest
	deleteWishlistReq    *orchestratorv1.DeleteWishlistRequest
	deletePrincipalReq   *orchestratorv1.DeletePrincipalDataRequest
	deletePrincipalReply *orchestratorv1.DeletePrincipalDataReply
	revokeConsentReq     *orchestratorv1.RevokeSensitiveConsentRequest
	watchReq             *orchestratorv1.WatchRunRequest
	watchEvents          []*orchestratorv1.RunEvent
	watchDelay           time.Duration
	cancelReply          *orchestratorv1.RunReceiptReply
	getReply             *orchestratorv1.RunViewReply
	submitMeta           metadata.MD
	resumeMeta           metadata.MD
	getMeta              metadata.MD
	cancelMeta           metadata.MD
	watchMeta            metadata.MD
	sessionMeta          metadata.MD
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
	f.resumeKeys = append(f.resumeKeys, req.GetIdempotencyKey())
	f.resumeMeta, _ = metadata.FromOutgoingContext(ctx)
	return &orchestratorv1.RunReceiptReply{
		Accepted:  true,
		RunId:     req.RunId,
		SessionId: "00000000-0000-0000-0000-000000000002",
		State:     orchestratorv1.RunState_QUEUED,
		Stage:     "PROFILE_CONFIRMED",
	}, nil
}

func (f *fakeRunClient) GetRun(
	ctx context.Context,
	req *orchestratorv1.GetRunRequest,
	_ ...grpc.CallOption,
) (*orchestratorv1.RunViewReply, error) {
	f.getReq = req
	f.getMeta, _ = metadata.FromOutgoingContext(ctx)
	if f.getReply != nil {
		return f.getReply, nil
	}
	return &orchestratorv1.RunViewReply{
		RunId:        req.GetRunId(),
		SessionId:    "00000000-0000-0000-0000-000000000002",
		State:        orchestratorv1.RunState_WAITING_HITL,
		Stage:        "PROFILE_CONFIRM",
		StateVersion: 3,
		ReleaseId:    "rel_legacy_v1",
		CurrentInterrupt: &orchestratorv1.InterruptView{
			InterruptId:    "00000000-0000-0000-0000-000000000003",
			Kind:           "PROFILE_CONFIRM",
			State:          "OPEN",
			ProfileVersion: "00000000-0000-0000-0000-000000000004",
		},
		ResultPreview: &orchestratorv1.AlgorithmPreview{
			ResultKind: "ALGORITHM_PREVIEW",
		},
		ExpiresAt: timestamppb.Now(),
	}, nil
}

func (f *fakeRunClient) CancelRun(
	ctx context.Context,
	req *orchestratorv1.CancelRunRequest,
	_ ...grpc.CallOption,
) (*orchestratorv1.RunReceiptReply, error) {
	f.cancelReq = req
	f.cancelMeta, _ = metadata.FromOutgoingContext(ctx)
	if f.cancelReply != nil {
		return f.cancelReply, nil
	}
	return &orchestratorv1.RunReceiptReply{
		Accepted:  true,
		RunId:     req.GetRunId(),
		SessionId: "00000000-0000-0000-0000-000000000002",
		State:     orchestratorv1.RunState_CANCELLED,
		Stage:     "CANCELLED",
	}, nil
}

func (f *fakeRunClient) WatchRun(
	ctx context.Context,
	req *orchestratorv1.WatchRunRequest,
	_ ...grpc.CallOption,
) (runEventStream, error) {
	f.watchReq = req
	f.watchMeta, _ = metadata.FromOutgoingContext(ctx)
	return &fakeRunEventStream{
		events: f.watchEvents,
		delay:  f.watchDelay,
	}, nil
}

func (f *fakeRunClient) ListSessions(
	ctx context.Context,
	req *orchestratorv1.ListSessionsRequest,
	_ ...grpc.CallOption,
) (*orchestratorv1.ListSessionsReply, error) {
	f.listReq = req
	f.sessionMeta, _ = metadata.FromOutgoingContext(ctx)
	return &orchestratorv1.ListSessionsReply{
		Sessions: []*orchestratorv1.ChatSessionSummary{
			{
				SessionId: "00000000-0000-0000-0000-000000000002",
				Title:     "物理类 612 分志愿方案",
			},
		},
	}, nil
}

func (f *fakeRunClient) GetSession(
	ctx context.Context,
	req *orchestratorv1.GetSessionRequest,
	_ ...grpc.CallOption,
) (*orchestratorv1.ChatSessionReply, error) {
	f.sessionReq = req
	f.sessionMeta, _ = metadata.FromOutgoingContext(ctx)
	return &orchestratorv1.ChatSessionReply{
		Summary: &orchestratorv1.ChatSessionSummary{
			SessionId: req.GetSessionId(),
			Title:     "物理类 612 分志愿方案",
		},
		Messages: []*orchestratorv1.ChatMessageView{
			{
				MessageId: "00000000-0000-0000-0000-000000000004",
				Role:      "USER",
				Kind:      "TEXT",
				Content:   "不要武汉",
			},
		},
	}, nil
}

func (f *fakeRunClient) SubmitConversationMessage(
	ctx context.Context,
	req *orchestratorv1.SubmitConversationMessageRequest,
	_ ...grpc.CallOption,
) (*orchestratorv1.SubmitConversationMessageReply, error) {
	f.messageReq = req
	f.sessionMeta, _ = metadata.FromOutgoingContext(ctx)
	return &orchestratorv1.SubmitConversationMessageReply{
		SessionId:        req.GetSessionId(),
		RunId:            "00000000-0000-0000-0000-000000000005",
		TaskKind:         "REVISE_PLAN",
		Status:           "accepted",
		AssistantMessage: "已收到筛选条件",
	}, nil
}

func (f *fakeRunClient) GetWishlistDraft(
	ctx context.Context,
	req *orchestratorv1.GetWishlistDraftRequest,
	_ ...grpc.CallOption,
) (*orchestratorv1.WishlistDraftReply, error) {
	f.sessionMeta, _ = metadata.FromOutgoingContext(ctx)
	return &orchestratorv1.WishlistDraftReply{
		Draft: &orchestratorv1.WishlistDraft{
			ResultVersion: req.GetResultVersion(),
			DraftVersion:  1,
		},
	}, nil
}

func (f *fakeRunClient) SaveWishlistDraft(
	ctx context.Context,
	req *orchestratorv1.SaveWishlistDraftRequest,
	_ ...grpc.CallOption,
) (*orchestratorv1.WishlistDraftReply, error) {
	f.draftReq = req
	f.sessionMeta, _ = metadata.FromOutgoingContext(ctx)
	return &orchestratorv1.WishlistDraftReply{
		Draft: &orchestratorv1.WishlistDraft{
			ResultVersion: req.GetResultVersion(),
			DraftVersion:  req.GetExpectedVersion() + 1,
			Groups:        req.GetGroups(),
		},
	}, nil
}

func (f *fakeRunClient) ListWishlists(
	ctx context.Context,
	req *orchestratorv1.ListWishlistsRequest,
	_ ...grpc.CallOption,
) (*orchestratorv1.ListWishlistsReply, error) {
	f.listWishlistsReq = req
	f.sessionMeta, _ = metadata.FromOutgoingContext(ctx)
	return &orchestratorv1.ListWishlistsReply{
		Wishlists: []*orchestratorv1.WishlistSummary{
			{
				ResultVersion: "00000000-0000-0000-0000-000000000010",
				DraftVersion:  2,
				Status:        "CURRENT",
			},
		},
	}, nil
}

func (f *fakeRunClient) ListWishlistHistory(
	ctx context.Context,
	req *orchestratorv1.GetWishlistDraftRequest,
	_ ...grpc.CallOption,
) (*orchestratorv1.WishlistHistoryReply, error) {
	f.historyReq = req
	f.sessionMeta, _ = metadata.FromOutgoingContext(ctx)
	return &orchestratorv1.WishlistHistoryReply{
		Drafts: []*orchestratorv1.WishlistDraft{
			{
				ResultVersion: req.GetResultVersion(),
				DraftVersion:  2,
			},
			{
				ResultVersion: req.GetResultVersion(),
				DraftVersion:  1,
			},
		},
	}, nil
}

func (f *fakeRunClient) RestoreWishlistDraftVersion(
	ctx context.Context,
	req *orchestratorv1.RestoreWishlistDraftVersionRequest,
	_ ...grpc.CallOption,
) (*orchestratorv1.WishlistDraftReply, error) {
	f.restoreReq = req
	f.sessionMeta, _ = metadata.FromOutgoingContext(ctx)
	return &orchestratorv1.WishlistDraftReply{
		Draft: &orchestratorv1.WishlistDraft{
			ResultVersion: req.GetResultVersion(),
			DraftVersion:  3,
		},
	}, nil
}

func (f *fakeRunClient) DeleteWishlist(
	ctx context.Context,
	req *orchestratorv1.DeleteWishlistRequest,
	_ ...grpc.CallOption,
) (*orchestratorv1.DeleteWishlistReply, error) {
	f.deleteWishlistReq = req
	f.sessionMeta, _ = metadata.FromOutgoingContext(ctx)
	return &orchestratorv1.DeleteWishlistReply{
		Deleted:   true,
		RequestId: req.GetRequestId(),
	}, nil
}

func (f *fakeRunClient) DeletePrincipalData(
	ctx context.Context,
	req *orchestratorv1.DeletePrincipalDataRequest,
	_ ...grpc.CallOption,
) (*orchestratorv1.DeletePrincipalDataReply, error) {
	f.deletePrincipalReq = req
	f.sessionMeta, _ = metadata.FromOutgoingContext(ctx)
	if f.deletePrincipalReply != nil {
		return f.deletePrincipalReply, nil
	}
	return &orchestratorv1.DeletePrincipalDataReply{
		Accepted: true,
		Status:   "DELETION_COMPLETED",
	}, nil
}

func (f *fakeRunClient) RevokeSensitiveConsent(
	ctx context.Context,
	req *orchestratorv1.RevokeSensitiveConsentRequest,
	_ ...grpc.CallOption,
) (*orchestratorv1.RevokeSensitiveConsentReply, error) {
	f.revokeConsentReq = req
	f.sessionMeta, _ = metadata.FromOutgoingContext(ctx)
	return &orchestratorv1.RevokeSensitiveConsentReply{
		RevokedCount: 1,
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
				"subjects":["物理","化学","生物"],
				"interests":["物理学"],
				"required_major_names":["物理学"]
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
	if got := client.submitReq.GetProfileInput().GetRequiredMajorNames(); len(got) != 1 || got[0] != "物理学" {
		t.Fatalf("required_major_names = %v", got)
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

func TestSaveWishlistDraftPreservesPrincipalAndGroups(t *testing.T) {
	client := &fakeRunClient{}
	svc := NewService(client)
	ctx := trustedContext("principal-draft")

	reply, err := svc.SaveWishlistDraft(
		ctx,
		&gatewayv1.SaveWishlistDraftRequest{
			ResultVersion:   "00000000-0000-0000-0000-000000000010",
			ExpectedVersion: 2,
			IdempotencyKey:  "draft-save-1",
			Groups: []*orchestratorv1.WishlistDraftGroup{
				{
					UniversityCode: "1001",
					MajorGroup:     "03",
					MajorCodes:     []string{"080901", "080902"},
				},
			},
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	if client.draftReq.GetResultVersion() != "00000000-0000-0000-0000-000000000010" {
		t.Fatalf("result_version = %q", client.draftReq.GetResultVersion())
	}
	if client.draftReq.GetExpectedVersion() != 2 {
		t.Fatalf("expected_version = %d", client.draftReq.GetExpectedVersion())
	}
	if got := client.draftReq.Groups[0].GetMajorCodes(); len(got) != 2 || got[0] != "080901" {
		t.Fatalf("major_codes = %v", got)
	}
	if got := client.sessionMeta.Get("x-principal-id"); len(got) != 1 || got[0] != "principal-draft" {
		t.Fatalf("principal metadata = %v", got)
	}
	if reply.GetDraft().GetDraftVersion() != 3 {
		t.Fatalf("draft_version = %d", reply.GetDraft().GetDraftVersion())
	}
}

func TestCreatePolicyQueryUsesTypedPolicyInput(t *testing.T) {
	client := &fakeRunClient{}
	svc := NewService(client)
	ctx := trustedContext("principal-policy")

	reply, err := svc.CreatePolicyQuery(
		ctx,
		&gatewayv1.CreatePolicyQueryRequest{
			QueryText:       "转专业政策是什么？",
			Province:        "湖南",
			ApplicationYear: 2025,
			Category:        "物理类",
			Batch:           "本科批",
			SchoolIds:       []string{"1001"},
			DocumentTypes:   []string{"charter"},
			Mode:            "keyword",
			IdempotencyKey:  "policy-query-submit",
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	if client.submitReq.GetTaskKind() != orchestratorv1.TaskKind_POLICY_QA {
		t.Fatalf("task_kind = %v", client.submitReq.GetTaskKind())
	}
	if client.submitReq.GetPolicyInput().GetQueryText() != "转专业政策是什么？" {
		t.Fatalf("query_text = %q", client.submitReq.GetPolicyInput().GetQueryText())
	}
	if got := client.submitMeta.Get("x-principal-id"); len(got) != 1 || got[0] != "principal-policy" {
		t.Fatalf("principal metadata = %v", got)
	}
	if reply.GetRunId() == "" {
		t.Fatal("CreatePolicyQuery 未返回 run_id")
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

func TestConfirmProfilePassesStructuredRegionAnswer(t *testing.T) {
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
			"idempotency_key":"gateway-region-confirm",
			"answer":{
				"type":"SELECT_PROVINCES",
				"clarification_id":"region-clarification-1",
				"provinces":["北京","天津"],
				"selected_preset_ids":["north_china"],
				"region_catalog_version":"region-seven-v1",
				"cancel_region_restriction":false
			}
		}`,
	})
	if err != nil {
		t.Fatal(err)
	}
	answer := client.resumeReq.GetRegionAnswer()
	if answer == nil {
		t.Fatal("RegionAnswer 未透传")
	}
	if answer.GetClarificationId() != "region-clarification-1" {
		t.Fatalf("clarification_id = %q", answer.GetClarificationId())
	}
	if answer.GetCatalogVersion() != "region-seven-v1" {
		t.Fatalf("catalog_version = %q", answer.GetCatalogVersion())
	}
	if got := answer.GetPresetIds(); len(got) != 1 || got[0] != "north_china" {
		t.Fatalf("preset_ids = %v", got)
	}
}
