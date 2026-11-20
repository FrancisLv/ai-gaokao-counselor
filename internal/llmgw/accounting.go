package llmgw

import (
	"errors"
	"math"
	"math/big"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

func actualCost(model modelPrice, inputTokens, outputTokens int64) (int64, error) {
	inputCost, err := costForTokens(inputTokens, model.inputRate)
	if err != nil {
		return 0, err
	}
	outputCost, err := costForTokens(outputTokens, model.outputRate)
	if err != nil {
		return 0, err
	}
	return checkedAdd(inputCost, outputCost)
}

func maxAttemptCost(models []modelPrice, inputBound, outputBound int64) (int64, error) {
	var maximum int64
	for _, model := range models {
		cost, err := actualCost(model, inputBound, outputBound)
		if err != nil {
			return 0, err
		}
		if cost > maximum {
			maximum = cost
		}
	}
	return maximum, nil
}

func effectiveAttemptLimit(route routeConfig) int32 {
	if route.maxAttempts < 1 {
		return 1
	}
	if route.maxAttempts > maxProviderAttempts {
		return maxProviderAttempts
	}
	return route.maxAttempts
}

func costForTokens(tokens, microUSDPerMillion int64) (int64, error) {
	if tokens < 0 || microUSDPerMillion < 0 {
		return 0, errors.New("negative token or rate")
	}
	if tokens == 0 || microUSDPerMillion == 0 {
		return 0, nil
	}
	if tokens > (math.MaxInt64-999_999)/microUSDPerMillion {
		return 0, errors.New("token cost overflow")
	}
	return (tokens*microUSDPerMillion + 999_999) / 1_000_000, nil
}

func checkedAdd(left, right int64) (int64, error) {
	if left < 0 || right < 0 || left > math.MaxInt64-right {
		return 0, errors.New("integer addition overflow")
	}
	return left + right, nil
}

func checkedMul(value, multiplier int64) (int64, bool) {
	if value < 0 || multiplier < 0 || (value != 0 && multiplier > math.MaxInt64/value) {
		return 0, false
	}
	return value * multiplier, true
}

func isUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23505"
}

func valueOrEmpty(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func databaseUUID(value string) pgtype.UUID {
	parsed, err := uuid.Parse(value)
	if err != nil {
		return pgtype.UUID{}
	}
	return pgtype.UUID{Bytes: parsed, Valid: true}
}

func databaseText(value string) pgtype.Text {
	return pgtype.Text{String: value, Valid: value != ""}
}

func databaseInt8(value int64) pgtype.Int8 {
	return pgtype.Int8{Int64: value, Valid: true}
}

func nullableInt8(value *int64) pgtype.Int8 {
	if value == nil {
		return pgtype.Int8{}
	}
	return databaseInt8(*value)
}

func databaseInt4(value int32) pgtype.Int4 {
	return pgtype.Int4{Int32: value, Valid: true}
}

func databaseNumericMicroUSD(value int64) pgtype.Numeric {
	return pgtype.Numeric{Int: big.NewInt(value), Exp: -6, Valid: true}
}

func databaseTimestamp(value time.Time) pgtype.Timestamptz {
	return pgtype.Timestamptz{Time: value, Valid: true}
}

func nullableTimestamp(value *time.Time) pgtype.Timestamptz {
	if value == nil {
		return pgtype.Timestamptz{}
	}
	return databaseTimestamp(*value)
}

func textPointer(value pgtype.Text) *string {
	if !value.Valid {
		return nil
	}
	return &value.String
}

func int8Pointer(value pgtype.Int8) *int64 {
	if !value.Valid {
		return nil
	}
	return &value.Int64
}

func trustedProviderUsage(response ProviderResponse) bool {
	return response.UsageStatus == ProviderUsageTrusted
}

func reconciliationMetadata(logicalCallID, attemptID, ledgerEntryID string) map[string]string {
	return map[string]string{
		"logical_call_id": logicalCallID,
		"attempt_id":      attemptID,
		"ledger_entry_id": ledgerEntryID,
	}
}

func withStableErrorMetadata(err error, errorMetadata map[string]string) error {
	var stable *stableError
	if !errors.As(err, &stable) || len(errorMetadata) == 0 {
		return err
	}
	return &stableError{
		err:  stable.err.WithMetadata(errorMetadata),
		code: stable.code,
	}
}
