package gateway

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base32"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/crypto/argon2"
	"golang.org/x/text/unicode/norm"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const (
	argonMemory        = 64 * 1024
	argonIterations    = 3
	argonParallelism   = 2
	argonSaltBytes     = 16
	argonKeyBytes      = 32
	loginFailureLimit  = 5
	loginFailureWindow = 15 * time.Minute
	loginBlockDuration = 15 * time.Minute
)

type PostgresIdentity struct {
	pool          *pgxpool.Pool
	clock         func() time.Time
	loginMu       sync.Mutex
	loginFailures map[string]loginFailure
}

type loginFailure struct {
	Count        int
	WindowStart  time.Time
	BlockedUntil time.Time
}

func NewPostgresIdentity(pool *pgxpool.Pool) (*PostgresIdentity, error) {
	if pool == nil {
		return nil, fmt.Errorf("PostgresIdentity requires a database pool")
	}
	return &PostgresIdentity{
		pool:          pool,
		clock:         time.Now,
		loginFailures: make(map[string]loginFailure),
	}, nil
}

func (p *PostgresIdentity) Register(
	ctx context.Context,
	input RegisterInput,
) (RegisterOutput, error) {
	username, normalized, nickname, err := validateIdentityFields(
		input.Username,
		input.Nickname,
		input.Password,
	)
	if err != nil {
		return RegisterOutput{}, err
	}
	if input.PrivacyPolicyVersion == "" || !input.GuardianAckRecorded {
		return RegisterOutput{}, status.Error(
			codes.InvalidArgument,
			"PRIVACY_ACK_REQUIRED",
		)
	}
	passwordHash, err := hashPassword(input.Password)
	if err != nil {
		return RegisterOutput{}, status.Error(codes.Internal, "PASSWORD_HASH_FAILED")
	}
	recoveryCodes, recoveryHashes, err := newRecoveryCodes(10)
	if err != nil {
		return RegisterOutput{}, status.Error(codes.Internal, "RECOVERY_CODE_FAILED")
	}
	sessionToken, sessionHash, err := newSessionToken()
	if err != nil {
		return RegisterOutput{}, status.Error(codes.Internal, "SESSION_CREATE_FAILED")
	}

	now := p.clock().UTC()
	expiresAt := now.Add(defaultSessionIdleTTL)
	absoluteExpiresAt := now.Add(defaultSessionAbsoluteTTL)
	accountID := uuid.New()
	inviteHash := tokenHash(strings.TrimSpace(input.InviteCode))

	tx, err := p.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return RegisterOutput{}, status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	defer func() { _ = tx.Rollback(ctx) }()

	var inviteExpiresAt time.Time
	var inviteStatus string
	if err := tx.QueryRow(ctx, `
		SELECT status, expires_at
		FROM invite_code
		WHERE code_hash = $1
		FOR UPDATE
	`, inviteHash).Scan(&inviteStatus, &inviteExpiresAt); err != nil {
		if err == pgx.ErrNoRows {
			return RegisterOutput{}, status.Error(codes.InvalidArgument, "INVITE_INVALID")
		}
		return RegisterOutput{}, status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	if inviteStatus != "AVAILABLE" || !inviteExpiresAt.After(now) {
		return RegisterOutput{}, status.Error(codes.InvalidArgument, "INVITE_INVALID")
	}
	var reserved bool
	if err := tx.QueryRow(ctx, `
		SELECT EXISTS (
			SELECT 1 FROM reserved_username WHERE username_hash = $1
		)
	`, tokenHash(normalized)).Scan(&reserved); err != nil {
		return RegisterOutput{}, status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	if reserved {
		return RegisterOutput{}, status.Error(codes.AlreadyExists, "USERNAME_RESERVED")
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO user_account (
			account_id, username, username_normalized, nickname, password_hash,
			status, privacy_policy_version, guardian_ack_recorded, last_login_at
		)
		VALUES ($1, $2, $3, $4, $5, 'ACTIVE', $6, $7, $8)
	`, accountID, username, normalized, nickname, passwordHash,
		input.PrivacyPolicyVersion, input.GuardianAckRecorded, now,
	); err != nil {
		if isUniqueViolation(err) {
			return RegisterOutput{}, status.Error(codes.AlreadyExists, "USERNAME_TAKEN")
		}
		return RegisterOutput{}, status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO account_consent (
			consent_id, account_id, purpose, policy_version, guardian_ack_recorded
		)
		VALUES ($1, $2, 'GENERAL_PRIVACY', $3, $4)
	`, uuid.New(), accountID, input.PrivacyPolicyVersion,
		input.GuardianAckRecorded,
	); err != nil {
		return RegisterOutput{}, status.Error(codes.Internal, "CONSENT_STORE_UNAVAILABLE")
	}
	for _, codeHash := range recoveryHashes {
		if _, err := tx.Exec(ctx, `
			INSERT INTO recovery_code (recovery_code_id, account_id, code_hash)
			VALUES ($1, $2, $3)
		`, uuid.New(), accountID, codeHash); err != nil {
			return RegisterOutput{}, status.Error(codes.Internal, "RECOVERY_CODE_STORE_FAILED")
		}
	}
	if _, err := tx.Exec(ctx, `
		UPDATE invite_code
		SET status = 'CONSUMED',
		    consumed_by_account_id = $2,
		    consumed_at = $3
		WHERE code_hash = $1
	`, inviteHash, accountID, now); err != nil {
		return RegisterOutput{}, status.Error(codes.Internal, "INVITE_CONSUME_FAILED")
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO login_session (
			login_session_id, account_id, token_hash,
			created_at, last_seen_at, idle_expires_at, absolute_expires_at
		)
		VALUES ($1, $2, $3, $4, $4, $5, $6)
	`, uuid.New(), accountID, sessionHash, now, expiresAt, absoluteExpiresAt,
	); err != nil {
		return RegisterOutput{}, status.Error(codes.Internal, "SESSION_STORE_FAILED")
	}
	if err := tx.Commit(ctx); err != nil {
		return RegisterOutput{}, status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	return RegisterOutput{
		Account: AuthenticatedAccount{
			AccountID: accountID.String(),
			Username:  username,
			Nickname:  nickname,
		},
		RecoveryCodes: recoveryCodes,
		SessionToken:  sessionToken,
		ExpiresAt:     expiresAt,
	}, nil
}

func (p *PostgresIdentity) Login(
	ctx context.Context,
	input LoginInput,
) (LoginOutput, error) {
	normalized := normalizeUsername(input.Username)
	loginKey := normalized + "|" + tokenHash(input.ClientIP)
	if err := p.checkLoginAllowed(loginKey); err != nil {
		return LoginOutput{}, err
	}
	var account AuthenticatedAccount
	var passwordHash, accountStatus string
	if err := p.pool.QueryRow(ctx, `
		SELECT account_id::text, username, nickname, password_hash, status
		FROM user_account
		WHERE username_normalized = $1
	`, normalized).Scan(
		&account.AccountID,
		&account.Username,
		&account.Nickname,
		&passwordHash,
		&accountStatus,
	); err != nil {
		if err == pgx.ErrNoRows {
			p.recordLoginFailure(loginKey)
			return LoginOutput{}, status.Error(codes.Unauthenticated, "LOGIN_INVALID")
		}
		return LoginOutput{}, status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	if accountStatus != "ACTIVE" || !verifyPassword(passwordHash, input.Password) {
		p.recordLoginFailure(loginKey)
		return LoginOutput{}, status.Error(codes.Unauthenticated, "LOGIN_INVALID")
	}
	p.clearLoginFailures(loginKey)
	sessionToken, sessionHash, err := newSessionToken()
	if err != nil {
		return LoginOutput{}, status.Error(codes.Internal, "SESSION_CREATE_FAILED")
	}
	now := p.clock().UTC()
	expiresAt := now.Add(defaultSessionIdleTTL)
	absoluteExpiresAt := now.Add(defaultSessionAbsoluteTTL)
	tx, err := p.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return LoginOutput{}, status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if _, err := tx.Exec(ctx, `
		INSERT INTO login_session (
			login_session_id, account_id, token_hash,
			created_at, last_seen_at, idle_expires_at, absolute_expires_at
		)
		VALUES ($1, $2::uuid, $3, $4, $4, $5, $6)
	`, uuid.New(), account.AccountID, sessionHash, now, expiresAt, absoluteExpiresAt,
	); err != nil {
		return LoginOutput{}, status.Error(codes.Internal, "SESSION_STORE_FAILED")
	}
	if _, err := tx.Exec(ctx, `
		UPDATE user_account SET last_login_at = $2, updated_at = $2
		WHERE account_id = $1::uuid
	`, account.AccountID, now); err != nil {
		return LoginOutput{}, status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	if err := tx.Commit(ctx); err != nil {
		return LoginOutput{}, status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	return LoginOutput{
		Account:      account,
		SessionToken: sessionToken,
		ExpiresAt:    expiresAt,
	}, nil
}

func (p *PostgresIdentity) checkLoginAllowed(key string) error {
	p.loginMu.Lock()
	defer p.loginMu.Unlock()
	failure, ok := p.loginFailures[key]
	if !ok {
		return nil
	}
	now := p.clock().UTC()
	if failure.BlockedUntil.After(now) {
		return status.Error(codes.ResourceExhausted, "LOGIN_RATE_LIMITED")
	}
	if now.Sub(failure.WindowStart) >= loginFailureWindow {
		delete(p.loginFailures, key)
	}
	return nil
}

func (p *PostgresIdentity) recordLoginFailure(key string) {
	p.loginMu.Lock()
	defer p.loginMu.Unlock()
	now := p.clock().UTC()
	failure := p.loginFailures[key]
	if failure.WindowStart.IsZero() ||
		now.Sub(failure.WindowStart) >= loginFailureWindow {
		failure = loginFailure{Count: 0, WindowStart: now}
	}
	failure.Count++
	if failure.Count >= loginFailureLimit {
		failure.BlockedUntil = now.Add(loginBlockDuration)
	}
	if len(p.loginFailures) > 10_000 {
		for currentKey, current := range p.loginFailures {
			if now.Sub(current.WindowStart) >= loginFailureWindow &&
				!current.BlockedUntil.After(now) {
				delete(p.loginFailures, currentKey)
			}
		}
	}
	p.loginFailures[key] = failure
}

func (p *PostgresIdentity) clearLoginFailures(key string) {
	p.loginMu.Lock()
	delete(p.loginFailures, key)
	p.loginMu.Unlock()
}

func (p *PostgresIdentity) ResolveSession(
	ctx context.Context,
	sessionToken string,
) (AuthenticatedAccount, error) {
	if strings.TrimSpace(sessionToken) == "" {
		return AuthenticatedAccount{}, ErrUnauthenticated
	}
	now := p.clock().UTC()
	var account AuthenticatedAccount
	err := p.pool.QueryRow(ctx, `
		UPDATE login_session AS session
		SET last_seen_at = $2,
		    idle_expires_at = LEAST($3, session.absolute_expires_at)
		FROM user_account AS account
		WHERE session.token_hash = $1
		  AND session.account_id = account.account_id
		  AND session.revoked_at IS NULL
		  AND session.idle_expires_at > $2
		  AND session.absolute_expires_at > $2
		  AND account.status = 'ACTIVE'
		RETURNING account.account_id::text, account.username, account.nickname
	`, tokenHash(sessionToken), now, now.Add(defaultSessionIdleTTL)).Scan(
		&account.AccountID,
		&account.Username,
		&account.Nickname,
	)
	if err != nil {
		if err == pgx.ErrNoRows {
			return AuthenticatedAccount{}, ErrUnauthenticated
		}
		return AuthenticatedAccount{}, status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	return account, nil
}

func (p *PostgresIdentity) Logout(ctx context.Context, sessionToken string) error {
	if strings.TrimSpace(sessionToken) == "" {
		return nil
	}
	_, err := p.pool.Exec(ctx, `
		UPDATE login_session
		SET revoked_at = COALESCE(revoked_at, NOW())
		WHERE token_hash = $1
	`, tokenHash(sessionToken))
	if err != nil {
		return status.Error(codes.Internal, "SESSION_REVOKE_FAILED")
	}
	return nil
}

func (p *PostgresIdentity) GetAccount(
	ctx context.Context,
	accountID string,
) (AuthenticatedAccount, error) {
	var account AuthenticatedAccount
	err := p.pool.QueryRow(ctx, `
		SELECT account_id::text, username, nickname
		FROM user_account
		WHERE account_id = $1::uuid
		  AND status = 'ACTIVE'
	`, accountID).Scan(
		&account.AccountID,
		&account.Username,
		&account.Nickname,
	)
	if err != nil {
		if err == pgx.ErrNoRows {
			return AuthenticatedAccount{}, ErrUnauthenticated
		}
		return AuthenticatedAccount{}, status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	return account, nil
}

func (p *PostgresIdentity) UpdateNickname(
	ctx context.Context,
	accountID string,
	nickname string,
) (AuthenticatedAccount, error) {
	nickname = norm.NFC.String(strings.TrimSpace(nickname))
	if nickname == "" || utf8.RuneCountInString(nickname) > 20 {
		return AuthenticatedAccount{}, status.Error(codes.InvalidArgument, "NICKNAME_INVALID")
	}
	var account AuthenticatedAccount
	err := p.pool.QueryRow(ctx, `
		UPDATE user_account
		SET nickname = $2, updated_at = NOW()
		WHERE account_id = $1::uuid
		  AND status = 'ACTIVE'
		RETURNING account_id::text, username, nickname
	`, accountID, nickname).Scan(
		&account.AccountID,
		&account.Username,
		&account.Nickname,
	)
	if err != nil {
		if err == pgx.ErrNoRows {
			return AuthenticatedAccount{}, ErrUnauthenticated
		}
		return AuthenticatedAccount{}, status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	return account, nil
}

func (p *PostgresIdentity) ChangePassword(
	ctx context.Context,
	input ChangePasswordInput,
) error {
	if length := utf8.RuneCountInString(input.NewPassword); length < 12 || length > 128 {
		return status.Error(codes.InvalidArgument, "PASSWORD_POLICY_FAILED")
	}
	tx, err := p.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var passwordHash string
	if err := tx.QueryRow(ctx, `
		SELECT password_hash
		FROM user_account
		WHERE account_id = $1::uuid
		  AND status = 'ACTIVE'
		FOR UPDATE
	`, input.AccountID).Scan(&passwordHash); err != nil {
		if err == pgx.ErrNoRows {
			return ErrUnauthenticated
		}
		return status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	if !verifyPassword(passwordHash, input.CurrentPassword) {
		return status.Error(codes.Unauthenticated, "CURRENT_PASSWORD_INVALID")
	}
	newHash, err := hashPassword(input.NewPassword)
	if err != nil {
		return status.Error(codes.Internal, "PASSWORD_HASH_FAILED")
	}
	if _, err := tx.Exec(ctx, `
		UPDATE user_account
		SET password_hash = $2, updated_at = NOW()
		WHERE account_id = $1::uuid
	`, input.AccountID, newHash); err != nil {
		return status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	if _, err := tx.Exec(ctx, `
		UPDATE login_session
		SET revoked_at = COALESCE(revoked_at, NOW())
		WHERE account_id = $1::uuid
		  AND token_hash <> $2
	`, input.AccountID, tokenHash(input.CurrentSessionToken)); err != nil {
		return status.Error(codes.Internal, "SESSION_REVOKE_FAILED")
	}
	if err := tx.Commit(ctx); err != nil {
		return status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	return nil
}

func (p *PostgresIdentity) ResetPassword(
	ctx context.Context,
	input ResetPasswordInput,
) error {
	if length := utf8.RuneCountInString(input.NewPassword); length < 12 || length > 128 {
		return status.Error(codes.InvalidArgument, "PASSWORD_POLICY_FAILED")
	}
	tx, err := p.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var accountID string
	var recoveryCodeID string
	if err := tx.QueryRow(ctx, `
		SELECT account.account_id::text, code.recovery_code_id::text
		FROM user_account AS account
		JOIN recovery_code AS code ON code.account_id = account.account_id
		WHERE account.username_normalized = $1
		  AND account.status = 'ACTIVE'
		  AND code.code_hash = $2
		  AND code.consumed_at IS NULL
		FOR UPDATE OF code, account
	`, normalizeUsername(input.Username), tokenHash(input.RecoveryCode)).Scan(
		&accountID,
		&recoveryCodeID,
	); err != nil {
		if err == pgx.ErrNoRows {
			return status.Error(codes.Unauthenticated, "RECOVERY_CODE_INVALID")
		}
		return status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	newHash, err := hashPassword(input.NewPassword)
	if err != nil {
		return status.Error(codes.Internal, "PASSWORD_HASH_FAILED")
	}
	if _, err := tx.Exec(ctx, `
		UPDATE user_account
		SET password_hash = $2, updated_at = NOW()
		WHERE account_id = $1::uuid
	`, accountID, newHash); err != nil {
		return status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	if _, err := tx.Exec(ctx, `
		UPDATE recovery_code
		SET consumed_at = NOW()
		WHERE recovery_code_id = $1::uuid
		  AND consumed_at IS NULL
	`, recoveryCodeID); err != nil {
		return status.Error(codes.Internal, "RECOVERY_CODE_STORE_FAILED")
	}
	if _, err := tx.Exec(ctx, `
		UPDATE login_session
		SET revoked_at = COALESCE(revoked_at, NOW())
		WHERE account_id = $1::uuid
	`, accountID); err != nil {
		return status.Error(codes.Internal, "SESSION_REVOKE_FAILED")
	}
	if err := tx.Commit(ctx); err != nil {
		return status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	return nil
}

func (p *PostgresIdentity) RegenerateRecoveryCodes(
	ctx context.Context,
	accountID string,
	currentPassword string,
) ([]string, error) {
	recoveryCodes, hashes, err := newRecoveryCodes(10)
	if err != nil {
		return nil, status.Error(codes.Internal, "RECOVERY_CODE_FAILED")
	}
	tx, err := p.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return nil, status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var passwordHash string
	if err := tx.QueryRow(ctx, `
		SELECT password_hash
		FROM user_account
		WHERE account_id = $1::uuid
		  AND status = 'ACTIVE'
		FOR UPDATE
	`, accountID).Scan(&passwordHash); err != nil {
		if err == pgx.ErrNoRows {
			return nil, ErrUnauthenticated
		}
		return nil, status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	if !verifyPassword(passwordHash, currentPassword) {
		return nil, status.Error(codes.Unauthenticated, "CURRENT_PASSWORD_INVALID")
	}
	if _, err := tx.Exec(ctx, `
		DELETE FROM recovery_code WHERE account_id = $1::uuid
	`, accountID); err != nil {
		return nil, status.Error(codes.Internal, "RECOVERY_CODE_STORE_FAILED")
	}
	for _, codeHash := range hashes {
		if _, err := tx.Exec(ctx, `
			INSERT INTO recovery_code (recovery_code_id, account_id, code_hash)
			VALUES ($1, $2::uuid, $3)
		`, uuid.New(), accountID, codeHash); err != nil {
			return nil, status.Error(codes.Internal, "RECOVERY_CODE_STORE_FAILED")
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	return recoveryCodes, nil
}

func (p *PostgresIdentity) VerifyAccountPassword(
	ctx context.Context,
	accountID string,
	currentPassword string,
) error {
	var passwordHash string
	if err := p.pool.QueryRow(ctx, `
		SELECT password_hash
		FROM user_account
		WHERE account_id = $1::uuid
		  AND status = 'ACTIVE'
	`, accountID).Scan(&passwordHash); err != nil {
		if err == pgx.ErrNoRows {
			return ErrUnauthenticated
		}
		return status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	if !verifyPassword(passwordHash, currentPassword) {
		return status.Error(codes.Unauthenticated, "CURRENT_PASSWORD_INVALID")
	}
	return nil
}

func (p *PostgresIdentity) CompleteAccountDeletion(
	ctx context.Context,
	accountID string,
) error {
	tx, err := p.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var normalized string
	if err := tx.QueryRow(ctx, `
		SELECT username_normalized
		FROM user_account
		WHERE account_id = $1::uuid
		FOR UPDATE
	`, accountID).Scan(&normalized); err != nil {
		if err == pgx.ErrNoRows {
			return ErrUnauthenticated
		}
		return status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO reserved_username (username_hash)
		VALUES ($1)
		ON CONFLICT (username_hash) DO NOTHING
	`, tokenHash(normalized)); err != nil {
		return status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	if _, err := tx.Exec(ctx, `
		UPDATE user_account
		SET username = '已删除',
		    username_normalized = $2,
		    nickname = '已删除',
		    password_hash = '!deleted',
		    status = 'DELETED',
		    guardian_ack_recorded = FALSE,
		    updated_at = NOW()
		WHERE account_id = $1::uuid
	`, accountID, "deleted:"+accountID); err != nil {
		return status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	if _, err := tx.Exec(ctx, `
		DELETE FROM account_consent WHERE account_id = $1::uuid
	`, accountID); err != nil {
		return status.Error(codes.Internal, "CONSENT_STORE_UNAVAILABLE")
	}
	if _, err := tx.Exec(ctx, `
		DELETE FROM recovery_code WHERE account_id = $1::uuid
	`, accountID); err != nil {
		return status.Error(codes.Internal, "RECOVERY_CODE_STORE_FAILED")
	}
	if _, err := tx.Exec(ctx, `
		UPDATE login_session
		SET revoked_at = COALESCE(revoked_at, NOW())
		WHERE account_id = $1::uuid
	`, accountID); err != nil {
		return status.Error(codes.Internal, "SESSION_REVOKE_FAILED")
	}
	if err := tx.Commit(ctx); err != nil {
		return status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	return nil
}

func (p *PostgresIdentity) ListDormantAccountIDs(
	ctx context.Context,
	cutoff time.Time,
) ([]string, error) {
	rows, err := p.pool.Query(ctx, `
		SELECT account_id::text
		FROM user_account
		WHERE status = 'ACTIVE'
		  AND COALESCE(last_login_at, created_at) < $1
		ORDER BY COALESCE(last_login_at, created_at), account_id
	`, cutoff.UTC())
	if err != nil {
		return nil, status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	defer rows.Close()
	accountIDs := make([]string, 0)
	for rows.Next() {
		var accountID string
		if err := rows.Scan(&accountID); err != nil {
			return nil, status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
		}
		accountIDs = append(accountIDs, accountID)
	}
	if rows.Err() != nil {
		return nil, status.Error(codes.Internal, "ACCOUNT_STORE_UNAVAILABLE")
	}
	return accountIDs, nil
}

func validateIdentityFields(
	username string,
	nickname string,
	password string,
) (string, string, string, error) {
	username = norm.NFC.String(strings.TrimSpace(username))
	count := utf8.RuneCountInString(username)
	if count < 4 || count > 32 {
		return "", "", "", status.Error(codes.InvalidArgument, "USERNAME_INVALID")
	}
	for _, char := range username {
		if !unicode.IsLetter(char) && !unicode.IsNumber(char) && char != '_' && char != '-' {
			return "", "", "", status.Error(codes.InvalidArgument, "USERNAME_INVALID")
		}
	}
	nickname = norm.NFC.String(strings.TrimSpace(nickname))
	if nickname == "" {
		nickname = username
	}
	if utf8.RuneCountInString(nickname) > 20 {
		return "", "", "", status.Error(codes.InvalidArgument, "NICKNAME_INVALID")
	}
	passwordLength := utf8.RuneCountInString(password)
	if passwordLength < 12 || passwordLength > 128 {
		return "", "", "", status.Error(codes.InvalidArgument, "PASSWORD_POLICY_FAILED")
	}
	return username, normalizeUsername(username), nickname, nil
}

func normalizeUsername(username string) string {
	normalized := norm.NFC.String(strings.TrimSpace(username))
	var builder strings.Builder
	for _, char := range normalized {
		if char <= unicode.MaxASCII {
			builder.WriteRune(unicode.ToLower(char))
		} else {
			builder.WriteRune(char)
		}
	}
	return builder.String()
}

func hashPassword(password string) (string, error) {
	salt := make([]byte, argonSaltBytes)
	if _, err := rand.Read(salt); err != nil {
		return "", err
	}
	key := argon2.IDKey(
		[]byte(password),
		salt,
		argonIterations,
		argonMemory,
		argonParallelism,
		argonKeyBytes,
	)
	return fmt.Sprintf(
		"$argon2id$v=19$m=%d,t=%d,p=%d$%s$%s",
		argonMemory,
		argonIterations,
		argonParallelism,
		base64.RawStdEncoding.EncodeToString(salt),
		base64.RawStdEncoding.EncodeToString(key),
	), nil
}

func verifyPassword(encoded string, password string) bool {
	parts := strings.Split(encoded, "$")
	if len(parts) != 6 || parts[1] != "argon2id" {
		return false
	}
	var memory uint32
	var iterations uint32
	var parallelism uint8
	if _, err := fmt.Sscanf(
		parts[3],
		"m=%d,t=%d,p=%d",
		&memory,
		&iterations,
		&parallelism,
	); err != nil {
		return false
	}
	salt, err := base64.RawStdEncoding.DecodeString(parts[4])
	if err != nil {
		return false
	}
	expected, err := base64.RawStdEncoding.DecodeString(parts[5])
	if err != nil {
		return false
	}
	actual := argon2.IDKey(
		[]byte(password),
		salt,
		iterations,
		memory,
		parallelism,
		uint32(len(expected)),
	)
	return subtle.ConstantTimeCompare(actual, expected) == 1
}

func newRecoveryCodes(count int) ([]string, []string, error) {
	encoder := base32.StdEncoding.WithPadding(base32.NoPadding)
	codes := make([]string, 0, count)
	hashes := make([]string, 0, count)
	for range count {
		raw := make([]byte, 10)
		if _, err := rand.Read(raw); err != nil {
			return nil, nil, err
		}
		value := encoder.EncodeToString(raw)
		code := value[:5] + "-" + value[5:10]
		codes = append(codes, code)
		hashes = append(hashes, tokenHash(code))
	}
	return codes, hashes, nil
}

func newSessionToken() (string, string, error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", "", err
	}
	token := base64.RawURLEncoding.EncodeToString(raw)
	return token, tokenHash(token), nil
}

func tokenHash(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

func isUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	return err != nil && errors.As(err, &pgErr) && pgErr.Code == "23505"
}
