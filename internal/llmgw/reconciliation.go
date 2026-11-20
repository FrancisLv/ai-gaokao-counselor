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

	llmgwdb "github.com/choucisan/ai-gaokao-counselor/internal/llmgw/dbgen"
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
	queries := llmgwdb.New(tx)

	row, err := loadReconciliationRow(ctx, queries, request)
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
	existing, found, err := loadExistingReconciliation(ctx, queries, request, row)
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
		affected, execErr := queries.ReleaseBudgetForReconciliation(
			ctx,
			llmgwdb.ReleaseBudgetForReconciliationParams{
				ProviderTokensReserved: row.logicalReservedTok,
				CostReservedMicroUsd:   row.logicalReservedCost,
				ProviderTokensSettled:  valueOrZero(inputTokens) + valueOrZero(outputTokens),
				CostSettledMicroUsd:    valueOrZero(costMicroUSD),
				UpdatedAt:              databaseTimestamp(reconciledAt),
				Column6:                databaseUUID(row.budgetScopeID),
			},
		)
		if execErr != nil {
			return ReconciliationResult{}, newStableError(
				codes.Internal, 500, "INTERNAL", "应用对账预算失败",
			)
		}
		if affected != 1 {
			return ReconciliationResult{}, newStableError(
				codes.Internal, 500, "INTERNAL", "对账预算预留状态不一致",
			)
		}
	} else {
		affected, execErr := queries.LockBudgetScopeOverrun(
			ctx,
			llmgwdb.LockBudgetScopeOverrunParams{
				UpdatedAt: databaseTimestamp(reconciledAt),
				Column2:   databaseUUID(row.budgetScopeID),
			},
		)
		if execErr != nil {
			return ReconciliationResult{}, newStableError(
				codes.Internal, 500, "INTERNAL", "封锁对账超限预算作用域失败",
			)
		}
		if affected != 1 {
			return ReconciliationResult{}, newStableError(
				codes.Internal, 500, "INTERNAL", "对账超限预算作用域状态不一致",
			)
		}
	}

	affected, err := queries.UpdateReconciledAttempt(
		ctx,
		llmgwdb.UpdateReconciledAttemptParams{
			Status:       attemptState,
			InputTokens:  nullableInt8(inputTokens),
			OutputTokens: nullableInt8(outputTokens),
			CostMicroUsd: nullableInt8(costMicroUSD),
			ErrorCode:    databaseText(valueOrEmpty(attemptError)),
			Column6:      databaseUUID(request.AttemptID),
		},
	)
	if err != nil {
		return ReconciliationResult{}, newStableError(
			codes.Internal, 500, "INTERNAL", "更新对账 attempt 失败",
		)
	}
	if affected != 1 {
		return ReconciliationResult{}, newStableError(
			codes.Aborted, 409, ReasonReconciliationConflict,
			"待对账 attempt 状态已变化",
		)
	}

	affected, err = queries.UpdateReconciledLogicalCall(
		ctx,
		llmgwdb.UpdateReconciledLogicalCallParams{
			Status:             logicalState,
			ActualInputTokens:  nullableInt8(inputTokens),
			ActualOutputTokens: nullableInt8(outputTokens),
			ActualCostMicroUsd: nullableInt8(costMicroUSD),
			ErrorReason:        databaseText(logicalError),
			CompletedAt:        nullableTimestamp(completedAt),
			UpdatedAt:          databaseTimestamp(reconciledAt),
			Column8:            databaseUUID(row.logicalCallPK),
		},
	)
	if err != nil {
		return ReconciliationResult{}, newStableError(
			codes.Internal, 500, "INTERNAL", "更新对账逻辑调用失败",
		)
	}
	if affected != 1 {
		return ReconciliationResult{}, newStableError(
			codes.Aborted, 409, ReasonReconciliationConflict,
			"待对账逻辑调用状态已变化",
		)
	}

	if err := queries.InsertReconciliationAudit(
		ctx,
		llmgwdb.InsertReconciliationAuditParams{
			Column1:                      databaseUUID(request.AttemptID),
			Column2:                      databaseUUID(row.logicalCallPK),
			LogicalCallID:                row.logicalCallID,
			Outcome:                      outcome,
			PreviousAttemptStatus:        row.attemptStatus,
			Column6:                      nullableText(row.attemptErrorCode),
			PreviousReservedTokens:       row.attemptReservedTok,
			PreviousReservedCostMicroUsd: row.attemptReservedCost,
			InputTokens:                  nullableInt8(inputTokens),
			OutputTokens:                 nullableInt8(outputTokens),
			CostMicroUsd:                 nullableInt8(costMicroUSD),
			EvidenceRef:                  strings.TrimSpace(request.EvidenceRef),
			ReconciledAt:                 databaseTimestamp(reconciledAt),
		},
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
	queries *llmgwdb.Queries,
	request ReconciliationRequest,
) (reconciliationRow, error) {
	result, err := queries.LockReconciliationRow(
		ctx,
		llmgwdb.LockReconciliationRowParams{
			LogicalCallID: request.LogicalCallID,
			Column2:       databaseUUID(request.AttemptID),
		},
	)
	if err != nil {
		return reconciliationRow{}, err
	}
	return reconciliationRow{
		logicalCallPK:       result.CallLogicalCallPk,
		logicalCallID:       result.LogicalCallID,
		ledgerEntryID:       result.CallLedgerEntryID,
		logicalStatus:       result.LogicalStatus,
		logicalReservedCost: result.LogicalReservedCost,
		logicalReservedTok:  result.LogicalReservedTokens,
		budgetScopeID:       result.CallBudgetScopeID,
		attemptStatus:       result.AttemptStatus,
		attemptNo:           result.AttemptNo,
		attemptReservedCost: result.AttemptReservedCost,
		attemptReservedTok:  result.AttemptReservedTokens,
		attemptErrorCode:    result.AttemptErrorCode,
		budgetStatus:        result.BudgetStatus,
		inputRate:           result.InputRate,
		outputRate:          result.OutputRate,
	}, nil
}

func loadExistingReconciliation(
	ctx context.Context,
	queries *llmgwdb.Queries,
	request ReconciliationRequest,
	row reconciliationRow,
) (ReconciliationResult, bool, error) {
	result, err := queries.GetExistingReconciliation(ctx, databaseUUID(request.AttemptID))
	if errors.Is(err, pgx.ErrNoRows) {
		return ReconciliationResult{}, false, nil
	}
	if err != nil {
		return ReconciliationResult{}, false, newStableError(
			codes.Internal, 500, "INTERNAL", "读取已有对账结果失败",
		)
	}

	inputTokens := int8Pointer(result.InputTokens)
	outputTokens := int8Pointer(result.OutputTokens)
	costMicroUSD := int8Pointer(result.CostMicroUsd)
	matches := result.EvidenceRef == strings.TrimSpace(request.EvidenceRef)
	switch request.Outcome {
	case ReconciliationCharged:
		matches = matches &&
			(result.Outcome == "CHARGED" || result.Outcome == "CHARGED_OVERRUN") &&
			inputTokens != nil && *inputTokens == request.InputTokens &&
			outputTokens != nil && *outputTokens == request.OutputTokens
	case ReconciliationNoCharge:
		matches = matches &&
			result.Outcome == "NO_CHARGE" &&
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
