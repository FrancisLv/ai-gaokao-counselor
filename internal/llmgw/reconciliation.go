package llmgw

import (
	"context"
	"errors"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc/codes"
)

type ReconciliationOutcome string

const (
	ReconciliationCharged  ReconciliationOutcome = "CHARGED"
	ReconciliationNoCharge ReconciliationOutcome = "NO_CHARGE"
)

const (
	ReasonReconciliationConflict   = "RECONCILIATION_CONFLICT"
	ReasonReconciliationNotPending = "RECONCILIATION_NOT_PENDING"
)

// ReconciliationRequest is an internal operator adapter input. It is not part
// of the public LLMGateway RPC surface.
type ReconciliationRequest struct {
	LogicalCallID string
	AttemptID     string
	Outcome       ReconciliationOutcome
	InputTokens   int64
	OutputTokens  int64
	EvidenceRef   string
}

type ReconciliationResult struct {
	LogicalCallID string
	AttemptID     string
	LedgerEntryID string
	Outcome       ReconciliationOutcome
	LogicalStatus string
	AttemptStatus string
	BudgetStatus  string
	InputTokens   int64
	OutputTokens  int64
	CostMicroUSD  int64
}

type Reconciler struct {
	pool *pgxpool.Pool
	now  func() time.Time
}

func NewReconciler(pool *pgxpool.Pool) *Reconciler {
	return &Reconciler{pool: pool, now: time.Now}
}

type reconciliationRow struct {
	logicalCallPK       string
	logicalCallID       string
	ledgerEntryID       string
	logicalStatus       string
	logicalReservedCost int64
	logicalReservedTok  int64
	budgetScopeID       string
	attemptStatus       string
	attemptNo           int32
	attemptReservedCost int64
	attemptReservedTok  int64
	attemptErrorCode    string
	budgetStatus        string
	inputRate           int64
	outputRate          int64
}

// Reconcile applies exactly one explicit accounting outcome to a pending
// provider attempt. Repeated equivalent calls return the original result.
func (r *Reconciler) Reconcile(
	ctx context.Context,
	request ReconciliationRequest,
) (ReconciliationResult, error) {
	if err := validateReconciliationRequest(request); err != nil {
		return ReconciliationResult{}, err
	}
	if r == nil || r.pool == nil {
		return ReconciliationResult{}, newStableError(
			codes.Internal, 500, "INTERNAL", "模型网关对账数据库不可用",
		)
	}

	tx, err := r.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return ReconciliationResult{}, newStableError(
			codes.Internal, 500, "INTERNAL", "启动对账事务失败",
		)
	}
	defer tx.Rollback(ctx)

	row, err := loadReconciliationRow(ctx, tx, request)
	if errors.Is(err, pgx.ErrNoRows) {
		return ReconciliationResult{}, newStableError(
			codes.NotFound, 404, ReasonReconciliationNotPending,
			"未找到指定 logical/attempt 的待对账记录",
		)
	}
	if err != nil {
		return ReconciliationResult{}, newStableError(
			codes.Internal, 500, "INTERNAL", "读取待对账记录失败",
		)
	}
	if row.logicalReservedTok < 0 {
		return ReconciliationResult{}, newStableError(
			codes.FailedPrecondition, 412, ReasonReconciliationNotPending,
			"历史逻辑调用缺少可审计的 token 预占总额",
		)
	}
	existing, found, err := loadExistingReconciliation(ctx, tx, request, row)
	if err != nil {
		return ReconciliationResult{}, err
	}
	if found {
		return existing, nil
	}
	if row.attemptStatus != "PENDING_RECONCILIATION" {
		return ReconciliationResult{}, newStableError(
			codes.Aborted, 409, ReasonReconciliationNotPending,
			"attempt 已不是待对账状态且缺少对账审计",
		)
	}

	reconciledAt := r.now()
	var (
		outcome       string
		inputTokens   *int64
		outputTokens  *int64
		costMicroUSD  *int64
		attemptState  string
		logicalState  string
		budgetState   string
		logicalError  string
		attemptError  *string
		completedAt   *time.Time
		releaseBudget = request.Outcome == ReconciliationNoCharge
	)

	switch request.Outcome {
	case ReconciliationNoCharge:
		outcome = string(ReconciliationNoCharge)
		attemptState = "FAILED_NO_CHARGE"
		logicalState = "FAILED"
		budgetState = row.budgetStatus
		logicalError = "RECONCILED_NO_CHARGE"
		completedAt = &reconciledAt
		noChargeError := logicalError
		attemptError = &noChargeError
	case ReconciliationCharged:
		actualCost, costErr := actualCost(
			modelPrice{inputRate: row.inputRate, outputRate: row.outputRate},
			request.InputTokens,
			request.OutputTokens,
		)
		if costErr != nil {
			return ReconciliationResult{}, newStableError(
				codes.InvalidArgument, 400, ReasonRequestInvalid,
				"明确账单 usage 无法计算成本",
			)
		}
		actualTokens, tokenErr := checkedAdd(request.InputTokens, request.OutputTokens)
		if tokenErr != nil {
			return ReconciliationResult{}, newStableError(
				codes.InvalidArgument, 400, ReasonRequestInvalid,
				"明确账单 usage 溢出",
			)
		}
		inputTokens = &request.InputTokens
		outputTokens = &request.OutputTokens
		costMicroUSD = &actualCost
		attemptError = nil
		if actualCost > row.logicalReservedCost || actualTokens > row.logicalReservedTok {
			outcome = string(ReconciliationCharged) + "_OVERRUN"
			attemptState = "PENDING_RECONCILIATION"
			logicalState = "BUDGET_OVERRUN"
			budgetState = "BUDGET_OVERRUN"
			logicalError = ReasonBudgetOverrun
			overrunError := ReasonBudgetOverrun
			attemptError = &overrunError
		} else {
			outcome = string(ReconciliationCharged)
			attemptState = "SUCCEEDED"
			logicalState = "FAILED"
			budgetState = row.budgetStatus
			logicalError = "RECONCILED_CHARGED"
			completedAt = &reconciledAt
			releaseBudget = true
		}
	default:
		return ReconciliationResult{}, newStableError(
			codes.InvalidArgument, 400, ReasonRequestInvalid,
			"不支持的对账结果",
		)
	}

	if releaseBudget {
		tag, execErr := tx.Exec(ctx, `
			UPDATE llm_budget_scope
			SET provider_tokens_reserved = provider_tokens_reserved - $1,
			    cost_reserved_micro_usd = cost_reserved_micro_usd - $2,
			    provider_tokens_settled = provider_tokens_settled + $3,
			    cost_settled_micro_usd = cost_settled_micro_usd + $4,
			    updated_at = $5
			WHERE budget_scope_id = $6
			  AND provider_tokens_reserved >= $1
			  AND cost_reserved_micro_usd >= $2`,
			row.logicalReservedTok, row.logicalReservedCost,
			valueOrZero(inputTokens)+valueOrZero(outputTokens),
			valueOrZero(costMicroUSD), reconciledAt, row.budgetScopeID,
		)
		if execErr != nil {
			return ReconciliationResult{}, newStableError(
				codes.Internal, 500, "INTERNAL", "应用对账预算失败",
			)
		}
		if tag.RowsAffected() != 1 {
			return ReconciliationResult{}, newStableError(
				codes.Internal, 500, "INTERNAL", "对账预算预留状态不一致",
			)
		}
	} else {
		tag, execErr := tx.Exec(ctx, `
			UPDATE llm_budget_scope
			SET budget_status = 'BUDGET_OVERRUN',
			    overrun_at = COALESCE(overrun_at, $1),
			    overrun_reason = 'RECONCILIATION_EXCEEDS_RESERVATION',
			    updated_at = $1
			WHERE budget_scope_id = $2`,
			reconciledAt, row.budgetScopeID,
		)
		if execErr != nil {
			return ReconciliationResult{}, newStableError(
				codes.Internal, 500, "INTERNAL", "封锁对账超限预算作用域失败",
			)
		}
		if tag.RowsAffected() != 1 {
			return ReconciliationResult{}, newStableError(
				codes.Internal, 500, "INTERNAL", "对账超限预算作用域状态不一致",
			)
		}
	}

	tag, err := tx.Exec(ctx, `
		UPDATE llm_provider_attempt
		SET status = $1,
		    input_tokens = $2,
		    output_tokens = $3,
		    cost_micro_usd = $4,
		    error_code = $5
		WHERE attempt_id = $6
		  AND status = 'PENDING_RECONCILIATION'`,
		attemptState, inputTokens, outputTokens, costMicroUSD,
		attemptError, request.AttemptID,
	)
	if err != nil {
		return ReconciliationResult{}, newStableError(
			codes.Internal, 500, "INTERNAL", "更新对账 attempt 失败",
		)
	}
	if tag.RowsAffected() != 1 {
		return ReconciliationResult{}, newStableError(
			codes.Aborted, 409, ReasonReconciliationConflict,
			"待对账 attempt 状态已变化",
		)
	}

	tag, err = tx.Exec(ctx, `
		UPDATE llm_logical_call
		SET status = $1,
		    actual_input_tokens = $2,
		    actual_output_tokens = $3,
		    actual_cost_micro_usd = $4,
		    error_reason = $5,
		    completed_at = $6,
		    updated_at = $7
		WHERE logical_call_pk = $8
		  AND status = 'PARTIAL_UNKNOWN'`,
		logicalState, inputTokens, outputTokens, costMicroUSD,
		logicalError, completedAt, reconciledAt, row.logicalCallPK,
	)
	if err != nil {
		return ReconciliationResult{}, newStableError(
			codes.Internal, 500, "INTERNAL", "更新对账逻辑调用失败",
		)
	}
	if tag.RowsAffected() != 1 {
		return ReconciliationResult{}, newStableError(
			codes.Aborted, 409, ReasonReconciliationConflict,
			"待对账逻辑调用状态已变化",
		)
	}

	if _, err := tx.Exec(ctx, `
		INSERT INTO llm_attempt_reconciliation (
			attempt_id, logical_call_pk, logical_call_id, outcome,
			previous_attempt_status, previous_error_code,
			previous_reserved_tokens, previous_reserved_cost_micro_usd,
			input_tokens, output_tokens, cost_micro_usd,
			evidence_ref, reconciled_at
		) VALUES (
			$1, $2, $3, $4, $5, NULLIF($6, ''), $7, $8, $9, $10, $11, $12, $13
		)`,
		request.AttemptID, row.logicalCallPK, row.logicalCallID, outcome,
		row.attemptStatus, row.attemptErrorCode,
		row.attemptReservedTok, row.attemptReservedCost,
		inputTokens, outputTokens, costMicroUSD,
		strings.TrimSpace(request.EvidenceRef), reconciledAt,
	); err != nil {
		return ReconciliationResult{}, newStableError(
			codes.Internal, 500, "INTERNAL", "记录对账审计依据失败",
		)
	}

	result := ReconciliationResult{
		LogicalCallID: row.logicalCallID,
		AttemptID:     request.AttemptID,
		LedgerEntryID: row.ledgerEntryID,
		Outcome:       request.Outcome,
		LogicalStatus: logicalState,
		AttemptStatus: attemptState,
		BudgetStatus:  budgetState,
		InputTokens:   valueOrZero(inputTokens),
		OutputTokens:  valueOrZero(outputTokens),
		CostMicroUSD:  valueOrZero(costMicroUSD),
	}
	if err := tx.Commit(ctx); err != nil {
		return ReconciliationResult{}, newStableError(
			codes.Internal, 500, "INTERNAL", "提交对账事务失败",
		)
	}
	return result, nil
}

func validateReconciliationRequest(request ReconciliationRequest) error {
	if _, err := uuid.Parse(request.AttemptID); err != nil ||
		strings.TrimSpace(request.LogicalCallID) == "" ||
		strings.TrimSpace(request.EvidenceRef) == "" {
		return newStableError(
			codes.InvalidArgument, 400, ReasonRequestInvalid,
			"对账缺少 logical/attempt 或证据引用",
		)
	}
	switch request.Outcome {
	case ReconciliationNoCharge:
		if request.InputTokens != 0 || request.OutputTokens != 0 {
			return newStableError(
				codes.InvalidArgument, 400, ReasonRequestInvalid,
				"免计费对账不得携带 usage",
			)
		}
	case ReconciliationCharged:
		if request.InputTokens < 0 || request.OutputTokens < 0 {
			return newStableError(
				codes.InvalidArgument, 400, ReasonRequestInvalid,
				"明确账单 usage 不能为负数",
			)
		}
	default:
		return newStableError(
			codes.InvalidArgument, 400, ReasonRequestInvalid,
			"不支持的对账结果",
		)
	}
	return nil
}

func loadReconciliationRow(
	ctx context.Context,
	tx pgx.Tx,
	request ReconciliationRequest,
) (reconciliationRow, error) {
	var row reconciliationRow
	err := tx.QueryRow(ctx, `
		SELECT call.logical_call_pk::text,
		       call.logical_call_id,
		       call.ledger_entry_id::text,
		       call.status,
		       call.reserved_cost_micro_usd,
		       COALESCE(call.reserved_provider_tokens, -1),
		       call.budget_scope_id::text,
		       attempt.status,
		       attempt.attempt_no,
		       attempt.reserved_cost_micro_usd,
		       attempt.reserved_tokens,
		       COALESCE(attempt.error_code, ''),
		       scope.budget_status,
		       price.input_micro_usd_per_million_tokens,
		       price.output_micro_usd_per_million_tokens
		FROM llm_provider_attempt attempt
		JOIN llm_logical_call call
		  ON call.logical_call_pk = attempt.logical_call_pk
		JOIN llm_budget_scope scope
		  ON scope.budget_scope_id = call.budget_scope_id
		JOIN llm_price_entry price
		  ON price.price_table_version = call.price_table_version
		 AND price.model_id = attempt.model_id
		WHERE call.logical_call_id = $1
		  AND attempt.attempt_id = $2
		FOR UPDATE OF attempt, call, scope`,
		request.LogicalCallID, request.AttemptID,
	).Scan(
		&row.logicalCallPK, &row.logicalCallID, &row.ledgerEntryID,
		&row.logicalStatus, &row.logicalReservedCost, &row.logicalReservedTok,
		&row.budgetScopeID, &row.attemptStatus, &row.attemptNo,
		&row.attemptReservedCost, &row.attemptReservedTok,
		&row.attemptErrorCode, &row.budgetStatus,
		&row.inputRate, &row.outputRate,
	)
	return row, err
}

func loadExistingReconciliation(
	ctx context.Context,
	tx pgx.Tx,
	request ReconciliationRequest,
	row reconciliationRow,
) (ReconciliationResult, bool, error) {
	var (
		outcome      string
		inputTokens  *int64
		outputTokens *int64
		costMicroUSD *int64
		evidenceRef  string
	)
	err := tx.QueryRow(ctx, `
		SELECT outcome, input_tokens, output_tokens, cost_micro_usd, evidence_ref
		FROM llm_attempt_reconciliation
		WHERE attempt_id = $1`,
		request.AttemptID,
	).Scan(&outcome, &inputTokens, &outputTokens, &costMicroUSD, &evidenceRef)
	if errors.Is(err, pgx.ErrNoRows) {
		return ReconciliationResult{}, false, nil
	}
	if err != nil {
		return ReconciliationResult{}, false, newStableError(
			codes.Internal, 500, "INTERNAL", "读取已有对账结果失败",
		)
	}

	matches := evidenceRef == strings.TrimSpace(request.EvidenceRef)
	switch request.Outcome {
	case ReconciliationCharged:
		matches = matches &&
			(outcome == "CHARGED" || outcome == "CHARGED_OVERRUN") &&
			inputTokens != nil && *inputTokens == request.InputTokens &&
			outputTokens != nil && *outputTokens == request.OutputTokens
	case ReconciliationNoCharge:
		matches = matches &&
			outcome == "NO_CHARGE" &&
			inputTokens == nil && outputTokens == nil && costMicroUSD == nil
	}
	if !matches {
		return ReconciliationResult{}, false, newStableError(
			codes.Aborted, 409, ReasonReconciliationConflict,
			"attempt 已按不同依据完成对账",
		)
	}
	return ReconciliationResult{
		LogicalCallID: row.logicalCallID,
		AttemptID:     request.AttemptID,
		LedgerEntryID: row.ledgerEntryID,
		Outcome:       request.Outcome,
		LogicalStatus: row.logicalStatus,
		AttemptStatus: row.attemptStatus,
		BudgetStatus:  row.budgetStatus,
		InputTokens:   valueOrZero(inputTokens),
		OutputTokens:  valueOrZero(outputTokens),
		CostMicroUSD:  valueOrZero(costMicroUSD),
	}, true, nil
}

func valueOrZero(value *int64) int64 {
	if value == nil {
		return 0
	}
	return *value
}
