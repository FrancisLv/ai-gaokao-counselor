// Package gateway 实现 svc-gateway：入口、鉴权、限流、会话、HITL 推送。
//
// 边界：只做接入与门控；run 生命周期和图内控制归 svc-orchestrator。
package gateway

import (
	"context"
	"encoding/json"
	"errors"
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
	"github.com/google/uuid"
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
	ListSessions(context.Context, *orchestratorv1.ListSessionsRequest, ...grpc.CallOption) (*orchestratorv1.ListSessionsReply, error)
	GetSession(context.Context, *orchestratorv1.GetSessionRequest, ...grpc.CallOption) (*orchestratorv1.ChatSessionReply, error)
	SubmitConversationMessage(context.Context, *orchestratorv1.SubmitConversationMessageRequest, ...grpc.CallOption) (*orchestratorv1.SubmitConversationMessageReply, error)
	GetWishlistDraft(context.Context, *orchestratorv1.GetWishlistDraftRequest, ...grpc.CallOption) (*orchestratorv1.WishlistDraftReply, error)
	SaveWishlistDraft(context.Context, *orchestratorv1.SaveWishlistDraftRequest, ...grpc.CallOption) (*orchestratorv1.WishlistDraftReply, error)
	ListWishlists(context.Context, *orchestratorv1.ListWishlistsRequest, ...grpc.CallOption) (*orchestratorv1.ListWishlistsReply, error)
	ListWishlistHistory(context.Context, *orchestratorv1.GetWishlistDraftRequest, ...grpc.CallOption) (*orchestratorv1.WishlistHistoryReply, error)
	RestoreWishlistDraftVersion(context.Context, *orchestratorv1.RestoreWishlistDraftVersionRequest, ...grpc.CallOption) (*orchestratorv1.WishlistDraftReply, error)
	DeleteWishlist(context.Context, *orchestratorv1.DeleteWishlistRequest, ...grpc.CallOption) (*orchestratorv1.DeleteWishlistReply, error)
	DeletePrincipalData(context.Context, *orchestratorv1.DeletePrincipalDataRequest, ...grpc.CallOption) (*orchestratorv1.DeletePrincipalDataReply, error)
	RevokeSensitiveConsent(context.Context, *orchestratorv1.RevokeSensitiveConsentRequest, ...grpc.CallOption) (*orchestratorv1.RevokeSensitiveConsentReply, error)
}

// Service 实现 gateway.v1.GatewayHTTPServer。
type Service struct {
	client      runClient
	identity    identityService
	authOptions AuthOptions
}

// NewService 构造 Service；orchestrator 连接由 main 负责生命周期。
func NewService(client runClient) *Service {
	return newServiceWithIdentity(client, nil, AuthOptions{})
}

func newServiceWithIdentity(
	client runClient,
	identity identityService,
	options AuthOptions,
) *Service {
	if options.SessionIdleTTL <= 0 {
		options.SessionIdleTTL = defaultSessionIdleTTL
	}
	if options.SessionAbsoluteTTL <= 0 {
		options.SessionAbsoluteTTL = defaultSessionAbsoluteTTL
	}
	return &Service{
		client:      client,
		identity:    identity,
		authOptions: options,
	}
}

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

func NewServiceForOrchestratorWithIdentity(
	client orchestratorv1.OrchestratorClient,
	identity identityService,
	options AuthOptions,
) *Service {
	return newServiceWithIdentity(
		orchestratorRunClient{OrchestratorClient: client},
		identity,
		options,
	)
}

func registerGatewayRoutes(server *khttp.Server, service *Service) {
	gatewayv1.RegisterGatewayHTTPServer(server, service)
	RegisterRunEventStream(server, service, EventStreamOptions{})
}

// EventStreamOptions 只控制传输层心跳；业务事件序号仍由 orchestrator 决定。
type EventStreamOptions struct {
	HeartbeatInterval time.Duration
}

type createPlanPayload struct {
	SessionID           string               `json:"session_id"`
	TaskKind            string               `json:"task_kind"`
	ParentResultVersion string               `json:"parent_result_version"`
	UserMessage         string               `json:"user_message"`
	Messages            []chatMessagePayload `json:"messages"`
	ProfileInput        struct {
		Province              string   `json:"province"`
		ApplicationYear       int32    `json:"application_year"`
		Category              string   `json:"category"`
		Batch                 string   `json:"batch"`
		Score                 int32    `json:"score"`
		Subjects              []string `json:"subjects"`
		RiskAppetite          string   `json:"risk_appetite"`
		PriorityDims          []string `json:"priority_dims"`
		Interests             []string `json:"interests"`
		PreferredCities       []string `json:"preferred_cities"`
		PreferredTiers        []string `json:"preferred_tiers"`
		RequiredCities        []string `json:"required_cities"`
		ExcludedCities        []string `json:"excluded_cities"`
		RequiredTiers         []string `json:"required_tiers"`
		RequiredMajorNames    []string `json:"required_major_names"`
		RequiredMajorKeywords []string `json:"required_major_keywords"`
		ExcludedMajorKeywords []string `json:"excluded_major_keywords"`
		ExcludedMajorNames    []string `json:"excluded_major_names"`
		ExcludedUniversities  []string `json:"excluded_universities"`
	} `json:"profile_input"`
	IdempotencyKey string `json:"idempotency_key"`
}

type chatMessagePayload struct {
	ClientMessageID string `json:"client_message_id"`
	Role            string `json:"role"`
	Kind            string `json:"kind"`
	Content         string `json:"content"`
}

type resumePayload struct {
	RunID                string `json:"run_id"`
	InterruptID          string `json:"interrupt_id"`
	ExpectedStateVersion int64  `json:"expected_state_version"`
	ProfileVersion       string `json:"profile_version"`
	IdempotencyKey       string `json:"idempotency_key"`
	Answer               struct {
		Type                    string                     `json:"type"`
		Fields                  map[string]profileFieldDTO `json:"fields"`
		Patch                   map[string]profileFieldDTO `json:"patch"`
		FieldKeys               []string                   `json:"field_keys"`
		ClarificationID         string                     `json:"clarification_id"`
		Provinces               []string                   `json:"provinces"`
		PresetIDs               []string                   `json:"selected_preset_ids"`
		RegionCatalogVersion    string                     `json:"region_catalog_version"`
		CancelRegionRestriction bool                       `json:"cancel_region_restriction"`
	} `json:"answer"`
}

type profileFieldDTO struct {
	State  string `json:"state"`
	Source string `json:"source"`
	Value  any    `json:"value"`
}

func (s *Service) CreatePlan(ctx context.Context, req *gatewayv1.CreatePlanRequest) (*gatewayv1.CreatePlanReply, error) {
	principal, err := s.requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	if err := validateCSRF(ctx); err != nil {
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
				Province:              payload.ProfileInput.Province,
				ApplicationYear:       payload.ProfileInput.ApplicationYear,
				Category:              payload.ProfileInput.Category,
				Batch:                 payload.ProfileInput.Batch,
				Score:                 payload.ProfileInput.Score,
				Subjects:              payload.ProfileInput.Subjects,
				RiskAppetite:          payload.ProfileInput.RiskAppetite,
				PriorityDims:          payload.ProfileInput.PriorityDims,
				Interests:             payload.ProfileInput.Interests,
				PreferredCities:       payload.ProfileInput.PreferredCities,
				PreferredTiers:        payload.ProfileInput.PreferredTiers,
				RequiredCities:        payload.ProfileInput.RequiredCities,
				ExcludedCities:        payload.ProfileInput.ExcludedCities,
				RequiredTiers:         payload.ProfileInput.RequiredTiers,
				RequiredMajorNames:    payload.ProfileInput.RequiredMajorNames,
				RequiredMajorKeywords: payload.ProfileInput.RequiredMajorKeywords,
				ExcludedMajorKeywords: payload.ProfileInput.ExcludedMajorKeywords,
				ExcludedMajorNames:    payload.ProfileInput.ExcludedMajorNames,
				ExcludedUniversities:  payload.ProfileInput.ExcludedUniversities,
			},
			IdempotencyKey:      payload.IdempotencyKey,
			ParentResultVersion: payload.ParentResultVersion,
			UserMessage:         payload.UserMessage,
			Messages:            chatMessages(payload.Messages),
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

func chatMessages(messages []chatMessagePayload) []*orchestratorv1.ChatMessageInput {
	result := make([]*orchestratorv1.ChatMessageInput, 0, len(messages))
	for _, message := range messages {
		result = append(result, &orchestratorv1.ChatMessageInput{
			ClientMessageId: message.ClientMessageID,
			Role:            message.Role,
			Kind:            message.Kind,
			Content:         message.Content,
		})
	}
	return result
}

func (s *Service) CreatePolicyQuery(
	ctx context.Context,
	req *gatewayv1.CreatePolicyQueryRequest,
) (*gatewayv1.CreatePolicyQueryReply, error) {
	principal, err := s.requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	if err := validateCSRF(ctx); err != nil {
		return nil, err
	}
	if strings.TrimSpace(req.GetQueryText()) == "" {
		return nil, status.Error(codes.InvalidArgument, "RETRIEVAL_QUERY_REQUIRED")
	}
	if strings.TrimSpace(req.GetIdempotencyKey()) == "" {
		return nil, status.Error(codes.InvalidArgument, "IDEMPOTENCY_KEY_REQUIRED")
	}
	reply, err := s.client.SubmitRun(
		outgoingPrincipal(ctx, principal),
		&orchestratorv1.SubmitRunRequest{
			Trace:     &commonv1.Trace{SessionId: req.GetSessionId()},
			SessionId: req.GetSessionId(),
			TaskKind:  orchestratorv1.TaskKind_POLICY_QA,
			PolicyInput: &orchestratorv1.PolicyQaInput{
				QueryText:       req.GetQueryText(),
				Province:        req.GetProvince(),
				ApplicationYear: req.GetApplicationYear(),
				Category:        req.GetCategory(),
				Batch:           req.GetBatch(),
				SchoolIds:       req.GetSchoolIds(),
				DocumentTypes:   req.GetDocumentTypes(),
				Mode:            req.GetMode(),
			},
			IdempotencyKey: req.GetIdempotencyKey(),
		},
	)
	if err != nil {
		return nil, err
	}
	return &gatewayv1.CreatePolicyQueryReply{
		SessionId: reply.GetSessionId(),
		Status:    receiptStatus(reply),
		RunId:     reply.GetRunId(),
	}, nil
}

func (s *Service) ConfirmProfile(ctx context.Context, req *gatewayv1.ConfirmProfileRequest) (*gatewayv1.ConfirmProfileReply, error) {
	principal, err := s.requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	if err := validateCSRF(ctx); err != nil {
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
	principal, err := s.requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	if err := validateCSRF(ctx); err != nil {
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
	principal, err := s.requirePrincipal(ctx)
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
	principal, err := s.requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	if err := validateCSRF(ctx); err != nil {
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
	principal, err := s.requirePrincipal(ctx)
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

func (s *Service) ListSessions(
	ctx context.Context,
	req *gatewayv1.ListSessionsRequest,
) (*orchestratorv1.ListSessionsReply, error) {
	principal, err := s.requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	return s.client.ListSessions(
		outgoingPrincipal(ctx, principal),
		&orchestratorv1.ListSessionsRequest{Limit: req.GetLimit()},
	)
}

func (s *Service) GetSession(
	ctx context.Context,
	req *gatewayv1.GetSessionRequest,
) (*orchestratorv1.ChatSessionReply, error) {
	principal, err := s.requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(req.GetSessionId()) == "" {
		return nil, status.Error(codes.InvalidArgument, "SESSION_ID_REQUIRED")
	}
	return s.client.GetSession(
		outgoingPrincipal(ctx, principal),
		&orchestratorv1.GetSessionRequest{SessionId: req.GetSessionId()},
	)
}

func (s *Service) SubmitConversationMessage(
	ctx context.Context,
	req *gatewayv1.SubmitConversationMessageRequest,
) (*orchestratorv1.SubmitConversationMessageReply, error) {
	principal, err := s.requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	if err := validateCSRF(ctx); err != nil {
		return nil, err
	}
	if strings.TrimSpace(req.GetSessionId()) == "" ||
		strings.TrimSpace(req.GetMessageText()) == "" ||
		strings.TrimSpace(req.GetIdempotencyKey()) == "" {
		return nil, status.Error(
			codes.InvalidArgument,
			"CONVERSATION_COMMAND_INCOMPLETE",
		)
	}
	return s.client.SubmitConversationMessage(
		outgoingPrincipal(ctx, principal),
		&orchestratorv1.SubmitConversationMessageRequest{
			SessionId:           req.GetSessionId(),
			ProfileInput:        req.GetProfileInput(),
			ParentResultVersion: req.GetParentResultVersion(),
			MessageText:         req.GetMessageText(),
			IdempotencyKey:      req.GetIdempotencyKey(),
			RegionAnswer:        req.GetRegionAnswer(),
		},
	)
}

func (s *Service) GetWishlistDraft(
	ctx context.Context,
	req *gatewayv1.GetWishlistDraftRequest,
) (*orchestratorv1.WishlistDraftReply, error) {
	principal, err := s.requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(req.GetResultVersion()) == "" {
		return nil, status.Error(
			codes.InvalidArgument,
			"RESULT_VERSION_REQUIRED",
		)
	}
	return s.client.GetWishlistDraft(
		outgoingPrincipal(ctx, principal),
		&orchestratorv1.GetWishlistDraftRequest{
			ResultVersion: req.GetResultVersion(),
		},
	)
}

func (s *Service) SaveWishlistDraft(
	ctx context.Context,
	req *gatewayv1.SaveWishlistDraftRequest,
) (*orchestratorv1.WishlistDraftReply, error) {
	principal, err := s.requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	if err := validateCSRF(ctx); err != nil {
		return nil, err
	}
	if strings.TrimSpace(req.GetResultVersion()) == "" ||
		strings.TrimSpace(req.GetIdempotencyKey()) == "" ||
		req.GetExpectedVersion() < 0 {
		return nil, status.Error(
			codes.InvalidArgument,
			"DRAFT_COMMAND_INCOMPLETE",
		)
	}
	return s.client.SaveWishlistDraft(
		outgoingPrincipal(ctx, principal),
		&orchestratorv1.SaveWishlistDraftRequest{
			ResultVersion:   req.GetResultVersion(),
			ExpectedVersion: req.GetExpectedVersion(),
			IdempotencyKey:  req.GetIdempotencyKey(),
			Groups:          req.GetGroups(),
		},
	)
}

func (s *Service) ListWishlists(
	ctx context.Context,
	req *gatewayv1.ListWishlistsRequest,
) (*orchestratorv1.ListWishlistsReply, error) {
	principal, err := s.requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	limit := req.GetLimit()
	if limit == 0 {
		limit = 20
	}
	if limit < 1 || limit > 100 || req.GetOffset() < 0 {
		return nil, status.Error(codes.InvalidArgument, "WISHLIST_QUERY_INVALID")
	}
	return s.client.ListWishlists(
		outgoingPrincipal(ctx, principal),
		&orchestratorv1.ListWishlistsRequest{
			Limit:  limit,
			Offset: req.GetOffset(),
			Status: req.GetStatus(),
		},
	)
}

func (s *Service) ListWishlistHistory(
	ctx context.Context,
	req *gatewayv1.ListWishlistHistoryRequest,
) (*orchestratorv1.WishlistHistoryReply, error) {
	principal, err := s.requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(req.GetResultVersion()) == "" {
		return nil, status.Error(codes.InvalidArgument, "RESULT_VERSION_REQUIRED")
	}
	return s.client.ListWishlistHistory(
		outgoingPrincipal(ctx, principal),
		&orchestratorv1.GetWishlistDraftRequest{
			ResultVersion: req.GetResultVersion(),
		},
	)
}

func (s *Service) RestoreWishlistDraftVersion(
	ctx context.Context,
	req *gatewayv1.RestoreWishlistDraftVersionRequest,
) (*orchestratorv1.WishlistDraftReply, error) {
	principal, err := s.requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	if err := validateCSRF(ctx); err != nil {
		return nil, err
	}
	if strings.TrimSpace(req.GetResultVersion()) == "" ||
		req.GetDraftVersion() < 1 ||
		strings.TrimSpace(req.GetIdempotencyKey()) == "" {
		return nil, status.Error(codes.InvalidArgument, "DRAFT_RESTORE_INCOMPLETE")
	}
	return s.client.RestoreWishlistDraftVersion(
		outgoingPrincipal(ctx, principal),
		&orchestratorv1.RestoreWishlistDraftVersionRequest{
			ResultVersion:  req.GetResultVersion(),
			DraftVersion:   req.GetDraftVersion(),
			IdempotencyKey: req.GetIdempotencyKey(),
		},
	)
}

func (s *Service) DeleteWishlist(
	ctx context.Context,
	req *gatewayv1.DeleteWishlistRequest,
) (*orchestratorv1.DeleteWishlistReply, error) {
	principal, err := s.requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	if err := validateCSRF(ctx); err != nil {
		return nil, err
	}
	if strings.TrimSpace(req.GetResultVersion()) == "" ||
		strings.TrimSpace(req.GetRequestId()) == "" {
		return nil, status.Error(codes.InvalidArgument, "WISHLIST_DELETE_INCOMPLETE")
	}
	return s.client.DeleteWishlist(
		outgoingPrincipal(ctx, principal),
		&orchestratorv1.DeleteWishlistRequest{
			ResultVersion: req.GetResultVersion(),
			RequestId:     req.GetRequestId(),
		},
	)
}

func (s *Service) CleanupDormantAccounts(ctx context.Context) error {
	if s.identity == nil {
		return nil
	}
	accountIDs, err := s.identity.ListDormantAccountIDs(
		ctx,
		time.Now().UTC().AddDate(-2, 0, 0),
	)
	if err != nil {
		return err
	}
	for _, accountID := range accountIDs {
		deletion, err := s.client.DeletePrincipalData(
			outgoingPrincipal(ctx, accountID),
			&orchestratorv1.DeletePrincipalDataRequest{
				RequestId: uuid.NewString(),
			},
		)
		if err != nil {
			return err
		}
		if !deletion.GetAccepted() ||
			deletion.GetStatus() != "DELETION_COMPLETED" {
			continue
		}
		if err := s.identity.CompleteAccountDeletion(ctx, accountID); err != nil {
			return err
		}
	}
	return nil
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return "DELETION_FAILED_RETRYABLE"
}

func (s *Service) RevokeSensitiveConsent(
	ctx context.Context,
	_ *gatewayv1.RevokeSensitiveConsentRequest,
) (*gatewayv1.RevokeSensitiveConsentReply, error) {
	principal, err := s.requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	if err := validateCSRF(ctx); err != nil {
		return nil, err
	}
	reply, err := s.client.RevokeSensitiveConsent(
		outgoingPrincipal(ctx, principal),
		&orchestratorv1.RevokeSensitiveConsentRequest{},
	)
	if err != nil {
		return nil, err
	}
	return &gatewayv1.RevokeSensitiveConsentReply{
		RevokedCount: reply.GetRevokedCount(),
	}, nil
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
	case "SELECT_PROVINCES":
		resume.Answer = &orchestratorv1.ResumeRunRequest_RegionAnswer{
			RegionAnswer: &orchestratorv1.RegionAnswer{
				ClarificationId:         payload.Answer.ClarificationID,
				Provinces:               payload.Answer.Provinces,
				PresetIds:               payload.Answer.PresetIDs,
				CatalogVersion:          payload.Answer.RegionCatalogVersion,
				CancelRegionRestriction: payload.Answer.CancelRegionRestriction,
			},
		}
	default:
		return nil, status.Error(codes.InvalidArgument, "RESUME_ANSWER_TYPE_UNSUPPORTED")
	}
	return resume, nil
}

func (s *Service) requirePrincipal(ctx context.Context) (string, error) {
	if tr, ok := transport.FromServerContext(ctx); ok &&
		tr.Kind() == transport.KindHTTP {
		if s.identity == nil {
			return "", status.Error(codes.Unauthenticated, "PRINCIPAL_REQUIRED")
		}
		request, ok := khttp.RequestFromServerContext(ctx)
		if !ok {
			return "", status.Error(codes.Unauthenticated, "PRINCIPAL_REQUIRED")
		}
		cookie, err := request.Cookie(sessionCookieName)
		if err != nil || strings.TrimSpace(cookie.Value) == "" {
			return "", status.Error(codes.Unauthenticated, "PRINCIPAL_REQUIRED")
		}
		account, err := s.identity.ResolveSession(ctx, cookie.Value)
		if errors.Is(err, ErrUnauthenticated) {
			return "", status.Error(codes.Unauthenticated, "PRINCIPAL_REQUIRED")
		}
		if err != nil {
			return "", err
		}
		if strings.TrimSpace(account.AccountID) == "" {
			return "", status.Error(codes.Unauthenticated, "PRINCIPAL_REQUIRED")
		}
		return account.AccountID, nil
	}
	if md, ok := metadata.FromIncomingContext(ctx); ok {
		if values := md.Get("x-principal-id"); len(values) > 0 && strings.TrimSpace(values[0]) != "" {
			return strings.TrimSpace(values[0]), nil
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
