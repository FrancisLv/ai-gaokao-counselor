package llmgw

import (
	"context"
	"strings"

	kerrors "github.com/go-kratos/kratos/v2/errors"
	"google.golang.org/grpc/status"

	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
)

type callObserver interface {
	OnReserved(context.Context, *preparedCall) error
	OnResult(context.Context, *llmgwv1.ChatResponse) error
}

func observeReservation(ctx context.Context, observer callObserver, prepared *preparedCall) error {
	if observer == nil {
		return nil
	}
	return observer.OnReserved(ctx, prepared)
}

func observeResult(ctx context.Context, observer callObserver, response *llmgwv1.ChatResponse) error {
	if observer == nil {
		return nil
	}
	return observer.OnResult(ctx, response)
}

type grpcStreamObserver struct {
	server     llmgwv1.LLMGateway_StreamChatServer
	sequence   int64
	logicalID  string
	attemptID  string
	ledgerID   string
	modelID    string
	route      string
	priceTable string
	releaseID  string
}

func (o *grpcStreamObserver) OnReserved(
	_ context.Context,
	prepared *preparedCall,
) error {
	o.logicalID = prepared.logicalCallID
	o.attemptID = prepared.attemptID
	o.ledgerID = prepared.ledgerEntryID
	o.modelID = prepared.selected.modelID
	o.route = prepared.route.version
	o.priceTable = prepared.route.priceVersion
	o.releaseID = prepared.releaseID
	return o.send(&llmgwv1.ChatStreamEvent{
		Type:              llmgwv1.ChatStreamEventType_CHAT_STREAM_EVENT_START,
		LogicalCallId:     prepared.logicalCallID,
		AttemptId:         prepared.attemptID,
		ModelId:           prepared.selected.modelID,
		LedgerEntryId:     prepared.ledgerEntryID,
		RouteVersion:      prepared.route.version,
		PriceTableVersion: prepared.route.priceVersion,
		ReleaseId:         prepared.releaseID,
		Cost: &llmgwv1.Cost{
			CurrencyUnit:     "USD_MICRO",
			ReservedMicroUsd: prepared.reservedCost,
			SettlementStatus: "RESERVED",
		},
	})
}

func (o *grpcStreamObserver) OnResult(
	_ context.Context,
	response *llmgwv1.ChatResponse,
) error {
	o.rememberResponse(response)

	delta := &llmgwv1.ChatStreamEvent{
		Type:                  llmgwv1.ChatStreamEventType_CHAT_STREAM_EVENT_DELTA,
		LogicalCallId:         response.GetLogicalCallId(),
		AttemptId:             successfulAttemptID(response),
		ModelId:               response.GetModel(),
		DeltaText:             response.GetContent(),
		LedgerEntryId:         response.GetLedgerEntryId(),
		RouteVersion:          response.GetRouteVersion(),
		PriceTableVersion:     response.GetPriceTableVersion(),
		ReleaseId:             response.GetReleaseId(),
		ResponseSchemaKey:     response.GetResponseSchemaKey(),
		ResponseSchemaVersion: response.GetResponseSchemaVersion(),
		CacheHit:              response.GetCacheHit(),
	}
	if err := o.sendWithLegacy(delta, response.GetContent(), false); err != nil {
		return err
	}

	if !response.GetCacheHit() {
		usage := &llmgwv1.ChatStreamEvent{
			Type:                  llmgwv1.ChatStreamEventType_CHAT_STREAM_EVENT_USAGE,
			LogicalCallId:         response.GetLogicalCallId(),
			AttemptId:             successfulAttemptID(response),
			ModelId:               response.GetModel(),
			Usage:                 response.GetUsage(),
			Cost:                  response.GetCostDetail(),
			LedgerEntryId:         response.GetLedgerEntryId(),
			RouteVersion:          response.GetRouteVersion(),
			PriceTableVersion:     response.GetPriceTableVersion(),
			ReleaseId:             response.GetReleaseId(),
			ResponseSchemaKey:     response.GetResponseSchemaKey(),
			ResponseSchemaVersion: response.GetResponseSchemaVersion(),
		}
		if err := o.send(usage); err != nil {
			return err
		}
	}

	done := &llmgwv1.ChatStreamEvent{
		Type:                  llmgwv1.ChatStreamEventType_CHAT_STREAM_EVENT_DONE,
		LogicalCallId:         response.GetLogicalCallId(),
		AttemptId:             successfulAttemptID(response),
		ModelId:               response.GetModel(),
		FinishReason:          response.GetFinishReason(),
		Usage:                 response.GetUsage(),
		Cost:                  response.GetCostDetail(),
		LedgerEntryId:         response.GetLedgerEntryId(),
		SchemaValidated:       response.GetResponseSchemaKey() != "",
		StructuredOutput:      response.GetStructuredOutput(),
		Content:               response.GetContent(),
		Attempts:              response.GetAttempts(),
		RouteVersion:          response.GetRouteVersion(),
		PriceTableVersion:     response.GetPriceTableVersion(),
		ReleaseId:             response.GetReleaseId(),
		ResponseSchemaKey:     response.GetResponseSchemaKey(),
		ResponseSchemaVersion: response.GetResponseSchemaVersion(),
		CacheHit:              response.GetCacheHit(),
	}
	return o.sendWithLegacy(done, "", true)
}

func (o *grpcStreamObserver) OnError(ctx context.Context, err error) error {
	if err == nil {
		return nil
	}
	kerr := kerrors.FromError(err)
	metadata := make(map[string]string, len(kerr.GetMetadata()))
	for key, value := range kerr.GetMetadata() {
		metadata[key] = value
	}
	logicalID := firstNonEmpty(metadata["logical_call_id"], o.logicalID)
	attemptID := firstNonEmpty(metadata["attempt_id"], o.attemptID)
	ledgerID := firstNonEmpty(metadata["ledger_entry_id"], o.ledgerID)
	event := &llmgwv1.ChatStreamEvent{
		Type:          llmgwv1.ChatStreamEventType_CHAT_STREAM_EVENT_ERROR,
		LogicalCallId: logicalID,
		AttemptId:     attemptID,
		ModelId:       o.modelID,
		LedgerEntryId: ledgerID,
		ErrorCode:     firstNonEmpty(kerr.GetReason(), status.Code(err).String()),
		ErrorMetadata: metadata,
		ErrorMessage:  status.Convert(err).Message(),
		RouteVersion:  o.route,
		ReleaseId:     o.releaseID,
	}
	return o.send(event)
}

func (o *grpcStreamObserver) rememberResponse(response *llmgwv1.ChatResponse) {
	o.logicalID = firstNonEmpty(response.GetLogicalCallId(), o.logicalID)
	o.attemptID = firstNonEmpty(successfulAttemptID(response), o.attemptID)
	o.ledgerID = firstNonEmpty(response.GetLedgerEntryId(), o.ledgerID)
	o.modelID = firstNonEmpty(response.GetModel(), o.modelID)
	o.route = firstNonEmpty(response.GetRouteVersion(), o.route)
	o.priceTable = firstNonEmpty(response.GetPriceTableVersion(), o.priceTable)
	o.releaseID = firstNonEmpty(response.GetReleaseId(), o.releaseID)
}

func (o *grpcStreamObserver) send(event *llmgwv1.ChatStreamEvent) error {
	return o.sendWithLegacy(event, "", false)
}

func (o *grpcStreamObserver) sendWithLegacy(
	event *llmgwv1.ChatStreamEvent,
	content string,
	done bool,
) error {
	o.sequence++
	event.Sequence = o.sequence
	return o.server.Send(&llmgwv1.ChatDelta{
		Content: content,
		Done:    done,
		Event:   event,
	})
}

func successfulAttemptID(response *llmgwv1.ChatResponse) string {
	for _, attempt := range response.GetAttempts() {
		if attempt.GetStatus() == "SUCCEEDED" {
			return attempt.GetAttemptId()
		}
	}
	return ""
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}
