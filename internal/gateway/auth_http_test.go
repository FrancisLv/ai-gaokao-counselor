package gateway

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	khttp "github.com/go-kratos/kratos/v2/transport/http"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	orchestratorv1 "github.com/choucisan/ai-gaokao-counselor/api/orchestrator/v1"
)

type fakeIdentity struct {
	registered          RegisterInput
	output              RegisterOutput
	err                 error
	resolved            AuthenticatedAccount
	token               string
	loggedIn            LoginInput
	loginOut            LoginOutput
	logout              string
	account             AuthenticatedAccount
	nickname            string
	changed             ChangePasswordInput
	reset               ResetPasswordInput
	regeneratedCodes    []string
	regeneratedPassword string
	deletePassword      string
	completedDeletion   string
	dormant             []string
}

type testIdentity struct{}

func (testIdentity) Register(
	context.Context,
	RegisterInput,
) (RegisterOutput, error) {
	return RegisterOutput{}, status.Error(codes.Unavailable, "REGISTER_NOT_CONFIGURED")
}

func (testIdentity) Login(
	context.Context,
	LoginInput,
) (LoginOutput, error) {
	return LoginOutput{}, status.Error(codes.Unavailable, "LOGIN_NOT_CONFIGURED")
}

func (testIdentity) ResolveSession(
	_ context.Context,
	token string,
) (AuthenticatedAccount, error) {
	if !strings.HasPrefix(token, "test:") {
		return AuthenticatedAccount{}, ErrUnauthenticated
	}
	return AuthenticatedAccount{
		AccountID: strings.TrimPrefix(token, "test:"),
	}, nil
}

func (testIdentity) Logout(context.Context, string) error {
	return nil
}

func (testIdentity) GetAccount(
	context.Context,
	string,
) (AuthenticatedAccount, error) {
	return AuthenticatedAccount{}, ErrUnauthenticated
}

func (testIdentity) UpdateNickname(
	context.Context,
	string,
	string,
) (AuthenticatedAccount, error) {
	return AuthenticatedAccount{}, ErrUnauthenticated
}

func (testIdentity) ChangePassword(
	context.Context,
	ChangePasswordInput,
) error {
	return ErrUnauthenticated
}

func (testIdentity) ResetPassword(
	context.Context,
	ResetPasswordInput,
) error {
	return ErrUnauthenticated
}

func (testIdentity) RegenerateRecoveryCodes(
	context.Context,
	string,
	string,
) ([]string, error) {
	return nil, ErrUnauthenticated
}

func (testIdentity) VerifyAccountPassword(
	context.Context,
	string,
	string,
) error {
	return ErrUnauthenticated
}

func (testIdentity) CompleteAccountDeletion(context.Context, string) error {
	return ErrUnauthenticated
}

func (testIdentity) ListDormantAccountIDs(
	context.Context,
	time.Time,
) ([]string, error) {
	return nil, nil
}

func (f *fakeIdentity) Register(
	_ context.Context,
	input RegisterInput,
) (RegisterOutput, error) {
	f.registered = input
	return f.output, f.err
}

func (f *fakeIdentity) ResolveSession(
	_ context.Context,
	token string,
) (AuthenticatedAccount, error) {
	f.token = token
	if f.resolved.AccountID == "" {
		return AuthenticatedAccount{}, ErrUnauthenticated
	}
	return f.resolved, nil
}

func (f *fakeIdentity) Login(
	_ context.Context,
	input LoginInput,
) (LoginOutput, error) {
	f.loggedIn = input
	if f.loginOut.SessionToken == "" {
		return LoginOutput{}, ErrUnauthenticated
	}
	return f.loginOut, nil
}

func (f *fakeIdentity) Logout(_ context.Context, token string) error {
	f.logout = token
	return nil
}

func (f *fakeIdentity) GetAccount(
	_ context.Context,
	accountID string,
) (AuthenticatedAccount, error) {
	if f.account.AccountID == "" || f.account.AccountID != accountID {
		return AuthenticatedAccount{}, ErrUnauthenticated
	}
	return f.account, nil
}

func (f *fakeIdentity) UpdateNickname(
	_ context.Context,
	accountID string,
	nickname string,
) (AuthenticatedAccount, error) {
	if f.account.AccountID == "" || f.account.AccountID != accountID {
		return AuthenticatedAccount{}, ErrUnauthenticated
	}
	f.nickname = nickname
	f.account.Nickname = nickname
	return f.account, nil
}

func (f *fakeIdentity) ChangePassword(
	_ context.Context,
	input ChangePasswordInput,
) error {
	f.changed = input
	return nil
}

func (f *fakeIdentity) ResetPassword(
	_ context.Context,
	input ResetPasswordInput,
) error {
	f.reset = input
	return nil
}

func (f *fakeIdentity) RegenerateRecoveryCodes(
	_ context.Context,
	_ string,
	currentPassword string,
) ([]string, error) {
	f.regeneratedPassword = currentPassword
	return f.regeneratedCodes, nil
}

func (f *fakeIdentity) VerifyAccountPassword(
	_ context.Context,
	_ string,
	password string,
) error {
	f.deletePassword = password
	return nil
}

func (f *fakeIdentity) CompleteAccountDeletion(
	_ context.Context,
	accountID string,
) error {
	f.completedDeletion = accountID
	return nil
}

func (f *fakeIdentity) ListDormantAccountIDs(
	context.Context,
	time.Time,
) ([]string, error) {
	return f.dormant, nil
}

type unauditedRunClient struct {
	orchestratorv1.OrchestratorClient
}

func (unauditedRunClient) SubmitRun(
	context.Context,
	*orchestratorv1.SubmitRunRequest,
	...grpc.CallOption,
) (*orchestratorv1.RunReceiptReply, error) {
	return &orchestratorv1.RunReceiptReply{}, nil
}

func newGatewayHTTPServerWithIdentity(
	t *testing.T,
	identity identityService,
) *httptest.Server {
	t.Helper()
	server := newGatewayKratosServer()
	service := newServiceWithIdentity(&fakeRunClient{}, identity, AuthOptions{
		CookieSecure: false,
	})
	registerGatewayRoutes(server, service)
	ts := httptest.NewServer(server)
	t.Cleanup(ts.Close)
	return ts
}

func newGatewayKratosServer() *khttp.Server {
	return khttp.NewServer()
}

func TestRegisterCreatesAccountAndSessionCookie(t *testing.T) {
	expiresAt := time.Now().Add(30 * 24 * time.Hour)
	identity := &fakeIdentity{
		output: RegisterOutput{
			Account: AuthenticatedAccount{
				AccountID: "00000000-0000-0000-0000-000000000101",
				Username:  "candidate",
				Nickname:  "考生",
			},
			RecoveryCodes: []string{
				"AAAA-BBBB", "CCCC-DDDD", "EEEE-FFFF", "GGGG-HHHH",
				"IIII-JJJJ", "KKKK-LLLL", "MMMM-NNNN", "OOOO-PPPP",
				"QQQQ-RRRR", "SSSS-TTTT",
			},
			SessionToken: "session-token",
			ExpiresAt:    expiresAt,
		},
	}
	ts := newGatewayHTTPServerWithIdentity(t, identity)
	body := []byte(`{
		"invite_code":"INVITE-1",
		"username":"candidate",
		"password":"correct horse battery staple",
		"nickname":"考生",
		"privacy_policy_version":"privacy-v1",
		"guardian_ack_recorded":true
	}`)
	req, err := http.NewRequest(
		http.MethodPost,
		ts.URL+"/v1/auth/register",
		bytes.NewReader(body),
	)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", "test-browser")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		var detail map[string]any
		_ = json.NewDecoder(resp.Body).Decode(&detail)
		t.Fatalf("status = %d, body = %#v", resp.StatusCode, detail)
	}
	var got struct {
		Account struct {
			Username string `json:"username"`
			Nickname string `json:"nickname"`
		} `json:"account"`
		RecoveryCodes []string `json:"recoveryCodes"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatal(err)
	}
	if got.Account.Username != "candidate" || got.Account.Nickname != "考生" {
		t.Fatalf("account = %#v", got.Account)
	}
	if len(got.RecoveryCodes) != 10 {
		t.Fatalf("recovery codes = %d", len(got.RecoveryCodes))
	}
	cookies := resp.Cookies()
	if len(cookies) != 2 {
		t.Fatalf("cookies = %#v", cookies)
	}
	sessionCookie := cookies[0]
	if sessionCookie.Name != sessionCookieName ||
		sessionCookie.Value == "" ||
		!sessionCookie.HttpOnly ||
		sessionCookie.SameSite != http.SameSiteLaxMode {
		t.Fatalf("session cookie = %#v", sessionCookie)
	}
	if csrfCookie := cookies[1]; csrfCookie.Name != csrfCookieName ||
		csrfCookie.HttpOnly ||
		csrfCookie.Value == "" {
		t.Fatalf("csrf cookie = %#v", csrfCookie)
	}
	if identity.registered.InviteCode != "INVITE-1" ||
		identity.registered.Username != "candidate" ||
		identity.registered.Password != "correct horse battery staple" ||
		!identity.registered.GuardianAckRecorded {
		t.Fatalf("register input = %#v", identity.registered)
	}
}

func TestPrivateRoutesRejectClientPrincipalAndUseSessionCookie(t *testing.T) {
	client := &fakeRunClient{}
	identity := &fakeIdentity{
		resolved: AuthenticatedAccount{
			AccountID: "00000000-0000-0000-0000-000000000101",
			Username:  "candidate",
		},
	}
	server := newGatewayKratosServer()
	service := newServiceWithIdentity(client, identity, AuthOptions{
		CookieSecure: false,
	})
	registerGatewayRoutes(server, service)
	ts := httptest.NewServer(server)
	t.Cleanup(ts.Close)

	forged, err := http.NewRequest(
		http.MethodGet,
		ts.URL+"/v1/runs/00000000-0000-0000-0000-000000000001",
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	forged.Header.Set("x-principal-id", "forged-principal")
	response, err := http.DefaultClient.Do(forged)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusUnauthorized {
		t.Fatalf("forged principal status = %d", response.StatusCode)
	}

	authorized, err := http.NewRequest(
		http.MethodGet,
		ts.URL+"/v1/runs/00000000-0000-0000-0000-000000000001",
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	authorized.AddCookie(&http.Cookie{
		Name:  sessionCookieName,
		Value: "server-session",
	})
	response, err = http.DefaultClient.Do(authorized)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("session status = %d", response.StatusCode)
	}
	if identity.token != "server-session" {
		t.Fatalf("resolved token = %q", identity.token)
	}
	if got := client.getMeta.Get("x-principal-id"); len(got) != 1 ||
		got[0] != identity.resolved.AccountID {
		t.Fatalf("orchestrator principal = %v", got)
	}
}

func TestLoginCreatesCookieAndLogoutRevokesSession(t *testing.T) {
	expiresAt := time.Now().Add(30 * 24 * time.Hour)
	identity := &fakeIdentity{
		loginOut: LoginOutput{
			Account: AuthenticatedAccount{
				AccountID: "00000000-0000-0000-0000-000000000101",
				Username:  "candidate",
				Nickname:  "考生",
			},
			SessionToken: "login-session-token",
			ExpiresAt:    expiresAt,
		},
	}
	ts := newGatewayHTTPServerWithIdentity(t, identity)
	login, err := http.NewRequest(
		http.MethodPost,
		ts.URL+"/v1/auth/login",
		bytes.NewBufferString(
			`{"username":"candidate","password":"correct horse battery staple"}`,
		),
	)
	if err != nil {
		t.Fatal(err)
	}
	login.Header.Set("Content-Type", "application/json")
	response, err := http.DefaultClient.Do(login)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("login status = %d", response.StatusCode)
	}
	cookies := response.Cookies()
	sessionCookie := cookieNamed(cookies, sessionCookieName)
	csrfCookie := cookieNamed(cookies, csrfCookieName)
	if sessionCookie == nil ||
		sessionCookie.Value != "login-session-token" ||
		csrfCookie == nil {
		t.Fatalf("login cookies = %#v", cookies)
	}
	if identity.loggedIn.Username != "candidate" ||
		identity.loggedIn.Password != "correct horse battery staple" {
		t.Fatalf("login input = %#v", identity.loggedIn)
	}

	logout, err := http.NewRequest(
		http.MethodPost,
		ts.URL+"/v1/auth/logout",
		bytes.NewBufferString(`{}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	logout.Header.Set("Content-Type", "application/json")
	logout.AddCookie(sessionCookie)
	logout.AddCookie(csrfCookie)
	logout.Header.Set("X-CSRF-Token", csrfCookie.Value)
	response, err = http.DefaultClient.Do(logout)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("logout status = %d", response.StatusCode)
	}
	if identity.logout != "login-session-token" {
		t.Fatalf("revoked token = %q", identity.logout)
	}
}

func cookieNamed(cookies []*http.Cookie, name string) *http.Cookie {
	for _, cookie := range cookies {
		if cookie.Name == name {
			return cookie
		}
	}
	return nil
}

func TestAccountProfileCanBeReadAndNicknameUpdated(t *testing.T) {
	accountID := "00000000-0000-0000-0000-000000000101"
	identity := &fakeIdentity{
		resolved: AuthenticatedAccount{AccountID: accountID, Username: "candidate"},
		account: AuthenticatedAccount{
			AccountID: accountID,
			Username:  "candidate",
			Nickname:  "考生",
		},
	}
	ts := newGatewayHTTPServerWithIdentity(t, identity)
	sessionCookie := &http.Cookie{Name: sessionCookieName, Value: "account-session"}
	csrfCookie := &http.Cookie{Name: csrfCookieName, Value: "csrf-token"}

	get, err := http.NewRequest(http.MethodGet, ts.URL+"/v1/account", nil)
	if err != nil {
		t.Fatal(err)
	}
	get.AddCookie(sessionCookie)
	response, err := http.DefaultClient.Do(get)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("get account status = %d", response.StatusCode)
	}
	var account struct {
		Account struct {
			Username string `json:"username"`
			Nickname string `json:"nickname"`
		} `json:"account"`
	}
	if err := json.NewDecoder(response.Body).Decode(&account); err != nil {
		t.Fatal(err)
	}
	if account.Account.Nickname != "考生" {
		t.Fatalf("account = %#v", account)
	}

	update, err := http.NewRequest(
		http.MethodPatch,
		ts.URL+"/v1/account",
		bytes.NewBufferString(`{"nickname":"湖南考生"}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	update.Header.Set("Content-Type", "application/json")
	update.AddCookie(sessionCookie)
	update.AddCookie(csrfCookie)
	update.Header.Set("X-CSRF-Token", csrfCookie.Value)
	response, err = http.DefaultClient.Do(update)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("update account status = %d", response.StatusCode)
	}
	if identity.nickname != "湖南考生" {
		t.Fatalf("nickname = %q", identity.nickname)
	}
}

func TestAccountPasswordChangeUsesCurrentSessionAndCSRF(t *testing.T) {
	accountID := "00000000-0000-0000-0000-000000000101"
	identity := &fakeIdentity{
		resolved: AuthenticatedAccount{AccountID: accountID},
	}
	ts := newGatewayHTTPServerWithIdentity(t, identity)
	req, err := http.NewRequest(
		http.MethodPost,
		ts.URL+"/v1/account/password:change",
		bytes.NewBufferString(
			`{"current_password":"old password value","new_password":"new password value"}`,
		),
	)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.AddCookie(&http.Cookie{
		Name:  sessionCookieName,
		Value: "current-session",
	})
	req.AddCookie(&http.Cookie{
		Name:  csrfCookieName,
		Value: "csrf-token",
	})
	req.Header.Set("X-CSRF-Token", "csrf-token")
	response, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", response.StatusCode)
	}
	if identity.changed.AccountID != accountID ||
		identity.changed.CurrentSessionToken != "current-session" ||
		identity.changed.CurrentPassword != "old password value" ||
		identity.changed.NewPassword != "new password value" {
		t.Fatalf("change password input = %#v", identity.changed)
	}
}

func TestListWishlistsForwardsAuthenticatedPrincipal(t *testing.T) {
	client := &fakeRunClient{}
	identity := &fakeIdentity{
		resolved: AuthenticatedAccount{
			AccountID: "00000000-0000-0000-0000-000000000101",
		},
	}
	server := newGatewayKratosServer()
	service := newServiceWithIdentity(client, identity, AuthOptions{
		CookieSecure: false,
	})
	registerGatewayRoutes(server, service)
	ts := httptest.NewServer(server)
	t.Cleanup(ts.Close)
	req, err := http.NewRequest(
		http.MethodGet,
		ts.URL+"/v1/wishlists?limit=10&status=CURRENT",
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	req.AddCookie(&http.Cookie{
		Name:  sessionCookieName,
		Value: "wishlist-session",
	})
	response, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", response.StatusCode)
	}
	if client.listWishlistsReq.GetLimit() != 10 ||
		client.listWishlistsReq.GetStatus() != "CURRENT" {
		t.Fatalf("request = %#v", client.listWishlistsReq)
	}
	if got := client.sessionMeta.Get("x-principal-id"); len(got) != 1 ||
		got[0] != identity.resolved.AccountID {
		t.Fatalf("principal = %v", got)
	}
}

func TestWishlistHistoryRestoreAndDeleteRoutesProxyOwnerCommands(t *testing.T) {
	client := &fakeRunClient{}
	identity := &fakeIdentity{
		resolved: AuthenticatedAccount{
			AccountID: "00000000-0000-0000-0000-000000000101",
		},
	}
	server := newGatewayKratosServer()
	service := newServiceWithIdentity(client, identity, AuthOptions{
		CookieSecure: false,
	})
	registerGatewayRoutes(server, service)
	ts := httptest.NewServer(server)
	t.Cleanup(ts.Close)
	resultVersion := "00000000-0000-0000-0000-000000000010"

	history, err := http.NewRequest(
		http.MethodGet,
		ts.URL+"/v1/results/"+resultVersion+"/wishlist-history",
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	authenticateRequest(history, identity.resolved.AccountID)
	response, err := http.DefaultClient.Do(history)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusOK ||
		client.historyReq.GetResultVersion() != resultVersion {
		t.Fatalf("history status = %d, request = %#v", response.StatusCode, client.historyReq)
	}

	restore, err := http.NewRequest(
		http.MethodPost,
		ts.URL+"/v1/results/"+resultVersion+"/wishlist-draft:restore",
		bytes.NewBufferString(`{"draft_version":1,"idempotency_key":"restore-1"}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	restore.Header.Set("Content-Type", "application/json")
	authenticateRequest(restore, identity.resolved.AccountID)
	response, err = http.DefaultClient.Do(restore)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusOK ||
		client.restoreReq.GetResultVersion() != resultVersion ||
		client.restoreReq.GetDraftVersion() != 1 {
		t.Fatalf("restore status = %d, request = %#v", response.StatusCode, client.restoreReq)
	}

	remove, err := http.NewRequest(
		http.MethodPost,
		ts.URL+"/v1/results/"+resultVersion+"/wishlist:delete",
		bytes.NewBufferString(`{"request_id":"delete-1"}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	remove.Header.Set("Content-Type", "application/json")
	authenticateRequest(remove, identity.resolved.AccountID)
	response, err = http.DefaultClient.Do(remove)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusOK ||
		client.deleteWishlistReq.GetResultVersion() != resultVersion ||
		client.deleteWishlistReq.GetRequestId() != "delete-1" {
		t.Fatalf("delete status = %d, request = %#v", response.StatusCode, client.deleteWishlistReq)
	}
}

func TestRecoveryCodesCanBeRegeneratedWithCurrentPassword(t *testing.T) {
	accountID := "00000000-0000-0000-0000-000000000101"
	identity := &fakeIdentity{
		resolved: AuthenticatedAccount{AccountID: accountID},
		regeneratedCodes: []string{
			"AAAAA-BBBBB", "CCCCC-DDDDD", "EEEEE-FFFFF", "GGGGG-HHHHH",
			"IIIII-JJJJJ", "KKKKK-LLLLL", "MMMMM-NNNNN", "OOOOO-PPPPP",
			"QQQQQ-RRRRR", "SSSSS-TTTTT",
		},
	}
	ts := newGatewayHTTPServerWithIdentity(t, identity)
	req, err := http.NewRequest(
		http.MethodPost,
		ts.URL+"/v1/account/recovery-codes:regenerate",
		bytes.NewBufferString(`{"current_password":"correct password"}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.AddCookie(&http.Cookie{Name: sessionCookieName, Value: "current-session"})
	req.AddCookie(&http.Cookie{Name: csrfCookieName, Value: "csrf-token"})
	req.Header.Set("X-CSRF-Token", "csrf-token")
	response, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", response.StatusCode)
	}
	var result struct {
		RecoveryCodes []string `json:"recoveryCodes"`
	}
	if err := json.NewDecoder(response.Body).Decode(&result); err != nil {
		t.Fatal(err)
	}
	if len(result.RecoveryCodes) != 10 ||
		identity.regeneratedPassword != "correct password" {
		t.Fatalf("result = %#v, password = %q", result, identity.regeneratedPassword)
	}
}

func TestDeleteAccountVerifiesPasswordAndDeletesPrivateData(t *testing.T) {
	accountID := "00000000-0000-0000-0000-000000000101"
	identity := &fakeIdentity{
		resolved: AuthenticatedAccount{AccountID: accountID},
	}
	client := &fakeRunClient{}
	server := newGatewayKratosServer()
	service := newServiceWithIdentity(client, identity, AuthOptions{
		CookieSecure: false,
	})
	registerGatewayRoutes(server, service)
	ts := httptest.NewServer(server)
	t.Cleanup(ts.Close)
	req, err := http.NewRequest(
		http.MethodPost,
		ts.URL+"/v1/account:delete",
		bytes.NewBufferString(`{
			"current_password":"correct password",
			"confirmation":"删除账号"
		}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.AddCookie(&http.Cookie{Name: sessionCookieName, Value: "delete-session"})
	req.AddCookie(&http.Cookie{Name: csrfCookieName, Value: "csrf-token"})
	req.Header.Set("X-CSRF-Token", "csrf-token")
	response, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", response.StatusCode)
	}
	if identity.deletePassword != "correct password" ||
		identity.completedDeletion != accountID {
		t.Fatalf(
			"password = %q, completed = %q",
			identity.deletePassword,
			identity.completedDeletion,
		)
	}
}

func TestDeleteAccountDoesNotFinalizeWhenPrivateCleanupFails(t *testing.T) {
	accountID := "00000000-0000-0000-0000-000000000101"
	identity := &fakeIdentity{
		resolved: AuthenticatedAccount{AccountID: accountID},
	}
	client := &fakeRunClient{
		deletePrincipalReply: &orchestratorv1.DeletePrincipalDataReply{
			Accepted: true,
			Status:   "DELETION_FAILED_RETRYABLE",
			Reason:   "DELETION_FAILED_RETRYABLE",
		},
	}
	server := newGatewayKratosServer()
	service := newServiceWithIdentity(client, identity, AuthOptions{
		CookieSecure: false,
	})
	registerGatewayRoutes(server, service)
	ts := httptest.NewServer(server)
	t.Cleanup(ts.Close)
	req, err := http.NewRequest(
		http.MethodPost,
		ts.URL+"/v1/account:delete",
		bytes.NewBufferString(`{
			"current_password":"correct password",
			"confirmation":"删除账号"
		}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.AddCookie(&http.Cookie{Name: sessionCookieName, Value: "delete-session"})
	req.AddCookie(&http.Cookie{Name: csrfCookieName, Value: "csrf-token"})
	req.Header.Set("X-CSRF-Token", "csrf-token")
	response, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d", response.StatusCode)
	}
	if identity.completedDeletion != "" {
		t.Fatalf("account was finalized: %q", identity.completedDeletion)
	}
}

func TestDormantAccountsUsePrivateDeletionThenBlockLogin(t *testing.T) {
	identity := &fakeIdentity{
		dormant: []string{
			"00000000-0000-0000-0000-000000000301",
			"00000000-0000-0000-0000-000000000302",
		},
	}
	client := &fakeRunClient{}
	service := newServiceWithIdentity(client, identity, AuthOptions{})
	if err := service.CleanupDormantAccounts(context.Background()); err != nil {
		t.Fatal(err)
	}
	if client.deletePrincipalReq == nil {
		t.Fatal("private deletion was not requested")
	}
	if identity.completedDeletion == "" {
		t.Fatal("account was not finalized")
	}
}

func TestPasswordResetConsumesRecoveryCodeWithoutLogin(t *testing.T) {
	identity := &fakeIdentity{}
	ts := newGatewayHTTPServerWithIdentity(t, identity)
	req, err := http.NewRequest(
		http.MethodPost,
		ts.URL+"/v1/auth/password:reset",
		bytes.NewBufferString(`{
			"username":"candidate",
			"recovery_code":"AAAAA-BBBBB",
			"new_password":"new password value"
		}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	response, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", response.StatusCode)
	}
	if len(response.Cookies()) != 0 {
		t.Fatalf("reset unexpectedly logged in: %#v", response.Cookies())
	}
	if identity.reset.Username != "candidate" ||
		identity.reset.RecoveryCode != "AAAAA-BBBBB" ||
		identity.reset.NewPassword != "new password value" {
		t.Fatalf("reset input = %#v", identity.reset)
	}
}
