// Package gateway 实现 svc-gateway：入口、鉴权、限流、会话、HITL 推送。
//
// 边界：只做接入与门控；run 生命周期和图内控制归 svc-orchestrator。
package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"strconv"
	"strings"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"

	commonv1 "github.com/choucisan/ai-gaokao-counselor/api/common/v1"
	gatewayv1 "github.com/choucisan/ai-gaokao-counselor/api/gateway/v1"
	orchestratorv1 "github.com/choucisan/ai-gaokao-counselor/api/orchestrator/v1"
	"github.com/go-kratos/kratos/v2/transport"
	khttp "github.com/go-kratos/kratos/v2/transport/http"
)

const defaultSSEHeartbeat = 15 * time.Second

type runEventStream interface {
	Recv() (*orchestratorv1.RunEvent, error)
}

// runClient 是 gateway 对 RunLifecycle 的最小传输依赖，不包含第二套状态机。
type runClient interface {
	SubmitRun(context.Context, *orchestratorv1.SubmitRunRequest, ...grpc.CallOption) (*orchestratorv1.RunReceiptReply, error)
	ResumeRun(context.Context, *orchestratorv1.ResumeRunRequest, ...grpc.CallOption) (*orchestratorv1.RunReceiptReply, error)
	GetRun(context.Context, *orchestratorv1.GetRunRequest, ...grpc.CallOption) (*orchestratorv1.RunViewReply, error)
	CancelRun(context.Context, *orchestratorv1.CancelRunRequest, ...grpc.CallOption) (*orchestratorv1.RunReceiptReply, error)
	WatchRun(context.Context, *orchestratorv1.WatchRunRequest, ...grpc.CallOption) (runEventStream, error)
}

// Service 实现 gateway.v1.GatewayHTTPServer。
type Service struct {
	client runClient
}

// NewService 构造 Service；orchestrator 连接由 main 负责生命周期。
func NewService(client runClient) *Service { return &Service{client: client} }

type orchestratorRunClient struct {
	orchestratorv1.OrchestratorClient
}

func (c orchestratorRunClient) WatchRun(
	ctx context.Context,
	req *orchestratorv1.WatchRunRequest,
	opts ...grpc.CallOption,
) (runEventStream, error) {
	return c.OrchestratorClient.WatchRun(ctx, req, opts...)
}

// NewServiceForOrchestrator 把生成的 gRPC client 适配为 gateway 内部依赖。
func NewServiceForOrchestrator(client orchestratorv1.OrchestratorClient) *Service {
	return NewService(orchestratorRunClient{OrchestratorClient: client})
}

// EventStreamOptions 只控制传输层心跳；业务事件序号仍由 orchestrator 决定。
type EventStreamOptions struct {
	HeartbeatInterval time.Duration
}

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
		RunId:     reply.GetRunId(),
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
		RunId:     reply.GetRunId(),
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
		RunId:     reply.GetRunId(),
	}, nil
}

func (s *Service) GetRun(ctx context.Context, req *gatewayv1.GetRunRequest) (*orchestratorv1.RunViewReply, error) {
	principal, err := requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(req.GetRunId()) == "" {
		return nil, status.Error(codes.InvalidArgument, "RUN_ID_REQUIRED")
	}
	return s.client.GetRun(
		outgoingPrincipal(ctx, principal),
		&orchestratorv1.GetRunRequest{RunId: req.GetRunId()},
	)
}

func (s *Service) CancelRun(ctx context.Context, req *gatewayv1.CancelRunRequest) (*orchestratorv1.RunReceiptReply, error) {
	principal, err := requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(req.GetRunId()) == "" || strings.TrimSpace(req.GetIdempotencyKey()) == "" {
		return nil, status.Error(codes.InvalidArgument, "CANCEL_COMMAND_INCOMPLETE")
	}
	return s.client.CancelRun(
		outgoingPrincipal(ctx, principal),
		&orchestratorv1.CancelRunRequest{
			RunId:          req.GetRunId(),
			IdempotencyKey: req.GetIdempotencyKey(),
		},
	)
}

func (s *Service) WatchRun(ctx context.Context, req *gatewayv1.WatchRunRequest) (runEventStream, error) {
	principal, err := requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(req.GetRunId()) == "" {
		return nil, status.Error(codes.InvalidArgument, "RUN_ID_REQUIRED")
	}
	return s.client.WatchRun(
		outgoingPrincipal(ctx, principal),
		&orchestratorv1.WatchRunRequest{
			RunId:         req.GetRunId(),
			AfterEventSeq: req.GetAfterEventSeq(),
		},
	)
}

// RegisterRunEventStream 将 typed RunEvent 流写入标准 SSE；心跳不进入业务序号。
func RegisterRunEventStream(server *khttp.Server, service *Service, options EventStreamOptions) {
	heartbeat := options.HeartbeatInterval
	if heartbeat <= 0 {
		heartbeat = defaultSSEHeartbeat
	}
	server.Route("/").GET("/v1/runs/{run_id}/events", func(ctx khttp.Context) error {
		after, err := cursorFromRequest(ctx, 0)
		if err != nil {
			return err
		}
		if queryAfter := strings.TrimSpace(ctx.Query().Get("after_event_seq")); queryAfter != "" {
			after, err = strconv.ParseInt(queryAfter, 10, 64)
			if err != nil || after < 0 {
				return status.Error(codes.InvalidArgument, "INVALID_EVENT_CURSOR")
			}
		}
		stream, err := service.WatchRun(ctx, &gatewayv1.WatchRunRequest{
			RunId:         ctx.Vars().Get("run_id"),
			AfterEventSeq: after,
		})
		if err != nil {
			return err
		}
		writer := ctx.Response()
		flusher, ok := writer.(khttp.Flusher)
		if !ok {
			return status.Error(codes.Internal, "SSE_UNSUPPORTED")
		}
		writer.Header().Set("Content-Type", "text/event-stream")
		writer.Header().Set("Cache-Control", "no-cache")
		writer.Header().Set("Connection", "keep-alive")
		writer.Header().Set("X-Accel-Buffering", "no")
		writer.WriteHeader(200)
		flusher.Flush()

		events := make(chan *orchestratorv1.RunEvent)
		streamErr := make(chan error, 1)
		go func() {
			for {
				event, recvErr := stream.Recv()
				if recvErr != nil {
					streamErr <- recvErr
					return
				}
				select {
				case events <- event:
				case <-ctx.Done():
					return
				}
			}
		}()

		ticker := time.NewTicker(heartbeat)
		defer ticker.Stop()
		for {
			select {
			case event := <-events:
				data, marshalErr := protojson.Marshal(event)
				if marshalErr != nil {
					return nil
				}
				if _, writeErr := fmt.Fprintf(
					writer,
					"id: %d\nevent: %s\ndata: %s\n\n",
					event.GetEventSeq(),
					event.GetType().String(),
					data,
				); writeErr != nil {
					return nil
				}
				flusher.Flush()
			case recvErr := <-streamErr:
				if recvErr == io.EOF {
					return nil
				}
				return nil
			case <-ticker.C:
				if _, writeErr := io.WriteString(writer, ": heartbeat\n\n"); writeErr != nil {
					return nil
				}
				flusher.Flush()
			case <-ctx.Done():
				return nil
			}
		}
	})
}

func cursorFromRequest(ctx khttp.Context, fallback int64) (int64, error) {
	raw := strings.TrimSpace(ctx.Header().Get("Last-Event-ID"))
	if raw == "" {
		return fallback, nil
	}
	after, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || after < 0 {
		return 0, status.Error(codes.InvalidArgument, "INVALID_EVENT_CURSOR")
	}
	return after, nil
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
