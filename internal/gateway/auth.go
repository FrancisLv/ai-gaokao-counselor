package gateway

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"errors"
	"net"
	"net/http"
	"strings"
	"time"

	"github.com/go-kratos/kratos/v2/transport"
	khttp "github.com/go-kratos/kratos/v2/transport/http"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	gatewayv1 "github.com/choucisan/ai-gaokao-counselor/api/gateway/v1"
	orchestratorv1 "github.com/choucisan/ai-gaokao-counselor/api/orchestrator/v1"
)

const (
	sessionCookieName = "counselor_session"
	csrfCookieName    = "counselor_csrf"

	defaultSessionIdleTTL     = 30 * 24 * time.Hour
	defaultSessionAbsoluteTTL = 90 * 24 * time.Hour
)

var ErrUnauthenticated = errors.New("UNAUTHENTICATED")

type RegisterInput struct {
	InviteCode           string
	Username             string
	Password             string
	Nickname             string
	PrivacyPolicyVersion string
	GuardianAckRecorded  bool
	ClientIP             string
}

type AuthenticatedAccount struct {
	AccountID string
	Username  string
	Nickname  string
}

type RegisterOutput struct {
	Account       AuthenticatedAccount
	RecoveryCodes []string
	SessionToken  string
	ExpiresAt     time.Time
}

type LoginInput struct {
	Username string
	Password string
	ClientIP string
}

type LoginOutput struct {
	Account      AuthenticatedAccount
	SessionToken string
	ExpiresAt    time.Time
}

type ChangePasswordInput struct {
	AccountID           string
	CurrentSessionToken string
	CurrentPassword     string
	NewPassword         string
}

type ResetPasswordInput struct {
	Username     string
	RecoveryCode string
	NewPassword  string
}

type identityService interface {
	Register(context.Context, RegisterInput) (RegisterOutput, error)
	Login(context.Context, LoginInput) (LoginOutput, error)
	ResolveSession(context.Context, string) (AuthenticatedAccount, error)
	Logout(context.Context, string) error
	GetAccount(context.Context, string) (AuthenticatedAccount, error)
	UpdateNickname(context.Context, string, string) (AuthenticatedAccount, error)
	ChangePassword(context.Context, ChangePasswordInput) error
	ResetPassword(context.Context, ResetPasswordInput) error
	RegenerateRecoveryCodes(context.Context, string, string) ([]string, error)
	VerifyAccountPassword(context.Context, string, string) error
	CompleteAccountDeletion(context.Context, string) error
	ListDormantAccountIDs(context.Context, time.Time) ([]string, error)
}

type AuthOptions struct {
	CookieSecure       bool
	SessionIdleTTL     time.Duration
	SessionAbsoluteTTL time.Duration
	Now                func() time.Time
	Random             func([]byte) error
}

func (s *Service) Register(
	ctx context.Context,
	req *gatewayv1.RegisterRequest,
) (*gatewayv1.RegisterReply, error) {
	if s.identity == nil {
		return nil, status.Error(codes.Unavailable, "ACCOUNT_SERVICE_UNAVAILABLE")
	}
	if strings.TrimSpace(req.GetInviteCode()) == "" ||
		strings.TrimSpace(req.GetUsername()) == "" ||
		req.GetPassword() == "" ||
		strings.TrimSpace(req.GetPrivacyPolicyVersion()) == "" {
		return nil, status.Error(codes.InvalidArgument, "REGISTRATION_FIELDS_REQUIRED")
	}
	if !req.GetGuardianAckRecorded() {
		return nil, status.Error(codes.InvalidArgument, "GUARDIAN_ACK_REQUIRED")
	}
	clientIP := requestClientIP(ctx)
	result, err := s.identity.Register(ctx, RegisterInput{
		InviteCode:           strings.TrimSpace(req.GetInviteCode()),
		Username:             strings.TrimSpace(req.GetUsername()),
		Password:             req.GetPassword(),
		Nickname:             strings.TrimSpace(req.GetNickname()),
		PrivacyPolicyVersion: strings.TrimSpace(req.GetPrivacyPolicyVersion()),
		GuardianAckRecorded:  true,
		ClientIP:             clientIP,
	})
	if err != nil {
		return nil, err
	}
	if err := setAuthenticatedCookies(ctx, s.authOptions, result.SessionToken, result.ExpiresAt); err != nil {
		return nil, err
	}
	return &gatewayv1.RegisterReply{
		Account: &gatewayv1.AccountView{
			AccountId: result.Account.AccountID,
			Username:  result.Account.Username,
			Nickname:  result.Account.Nickname,
		},
		RecoveryCodes: result.RecoveryCodes,
	}, nil
}

func (s *Service) Login(
	ctx context.Context,
	req *gatewayv1.LoginRequest,
) (*gatewayv1.LoginReply, error) {
	if s.identity == nil {
		return nil, status.Error(codes.Unavailable, "ACCOUNT_SERVICE_UNAVAILABLE")
	}
	if strings.TrimSpace(req.GetUsername()) == "" || req.GetPassword() == "" {
		return nil, status.Error(codes.InvalidArgument, "LOGIN_FIELDS_REQUIRED")
	}
	clientIP := requestClientIP(ctx)
	result, err := s.identity.Login(ctx, LoginInput{
		Username: strings.TrimSpace(req.GetUsername()),
		Password: req.GetPassword(),
		ClientIP: clientIP,
	})
	if errors.Is(err, ErrUnauthenticated) {
		return nil, status.Error(codes.Unauthenticated, "LOGIN_INVALID")
	}
	if err != nil {
		return nil, err
	}
	if err := setAuthenticatedCookies(
		ctx,
		s.authOptions,
		result.SessionToken,
		result.ExpiresAt,
	); err != nil {
		return nil, err
	}
	return &gatewayv1.LoginReply{
		Account: &gatewayv1.AccountView{
			AccountId: result.Account.AccountID,
			Username:  result.Account.Username,
			Nickname:  result.Account.Nickname,
		},
	}, nil
}

func (s *Service) ResetPassword(
	ctx context.Context,
	req *gatewayv1.ResetPasswordRequest,
) (*gatewayv1.ResetPasswordReply, error) {
	if s.identity == nil {
		return nil, status.Error(codes.Unavailable, "ACCOUNT_SERVICE_UNAVAILABLE")
	}
	if strings.TrimSpace(req.GetUsername()) == "" ||
		strings.TrimSpace(req.GetRecoveryCode()) == "" ||
		req.GetNewPassword() == "" {
		return nil, status.Error(codes.InvalidArgument, "RESET_FIELDS_REQUIRED")
	}
	if err := s.identity.ResetPassword(ctx, ResetPasswordInput{
		Username:     strings.TrimSpace(req.GetUsername()),
		RecoveryCode: strings.TrimSpace(req.GetRecoveryCode()),
		NewPassword:  req.GetNewPassword(),
	}); err != nil {
		return nil, err
	}
	return &gatewayv1.ResetPasswordReply{Reset_: true}, nil
}

func (s *Service) Logout(
	ctx context.Context,
	_ *gatewayv1.LogoutRequest,
) (*gatewayv1.LogoutReply, error) {
	if s.identity == nil {
		return nil, status.Error(codes.Unavailable, "ACCOUNT_SERVICE_UNAVAILABLE")
	}
	if err := validateCSRF(ctx); err != nil {
		return nil, err
	}
	token, err := sessionTokenFromRequest(ctx)
	if err != nil {
		return nil, err
	}
	if err := s.identity.Logout(ctx, token); err != nil {
		return nil, err
	}
	clearAuthenticatedCookies(ctx, s.authOptions)
	return &gatewayv1.LogoutReply{LoggedOut: true}, nil
}

func (s *Service) GetAccount(
	ctx context.Context,
	_ *gatewayv1.GetAccountRequest,
) (*gatewayv1.AccountReply, error) {
	principal, err := s.requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	account, err := s.identity.GetAccount(ctx, principal)
	if errors.Is(err, ErrUnauthenticated) {
		return nil, status.Error(codes.Unauthenticated, "PRINCIPAL_REQUIRED")
	}
	if err != nil {
		return nil, err
	}
	return &gatewayv1.AccountReply{Account: accountView(account)}, nil
}

func (s *Service) UpdateAccount(
	ctx context.Context,
	req *gatewayv1.UpdateAccountRequest,
) (*gatewayv1.AccountReply, error) {
	principal, err := s.requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	if err := validateCSRF(ctx); err != nil {
		return nil, err
	}
	account, err := s.identity.UpdateNickname(
		ctx,
		principal,
		strings.TrimSpace(req.GetNickname()),
	)
	if err != nil {
		return nil, err
	}
	return &gatewayv1.AccountReply{Account: accountView(account)}, nil
}

func (s *Service) ChangePassword(
	ctx context.Context,
	req *gatewayv1.ChangePasswordRequest,
) (*gatewayv1.ChangePasswordReply, error) {
	principal, err := s.requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	if err := validateCSRF(ctx); err != nil {
		return nil, err
	}
	currentToken, err := sessionTokenFromRequest(ctx)
	if err != nil {
		return nil, err
	}
	if req.GetCurrentPassword() == "" || req.GetNewPassword() == "" {
		return nil, status.Error(codes.InvalidArgument, "PASSWORD_FIELDS_REQUIRED")
	}
	if err := s.identity.ChangePassword(ctx, ChangePasswordInput{
		AccountID:           principal,
		CurrentSessionToken: currentToken,
		CurrentPassword:     req.GetCurrentPassword(),
		NewPassword:         req.GetNewPassword(),
	}); err != nil {
		return nil, err
	}
	return &gatewayv1.ChangePasswordReply{Changed: true}, nil
}

func (s *Service) RegenerateRecoveryCodes(
	ctx context.Context,
	req *gatewayv1.RegenerateRecoveryCodesRequest,
) (*gatewayv1.RecoveryCodesReply, error) {
	principal, err := s.requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	if err := validateCSRF(ctx); err != nil {
		return nil, err
	}
	if req.GetCurrentPassword() == "" {
		return nil, status.Error(codes.InvalidArgument, "CURRENT_PASSWORD_REQUIRED")
	}
	codes, err := s.identity.RegenerateRecoveryCodes(
		ctx,
		principal,
		req.GetCurrentPassword(),
	)
	if err != nil {
		return nil, err
	}
	return &gatewayv1.RecoveryCodesReply{RecoveryCodes: codes}, nil
}

func (s *Service) DeleteAccount(
	ctx context.Context,
	req *gatewayv1.DeleteAccountRequest,
) (*gatewayv1.DeleteAccountReply, error) {
	principal, err := s.requirePrincipal(ctx)
	if err != nil {
		return nil, err
	}
	if err := validateCSRF(ctx); err != nil {
		return nil, err
	}
	if req.GetCurrentPassword() == "" ||
		req.GetConfirmation() != "删除账号" {
		return nil, status.Error(codes.InvalidArgument, "DELETION_CONFIRMATION_REQUIRED")
	}
	if err := s.identity.VerifyAccountPassword(
		ctx,
		principal,
		req.GetCurrentPassword(),
	); err != nil {
		return nil, err
	}
	requestID := uuid.NewString()
	deletion, err := s.client.DeletePrincipalData(
		outgoingPrincipal(ctx, principal),
		&orchestratorv1.DeletePrincipalDataRequest{
			RequestId: requestID,
		},
	)
	if err != nil {
		return nil, err
	}
	if !deletion.GetAccepted() ||
		deletion.GetStatus() != "DELETION_COMPLETED" {
		return nil, status.Error(
			codes.FailedPrecondition,
			firstNonEmpty(deletion.GetReason(), "DELETION_FAILED_RETRYABLE"),
		)
	}
	if err := s.identity.CompleteAccountDeletion(ctx, principal); err != nil {
		return nil, err
	}
	clearAuthenticatedCookies(ctx, s.authOptions)
	return &gatewayv1.DeleteAccountReply{Deleted: true}, nil
}

func accountView(account AuthenticatedAccount) *gatewayv1.AccountView {
	return &gatewayv1.AccountView{
		AccountId: account.AccountID,
		Username:  account.Username,
		Nickname:  account.Nickname,
	}
}

func requestClientIP(ctx context.Context) string {
	request, ok := khttp.RequestFromServerContext(ctx)
	if !ok {
		return ""
	}
	host, _, err := net.SplitHostPort(request.RemoteAddr)
	if err == nil {
		return host
	}
	return request.RemoteAddr
}

func sessionTokenFromRequest(ctx context.Context) (string, error) {
	request, ok := khttp.RequestFromServerContext(ctx)
	if !ok {
		return "", status.Error(codes.Unauthenticated, "PRINCIPAL_REQUIRED")
	}
	cookie, err := request.Cookie(sessionCookieName)
	if err != nil || strings.TrimSpace(cookie.Value) == "" {
		return "", status.Error(codes.Unauthenticated, "PRINCIPAL_REQUIRED")
	}
	return cookie.Value, nil
}

func validateCSRF(ctx context.Context) error {
	request, ok := khttp.RequestFromServerContext(ctx)
	if !ok {
		return nil
	}
	cookie, err := request.Cookie(csrfCookieName)
	if err != nil {
		return status.Error(codes.PermissionDenied, "CSRF_TOKEN_INVALID")
	}
	header := strings.TrimSpace(request.Header.Get("X-CSRF-Token"))
	if header == "" ||
		subtle.ConstantTimeCompare([]byte(cookie.Value), []byte(header)) != 1 {
		return status.Error(codes.PermissionDenied, "CSRF_TOKEN_INVALID")
	}
	return nil
}

func setAuthenticatedCookies(
	ctx context.Context,
	options AuthOptions,
	sessionToken string,
	expiresAt time.Time,
) error {
	tr, ok := transport.FromServerContext(ctx)
	if !ok {
		return status.Error(codes.Internal, "HTTP_TRANSPORT_REQUIRED")
	}
	csrfToken, err := randomToken(options.Random)
	if err != nil {
		return status.Error(codes.Internal, "CSRF_TOKEN_GENERATION_FAILED")
	}
	maxAge := int(time.Until(expiresAt).Seconds())
	if maxAge < 1 {
		maxAge = 1
	}
	sessionCookie := &http.Cookie{
		Name:     sessionCookieName,
		Value:    sessionToken,
		Path:     "/",
		MaxAge:   maxAge,
		Expires:  expiresAt,
		HttpOnly: true,
		Secure:   options.CookieSecure,
		SameSite: http.SameSiteLaxMode,
	}
	csrfCookie := &http.Cookie{
		Name:     csrfCookieName,
		Value:    csrfToken,
		Path:     "/",
		MaxAge:   maxAge,
		Expires:  expiresAt,
		HttpOnly: false,
		Secure:   options.CookieSecure,
		SameSite: http.SameSiteLaxMode,
	}
	tr.ReplyHeader().Add("Set-Cookie", sessionCookie.String())
	tr.ReplyHeader().Add("Set-Cookie", csrfCookie.String())
	return nil
}

func clearAuthenticatedCookies(ctx context.Context, options AuthOptions) {
	tr, ok := transport.FromServerContext(ctx)
	if !ok {
		return
	}
	for _, name := range []string{sessionCookieName, csrfCookieName} {
		cookie := (&http.Cookie{
			Name:     name,
			Value:    "",
			Path:     "/",
			MaxAge:   -1,
			Expires:  time.Unix(1, 0),
			HttpOnly: name == sessionCookieName,
			Secure:   options.CookieSecure,
			SameSite: http.SameSiteLaxMode,
		}).String()
		tr.ReplyHeader().Add("Set-Cookie", cookie)
	}
}

func randomToken(random func([]byte) error) (string, error) {
	raw := make([]byte, 32)
	if random == nil {
		if _, err := rand.Read(raw); err != nil {
			return "", err
		}
	} else if err := random(raw); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(raw), nil
}
