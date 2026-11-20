package llmgw

import (
	"context"
	"time"

	"google.golang.org/grpc/codes"

	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
)

func (s *Service) executeGovernedCall(
	ctx context.Context,
	req *llmgwv1.ChatRequest,
	observer callObserver,
) (*llmgwv1.ChatResponse, error) {
	principalID, err := requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	if s.pool == nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "模型网关数据库不可用")
	}
	governed, err := normalizeRequest(principalID, req)
	if err != nil {
		return nil, err
	}
	if err := rejectUnsafeInput(governed); err != nil {
		return nil, err
	}

	binding, err := s.loadRunBinding(ctx, governed)
	if err != nil {
		return nil, err
	}
	route, err := s.loadRoute(ctx, governed, binding)
	if err != nil {
		return nil, err
	}
	if err := validateRouteDataClassification(route, governed.DataClassification); err != nil {
		return nil, err
	}
	if err := s.ensureBudgetScopeActive(ctx, governed); err != nil {
		return nil, err
	}
	prompt, err := s.loadPromptTemplate(ctx, governed)
	if err != nil {
		return nil, err
	}
	if err := rejectUnsafeRenderedMessages(prompt.messages); err != nil {
		return nil, err
	}
	schema, err := s.loadResponseSchema(ctx, governed, route)
	if err != nil {
		return nil, err
	}
	governed.Messages = prompt.messages
	requestHash, inputBound, err := canonicalHash(governed, route, prompt, schema)
	if err != nil {
		return nil, err
	}

	var (
		cacheKeyHash string
		cacheClaim   *resultCacheClaim
	)
	if resultCacheEligible(governed, route) {
		identity, err := buildResultCacheIdentity(governed, route, prompt, schema)
		if err != nil {
			return nil, err
		}
		cacheKeyHash = identity.keyHash
		var cached *resultCacheEntry
		cacheClaimLease := time.Duration(route.timeoutMS)*
			time.Duration(effectiveAttemptLimit(route))*time.Millisecond +
			30*time.Second
		cacheClaim, cached, err = s.claimResultCache(ctx, identity, cacheClaimLease)
		if err != nil {
			return nil, err
		}
		if cached != nil {
			response, err := s.recordResultCacheHit(
				ctx, governed, route, prompt, requestHash, identity.keyHash, cached,
			)
			if err != nil {
				return nil, err
			}
			if err := observeResult(ctx, observer, response); err != nil {
				return nil, err
			}
			return response, nil
		}
	}

	prepared, replayed, err := s.reserveCall(
		ctx, governed, route, prompt, schema, requestHash, inputBound,
	)
	if err != nil {
		if cacheClaim != nil {
			s.failResultCache(cacheClaim)
		}
		return nil, err
	}
	if replayed != nil {
		if cacheClaim != nil {
			s.failResultCache(cacheClaim)
		}
		if replayed != nil {
			if err := observeResult(ctx, observer, replayed); err != nil {
				return nil, err
			}
		}
		return replayed, nil
	}
	prepared.cacheKeyHash = cacheKeyHash
	if cacheClaim != nil {
		prepared.cacheClaimToken = cacheClaim.token
	}
	if err := observeReservation(ctx, observer, prepared); err != nil {
		releaseErr := s.releaseKnownUnchargedFailure(
			ctx,
			prepared,
			"",
			"STREAM_START_FAILED",
			newStableError(
				codes.Unavailable,
				503,
				ReasonStreamStartFailed,
				"流开始事件发送失败，供应商未被调用且未计费",
			),
		)
		if cacheClaim != nil {
			s.failResultCache(cacheClaim)
		}
		return nil, releaseErr
	}

	response, err := s.executeProviderAttempts(ctx, prepared, observer)
	if err != nil && cacheClaim != nil {
		s.failResultCache(cacheClaim)
	}
	if err == nil {
		if observeErr := observeResult(ctx, observer, response); observeErr != nil {
			return nil, observeErr
		}
	}
	return response, err
}
