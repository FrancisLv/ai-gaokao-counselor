package llmgw

import (
	"context"
	"errors"

	"google.golang.org/grpc/codes"

	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
)

func (s *Service) executeProviderAttempts(
	ctx context.Context,
	prepared *preparedCall,
	observer callObserver,
) (*llmgwv1.ChatResponse, error) {
	candidateCount := len(prepared.route.models)
	if int64(candidateCount) > int64(effectiveAttemptLimit(prepared.route)) {
		candidateCount = int(effectiveAttemptLimit(prepared.route))
	}
	for candidateIndex := 0; candidateIndex < candidateCount; candidateIndex++ {
		if candidateIndex > 0 {
			if err := s.startAttempt(ctx, prepared, prepared.route.models[candidateIndex], int32(candidateIndex+1)); err != nil {
				return nil, err
			}
		}

		providerCtx, cancel := context.WithTimeout(ctx, prepared.providerRequest.Timeout)
		providerResponse, providerErr := s.provider.Chat(providerCtx, prepared.providerRequest)
		cancel()
		if providerErr != nil {
			if observer != nil && ctx.Err() != nil &&
				errors.Is(providerErr, context.Canceled) {
				return nil, s.markProviderUnknownWithReason(
					ctx, prepared, providerErr, ReasonStreamInterrupted,
				)
			}
			var failure *ProviderFailure
			if errors.As(providerErr, &failure) {
				if failure.Retryable && candidateIndex+1 < candidateCount {
					if err := s.finishRetryableAttempt(ctx, prepared, failure); err != nil {
						return nil, err
					}
					continue
				}
				return nil, s.releaseProviderFailure(ctx, prepared, failure)
			}
			if errors.Is(providerErr, ErrProviderNotCharged) {
				return nil, s.releaseUnchargedCall(ctx, prepared)
			}
			return nil, s.markProviderUnknown(ctx, prepared, providerErr)
		}
		if !trustedProviderUsage(providerResponse) {
			return nil, s.markProviderUnknown(
				ctx, prepared, errors.New("provider usage is missing or untrusted"),
			)
		}

		result := providerResult{
			Content:           providerResponse.Content,
			InputTokens:       providerResponse.InputTokens,
			OutputTokens:      providerResponse.OutputTokens,
			ProviderRequestID: providerResponse.ProviderRequestID,
			FinishReason:      providerResponse.FinishReason,
		}
		if result.FinishReason == "" {
			result.FinishReason = "STOP"
		}
		if result.InputTokens < 0 || result.OutputTokens < 0 {
			return nil, s.markProviderUnknown(ctx, prepared, errors.New("provider returned negative usage"))
		}

		actualCostMicroUSD, err := actualCost(prepared.selected, result.InputTokens, result.OutputTokens)
		if err != nil {
			return nil, s.markProviderUnknown(ctx, prepared, err)
		}
		actualTokens, err := checkedAdd(result.InputTokens, result.OutputTokens)
		if err != nil {
			return nil, s.markProviderUnknown(ctx, prepared, err)
		}
		if containsUnsafeProviderOutput(result) {
			return nil, s.settleSensitiveOutputFailure(
				ctx, prepared, result, actualCostMicroUSD, actualTokens,
			)
		}
		if actualCostMicroUSD > prepared.reservedCost || actualTokens > prepared.reservedTokens {
			return nil, s.markBudgetOverrun(ctx, prepared, result, actualCostMicroUSD)
		}
		if prepared.schema != nil {
			structuredOutput, err := validateStructuredOutput(prepared.schema, result.Content)
			if err != nil {
				return nil, s.settleSchemaFailure(
					ctx, prepared, result, actualCostMicroUSD, actualTokens,
				)
			}
			result.StructuredOutput = structuredOutput
			if containsUnsafeProviderOutput(result) {
				return nil, s.settleSensitiveOutputFailure(
					ctx, prepared, result, actualCostMicroUSD, actualTokens,
				)
			}
		}

		response, err := s.settleCall(ctx, prepared, result, actualCostMicroUSD, actualTokens)
		if err != nil {
			return nil, err
		}
		s.insertProjection(ctx, prepared, result, actualCostMicroUSD)
		return response, nil
	}
	return nil, newStableError(codes.Internal, 500, "INTERNAL", "route 未配置可用模型")
}
