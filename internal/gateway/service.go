// Package gateway 实现 svc-gateway：入口、鉴权、限流、会话、HITL 推送。
//
// 边界：只做接入与门控；run 生命周期和图内控制归 svc-orchestrator。
package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	commonv1 "github.com/choucisan/ai-gaokao-counselor/api/common/v1"
	gatewayv1 "github.com/choucisan/ai-gaokao-counselor/api/gateway/v1"
	orchestratorv1 "github.com/choucisan/ai-gaokao-counselor/api/orchestrator/v1"
	"github.com/go-kratos/kratos/v2/transport"
)

// runClient 是 gateway 对 RunLifecycle 的最小传输依赖，不包含第二套状态机。
type runClient interface {
	SubmitRun(context.Context, *orchestratorv1.SubmitRunRequest, ...grpc.CallOption) (*orchestratorv1.RunReceiptReply, error)
	ResumeRun(context.Context, *orchestratorv1.ResumeRunRequest, ...grpc.CallOption) (*orchestratorv1.RunReceiptReply, error)
}

// Service 实现 gateway.v1.GatewayHTTPServer。
type Service struct {
	client runClient
}

// NewService 构造 Service；orchestrator 连接由 main 负责生命周期。
func NewService(client runClient) *Service { return &Service{client: client} }

type createPlanPayload struct {
	SessionID    string `json:"session_id"`
	TaskKind     string `json:"task_kind"`
	ProfileInput struct {
		Province        string   `json:"province"`
		ApplicationYear int32    `json:"application_year"`
		Category        string   `json:"category"`
		Batch           string   `json:"batch"`
		Score           int32    `json:"score"`
		Subjects        []string `json:"subjects"`
	} `json:"profile_input"`
	IdempotencyKey string `json:"idempotency_key"`
}

type resumePayload struct {
	RunID                string `json:"run_id"`
	InterruptID          string `json:"interrupt_id"`
	ExpectedStateVersion int64  `json:"expected_state_version"`
	ProfileVersion       string `json:"profile_version"`
	IdempotencyKey       string `json:"idempotency_key"`
	Answer               struct {
		Type      string                     `json:"type"`
		Fields    map[string]profileFieldDTO `json:"fields"`
		Patch     map[string]profileFieldDTO `json:"patch"`
		FieldKeys []string                   `json:"field_keys"`
	} `json:"answer"`
}

type profileFieldDTO struct {
	State  string `json:"state"`
	Source string `json:"source"`
	Value  any    `json:"value"`
}

func (s *Service) CreatePlan(ctx context.Context, req *gatewayv1.CreatePlanRequest) (*gatewayv1.CreatePlanReply, error) {
	principal, err := requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	var payload createPlanPayload
	if err := json.Unmarshal([]byte(strings.TrimSpace(req.GetUserText())), &payload); err != nil {
		return nil, status.Error(codes.InvalidArgument, "PROFILE_INPUT_JSON_REQUIRED")
	}
	if payload.IdempotencyKey == "" {
		return nil, status.Error(codes.InvalidArgument, "IDEMPOTENCY_KEY_REQUIRED")
	}
	taskKind := orchestratorv1.TaskKind(orchestratorv1.TaskKind_value[payload.TaskKind])
	if taskKind == orchestratorv1.TaskKind_TASK_KIND_UNSPECIFIED {
		taskKind = orchestratorv1.TaskKind_GENERATE_PLAN
	}
	reply, err := s.client.SubmitRun(
		outgoingPrincipal(ctx, principal),
		&orchestratorv1.SubmitRunRequest{
			Trace:     &commonv1.Trace{SessionId: payload.SessionID},
			SessionId: payload.SessionID,
			TaskKind:  taskKind,
			ProfileInput: &orchestratorv1.StructuredProfileInput{
				Province:        payload.ProfileInput.Province,
				ApplicationYear: payload.ProfileInput.ApplicationYear,
				Category:        payload.ProfileInput.Category,
				Batch:           payload.ProfileInput.Batch,
				Score:           payload.ProfileInput.Score,
				Subjects:        payload.ProfileInput.Subjects,
			},
			IdempotencyKey: payload.IdempotencyKey,
		},
	)
	if err != nil {
		return nil, err
	}
	return &gatewayv1.CreatePlanReply{
		SessionId: reply.GetSessionId(),
		Status:    receiptStatus(reply),
	}, nil
}

func (s *Service) ConfirmProfile(ctx context.Context, req *gatewayv1.ConfirmProfileRequest) (*gatewayv1.ConfirmProfileReply, error) {
	principal, err := requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	var payload resumePayload
	if err := json.Unmarshal([]byte(strings.TrimSpace(req.GetProfileJson())), &payload); err != nil {
		return nil, status.Error(codes.InvalidArgument, "RESUME_COMMAND_JSON_REQUIRED")
	}
	resume, err := resumeRequest(payload, true)
	if err != nil {
		return nil, err
	}
	reply, err := s.client.ResumeRun(outgoingPrincipal(ctx, principal), resume)
	if err != nil {
		return nil, err
	}
	return &gatewayv1.ConfirmProfileReply{
		SessionId: reply.GetSessionId(),
		Status:    receiptStatus(reply),
	}, nil
}

func (s *Service) BackfillProfile(ctx context.Context, req *gatewayv1.BackfillProfileRequest) (*gatewayv1.BackfillProfileReply, error) {
	principal, err := requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	var payload resumePayload
	if err := json.Unmarshal([]byte(strings.TrimSpace(req.GetBackfillJson())), &payload); err != nil {
		return nil, status.Error(codes.InvalidArgument, "RESUME_COMMAND_JSON_REQUIRED")
	}
	resume, err := resumeRequest(payload, false)
	if err != nil {
		return nil, err
	}
	reply, err := s.client.ResumeRun(outgoingPrincipal(ctx, principal), resume)
	if err != nil {
		return nil, err
	}
	return &gatewayv1.BackfillProfileReply{
		SessionId: reply.GetSessionId(),
		Status:    receiptStatus(reply),
	}, nil
}

func resumeRequest(payload resumePayload, confirmation bool) (*orchestratorv1.ResumeRunRequest, error) {
	if payload.RunID == "" || payload.InterruptID == "" || payload.IdempotencyKey == "" {
		return nil, status.Error(codes.InvalidArgument, "RESUME_COMMAND_INCOMPLETE")
	}
	resume := &orchestratorv1.ResumeRunRequest{
		RunId:                payload.RunID,
		InterruptId:          payload.InterruptID,
		ExpectedStateVersion: payload.ExpectedStateVersion,
		ProfileVersion:       payload.ProfileVersion,
		IdempotencyKey:       payload.IdempotencyKey,
	}
	switch strings.ToUpper(payload.Answer.Type) {
	case "CONFIRM_PROFILE", "":
		if !confirmation {
			return nil, status.Error(codes.InvalidArgument, "BACKFILL_ANSWER_TYPE_UNSUPPORTED")
		}
		resume.Answer = &orchestratorv1.ResumeRunRequest_ConfirmProfile{
			ConfirmProfile: &orchestratorv1.ConfirmProfileAnswer{},
		}
	case "REJECT_OR_EDIT_PROFILE":
		if !confirmation {
			return nil, status.Error(codes.InvalidArgument, "BACKFILL_ANSWER_TYPE_UNSUPPORTED")
		}
		resume.Answer = &orchestratorv1.ResumeRunRequest_RejectOrEditProfile{
			RejectOrEditProfile: &orchestratorv1.RejectOrEditProfileAnswer{
				Patch: profileFields(payload.Answer.Patch),
			},
		}
	case "ANSWER_FIELDS":
		resume.Answer = &orchestratorv1.ResumeRunRequest_AnswerFields{
			AnswerFields: &orchestratorv1.AnswerFields{
				Fields: profileFields(payload.Answer.Fields),
			},
		}
	case "DECLINE_FIELDS":
		resume.Answer = &orchestratorv1.ResumeRunRequest_DeclineFields{
			DeclineFields: &orchestratorv1.DeclineFieldsAnswer{
				FieldKeys: payload.Answer.FieldKeys,
			},
		}
	default:
		return nil, status.Error(codes.InvalidArgument, "RESUME_ANSWER_TYPE_UNSUPPORTED")
	}
	return resume, nil
}

func requirePrincipal(ctx context.Context) (string, error) {
	if md, ok := metadata.FromIncomingContext(ctx); ok {
		if values := md.Get("x-principal-id"); len(values) > 0 && strings.TrimSpace(values[0]) != "" {
			return strings.TrimSpace(values[0]), nil
		}
	}
	if tr, ok := transport.FromServerContext(ctx); ok {
		if principal := strings.TrimSpace(tr.RequestHeader().Get("x-principal-id")); principal != "" {
			return principal, nil
		}
	}
	return "", status.Error(codes.Unauthenticated, "PRINCIPAL_REQUIRED")
}

func outgoingPrincipal(ctx context.Context, principal string) context.Context {
	return metadata.AppendToOutgoingContext(ctx, "x-principal-id", principal)
}

func receiptStatus(reply *orchestratorv1.RunReceiptReply) string {
	if reply == nil {
		return "rejected"
	}
	if !reply.GetAccepted() {
		if reply.GetReason() != "" {
			return reply.GetReason()
		}
		return "rejected"
	}
	if reply.GetReplayed() {
		return "replayed"
	}
	return "accepted"
}

func profileFields(in map[string]profileFieldDTO) map[string]*orchestratorv1.ProfileFieldView {
	out := make(map[string]*orchestratorv1.ProfileFieldView, len(in))
	for key, value := range in {
		field := &orchestratorv1.ProfileFieldView{State: value.State, Source: value.Source}
		switch typed := value.Value.(type) {
		case string:
			field.Value = &orchestratorv1.ProfileFieldView_StringValue{StringValue: typed}
		case float64:
			field.Value = &orchestratorv1.ProfileFieldView_IntValue{IntValue: int32(typed)}
		case []any:
			values := make([]string, 0, len(typed))
			for _, item := range typed {
				values = append(values, fmt.Sprint(item))
			}
			field.Value = &orchestratorv1.ProfileFieldView_StringList{
				StringList: &orchestratorv1.StringList{Values: values},
			}
		}
		out[key] = field
	}
	return out
}
