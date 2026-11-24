//go:build integration

package gateway

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestPostgresIdentityRegistersAndResolvesSession(t *testing.T) {
	dsn := strings.TrimSpace(os.Getenv("GATEWAY_TEST_DSN"))
	if dsn == "" {
		t.Skip("GATEWAY_TEST_DSN 未设置")
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)

	if _, err := pool.Exec(ctx, `
		DROP TABLE IF EXISTS login_session, recovery_code, invite_code,
			account_consent, reserved_username, user_account CASCADE
	`); err != nil {
		t.Fatal(err)
	}
	migration, err := os.ReadFile(filepath.Join(
		"..", "..", "migrations", "49_account_authentication.sql",
	))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, string(migration)); err != nil {
		t.Fatal(err)
	}

	inviteHash := sha256.Sum256([]byte("INVITE-1"))
	if _, err := pool.Exec(ctx, `
		INSERT INTO invite_code (code_hash, expires_at)
		VALUES ($1, NOW() + INTERVAL '1 day')
	`, hex.EncodeToString(inviteHash[:])); err != nil {
		t.Fatal(err)
	}

	identity, err := NewPostgresIdentity(pool)
	if err != nil {
		t.Fatal(err)
	}
	registered, err := identity.Register(ctx, RegisterInput{
		InviteCode:           "INVITE-1",
		Username:             "Candidate",
		Password:             "correct horse battery staple",
		Nickname:             "湖南考生",
		PrivacyPolicyVersion: "privacy-v1",
		GuardianAckRecorded:  true,
		ClientIP:             "127.0.0.1",
	})
	if err != nil {
		t.Fatal(err)
	}
	if registered.Account.Username != "Candidate" ||
		registered.Account.Nickname != "湖南考生" ||
		len(registered.RecoveryCodes) != 10 {
		t.Fatalf("registration = %#v", registered)
	}

	resolved, err := identity.ResolveSession(ctx, registered.SessionToken)
	if err != nil {
		t.Fatal(err)
	}
	if resolved.AccountID != registered.Account.AccountID {
		t.Fatalf("principal = %q", resolved.AccountID)
	}

	for range 5 {
		if _, err := identity.Login(ctx, LoginInput{
			Username: "candidate",
			Password: "wrong password value",
			ClientIP: "127.0.0.1",
		}); err == nil {
			t.Fatal("wrong password was accepted")
		}
	}
	if _, err := identity.Login(ctx, LoginInput{
		Username: "candidate",
		Password: "correct horse battery staple",
		ClientIP: "127.0.0.1",
	}); status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("rate limit error = %v", err)
	}
	identity, err = NewPostgresIdentity(pool)
	if err != nil {
		t.Fatal(err)
	}
	loggedIn, err := identity.Login(ctx, LoginInput{
		Username: "candidate",
		Password: "correct horse battery staple",
		ClientIP: "127.0.0.1",
	})
	if err != nil {
		t.Fatal(err)
	}
	if loggedIn.SessionToken == "" ||
		!loggedIn.ExpiresAt.After(time.Now().Add(29*24*time.Hour)) {
		t.Fatalf("login = %#v", loggedIn)
	}

	_, err = identity.Register(ctx, RegisterInput{
		InviteCode:           "INVITE-1",
		Username:             "another",
		Password:             "correct horse battery staple",
		PrivacyPolicyVersion: "privacy-v1",
		GuardianAckRecorded:  true,
	})
	if err == nil {
		t.Fatal("consumed invite was accepted")
	}

	if err := identity.ChangePassword(ctx, ChangePasswordInput{
		AccountID:           registered.Account.AccountID,
		CurrentSessionToken: loggedIn.SessionToken,
		CurrentPassword:     "correct horse battery staple",
		NewPassword:         "new correct horse battery staple",
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := identity.Login(ctx, LoginInput{
		Username: "candidate",
		Password: "correct horse battery staple",
	}); err == nil {
		t.Fatal("old password still works")
	}
	afterChange, err := identity.Login(ctx, LoginInput{
		Username: "candidate",
		Password: "new correct horse battery staple",
	})
	if err != nil {
		t.Fatal(err)
	}
	regenerated, err := identity.RegenerateRecoveryCodes(
		ctx,
		registered.Account.AccountID,
		"new correct horse battery staple",
	)
	if err != nil || len(regenerated) != 10 {
		t.Fatalf("regenerated = %#v, err = %v", regenerated, err)
	}
	if err := identity.ResetPassword(ctx, ResetPasswordInput{
		Username:     "candidate",
		RecoveryCode: regenerated[0],
		NewPassword:  "reset password value",
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := identity.ResolveSession(ctx, afterChange.SessionToken); err == nil {
		t.Fatal("password reset did not revoke sessions")
	}
	if err := identity.CompleteAccountDeletion(
		ctx,
		registered.Account.AccountID,
	); err != nil {
		t.Fatal(err)
	}
	if _, err := identity.GetAccount(ctx, registered.Account.AccountID); err == nil {
		t.Fatal("deleted account remained readable")
	}
}
