package gateway

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	khttp "github.com/go-kratos/kratos/v2/transport/http"
	"google.golang.org/protobuf/types/known/timestamppb"

	gatewayv1 "github.com/choucisan/ai-gaokao-counselor/api/gateway/v1"
	orchestratorv1 "github.com/choucisan/ai-gaokao-counselor/api/orchestrator/v1"
)

type fakeRunEventStream struct {
	events []*orchestratorv1.RunEvent
	delay  time.Duration
	index  int
}

func (f *fakeRunEventStream) Recv() (*orchestratorv1.RunEvent, error) {
	if f.index >= len(f.events) {
		if f.delay > 0 {
			time.Sleep(f.delay)
		}
		return nil, io.EOF
	}
	event := f.events[f.index]
	f.index++
	return event, nil
}

func newGatewayHTTPServerWithOptions(
	t *testing.T,
	client runClient,
	options EventStreamOptions,
) *httptest.Server {
	t.Helper()
	server := khttp.NewServer()
	service := NewService(client)
	gatewayv1.RegisterGatewayHTTPServer(server, service)
	RegisterRunEventStream(server, service, options)
	ts := httptest.NewServer(server)
	t.Cleanup(ts.Close)
	return ts
}

func newGatewayHTTPServer(t *testing.T, client runClient) *httptest.Server {
	return newGatewayHTTPServerWithOptions(t, client, EventStreamOptions{
		HeartbeatInterval: time.Hour,
	})
}

func TestCreatePlanReturnsRunIDThroughHTTP(t *testing.T) {
	client := &fakeRunClient{}
	ts := newGatewayHTTPServer(t, client)
	body := []byte(`{
		"user_text":"{\"task_kind\":\"GENERATE_PLAN\",\"profile_input\":{\"province\":\"湖南\",\"application_year\":2025,\"category\":\"物理类\",\"batch\":\"本科批\",\"score\":600,\"subjects\":[\"物理\",\"化学\",\"生物\"]},\"idempotency_key\":\"gateway-http-submit\"}"
	}`)
	req, err := http.NewRequest(http.MethodPost, ts.URL+"/v1/plans", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("x-principal-id", "principal-a")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var got map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatal(err)
	}
	if got["runId"] != "00000000-0000-0000-0000-000000000001" {
		t.Fatalf("runId = %#v", got["runId"])
	}
}

func TestCreatePolicyQueryReturnsRunIDThroughHTTP(t *testing.T) {
	client := &fakeRunClient{}
	ts := newGatewayHTTPServer(t, client)
	body := []byte(`{
		"query_text":"转专业政策是什么？",
		"province":"湖南",
		"application_year":2025,
		"category":"物理类",
		"batch":"本科批",
		"school_ids":["1001"],
		"document_types":["charter"],
		"mode":"keyword",
		"idempotency_key":"gateway-policy-query"
	}`)
	req, err := http.NewRequest(
		http.MethodPost,
		ts.URL+"/v1/policy-queries",
		bytes.NewReader(body),
	)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("x-principal-id", "principal-policy")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	if client.submitReq.GetTaskKind() != orchestratorv1.TaskKind_POLICY_QA {
		t.Fatalf("task_kind = %v", client.submitReq.GetTaskKind())
	}
}

func TestGetRunReturnsResumeContextThroughHTTP(t *testing.T) {
	client := &fakeRunClient{}
	ts := newGatewayHTTPServer(t, client)
	req, err := http.NewRequest(
		http.MethodGet,
		ts.URL+"/v1/runs/00000000-0000-0000-0000-000000000001",
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("x-principal-id", "principal-a")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var got map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatal(err)
	}
	if got["state"] != "WAITING_HITL" {
		t.Fatalf("state = %#v", got["state"])
	}
	if got["currentInterrupt"] == nil {
		t.Fatal("缺少 currentInterrupt")
	}
	if got["resultPreview"] == nil {
		t.Fatal("缺少 resultPreview")
	}
	if got["expiresAt"] == nil {
		t.Fatal("缺少 expiresAt")
	}
}

func TestGetRunReturnsPolicyEvidenceThroughHTTP(t *testing.T) {
	client := &fakeRunClient{
		getReply: &orchestratorv1.RunViewReply{
			RunId:        "00000000-0000-0000-0000-000000000001",
			SessionId:    "00000000-0000-0000-0000-000000000002",
			TaskKind:     orchestratorv1.TaskKind_POLICY_QA,
			State:        orchestratorv1.RunState_SUCCEEDED,
			Stage:        "EVIDENCE_READY",
			StateVersion: 3,
			ReleaseId:    "rel-policy",
			Result: &orchestratorv1.ResultReference{
				ResultVersion: "00000000-0000-0000-0000-000000000003",
				ResultKind:    "POLICY_EVIDENCE",
			},
			PolicyEvidence: &orchestratorv1.PolicyEvidenceResult{
				ResultVersion:     "00000000-0000-0000-0000-000000000003",
				ResultKind:        "POLICY_EVIDENCE",
				RunId:             "00000000-0000-0000-0000-000000000001",
				ReleaseId:         "rel-policy",
				RetrievalStatus:   "FOUND",
				VectorCandidates:  1,
				KeywordCandidates: 1,
				Evidence: []*orchestratorv1.PolicyEvidenceItem{
					{
						EvidenceId: "evidence-policy",
						DocId:      "doc-policy",
						ChunkId:    "chunk-policy",
						Title:      "测试政策",
						Excerpt:    "学生可以申请转专业。",
						ReleaseId:  "rel-policy",
					},
				},
			},
		},
	}
	ts := newGatewayHTTPServer(t, client)
	req, err := http.NewRequest(
		http.MethodGet,
		ts.URL+"/v1/runs/00000000-0000-0000-0000-000000000001",
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("x-principal-id", "principal-policy")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var got map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatal(err)
	}
	policyEvidence, ok := got["policyEvidence"].(map[string]any)
	if !ok {
		t.Fatalf("policyEvidence = %#v", got["policyEvidence"])
	}
	if policyEvidence["retrievalStatus"] != "FOUND" {
		t.Fatalf("retrievalStatus = %#v", policyEvidence["retrievalStatus"])
	}
}

func TestCancelRunReturnsReceiptThroughHTTP(t *testing.T) {
	client := &fakeRunClient{}
	ts := newGatewayHTTPServer(t, client)
	body := bytes.NewBufferString(`{"idempotency_key":"gateway-cancel"}`)
	req, err := http.NewRequest(
		http.MethodPost,
		ts.URL+"/v1/runs/00000000-0000-0000-0000-000000000001:cancel",
		body,
	)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("x-principal-id", "principal-a")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var got map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatal(err)
	}
	if got["accepted"] != true {
		t.Fatalf("accepted = %#v", got["accepted"])
	}
	if got["runId"] != "00000000-0000-0000-0000-000000000001" {
		t.Fatalf("runId = %#v", got["runId"])
	}
}

func TestCancelTerminalRunPassesStableReasonThroughHTTP(t *testing.T) {
	client := &fakeRunClient{
		cancelReply: &orchestratorv1.RunReceiptReply{
			Accepted: false,
			Reason:   "RUN_ALREADY_TERMINAL",
			RunId:    "00000000-0000-0000-0000-000000000001",
			State:    orchestratorv1.RunState_SUCCEEDED,
			Stage:    "PREVIEW_READY",
		},
	}
	ts := newGatewayHTTPServer(t, client)
	req, err := http.NewRequest(
		http.MethodPost,
		ts.URL+"/v1/runs/00000000-0000-0000-0000-000000000001:cancel",
		bytes.NewBufferString(`{"idempotency_key":"gateway-cancel-terminal"}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("x-principal-id", "principal-a")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var got map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatal(err)
	}
	if got["reason"] != "RUN_ALREADY_TERMINAL" {
		t.Fatalf("reason = %#v", got["reason"])
	}
}

func TestWatchRunResumesAfterLastEventIDThroughSSE(t *testing.T) {
	client := &fakeRunClient{
		watchEvents: []*orchestratorv1.RunEvent{
			{
				RunId:          "00000000-0000-0000-0000-000000000001",
				EventSeq:       2,
				Type:           orchestratorv1.RunEventType_INTERRUPT,
				Stage:          "PROFILE_CONFIRM",
				StateVersion:   2,
				OccurredAt:     timestamppb.Now(),
				PayloadVersion: 1,
				Payload: &orchestratorv1.RunEvent_Interrupt{
					Interrupt: &orchestratorv1.InterruptView{
						InterruptId: "00000000-0000-0000-0000-000000000003",
						Kind:        "PROFILE_CONFIRM",
						State:       "OPEN",
					},
				},
			},
			{
				RunId:          "00000000-0000-0000-0000-000000000001",
				EventSeq:       3,
				Type:           orchestratorv1.RunEventType_RESULT,
				Stage:          "PREVIEW_READY",
				StateVersion:   3,
				OccurredAt:     timestamppb.Now(),
				PayloadVersion: 2,
				Payload: &orchestratorv1.RunEvent_Result{
					Result: &orchestratorv1.AlgorithmPreview{
						ResultVersion: "00000000-0000-0000-0000-000000000005",
						ResultKind:    "ALGORITHM_PREVIEW",
					},
				},
			},
		},
	}
	ts := newGatewayHTTPServer(t, client)
	req, err := http.NewRequest(
		http.MethodGet,
		ts.URL+"/v1/runs/00000000-0000-0000-0000-000000000001/events",
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("x-principal-id", "principal-a")
	req.Header.Set("Last-Event-ID", "1")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	if contentType := resp.Header.Get("Content-Type"); !strings.HasPrefix(contentType, "text/event-stream") {
		t.Fatalf("content-type = %q", contentType)
	}
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	body := string(raw)
	if !strings.Contains(body, "id: 2") || !strings.Contains(body, "id: 3") {
		t.Fatalf("SSE 未按游标补发: %q", body)
	}
	if strings.Contains(body, "id: 1") {
		t.Fatalf("SSE 重发了 Last-Event-ID 事件: %q", body)
	}
	if client.watchReq.GetAfterEventSeq() != 1 {
		t.Fatalf("after_event_seq = %d", client.watchReq.GetAfterEventSeq())
	}
}

func TestWatchRunUsesQueryCursorInsteadOfHeaderThroughSSE(t *testing.T) {
	client := &fakeRunClient{
		watchEvents: []*orchestratorv1.RunEvent{
			{
				RunId:      "00000000-0000-0000-0000-000000000001",
				EventSeq:   6,
				Type:       orchestratorv1.RunEventType_PROGRESS,
				Stage:      "PREVIEW_READY",
				OccurredAt: timestamppb.Now(),
				Payload: &orchestratorv1.RunEvent_Progress{
					Progress: &orchestratorv1.ProgressPayload{
						Stage: "PREVIEW_READY",
					},
				},
			},
		},
	}
	ts := newGatewayHTTPServer(t, client)
	req, err := http.NewRequest(
		http.MethodGet,
		ts.URL+"/v1/runs/00000000-0000-0000-0000-000000000001/events?after_event_seq=5",
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("x-principal-id", "principal-a")
	req.Header.Set("Last-Event-ID", "1")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if _, err := io.ReadAll(resp.Body); err != nil {
		t.Fatal(err)
	}
	if client.watchReq.GetAfterEventSeq() != 5 {
		t.Fatalf("after_event_seq = %d", client.watchReq.GetAfterEventSeq())
	}
}

func TestSSEHeartbeatDoesNotConsumeEventID(t *testing.T) {
	client := &fakeRunClient{watchDelay: 20 * time.Millisecond}
	ts := newGatewayHTTPServerWithOptions(t, client, EventStreamOptions{
		HeartbeatInterval: time.Millisecond,
	})
	req, err := http.NewRequest(
		http.MethodGet,
		ts.URL+"/v1/runs/00000000-0000-0000-0000-000000000001/events",
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("x-principal-id", "principal-a")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	body := string(raw)
	if !strings.Contains(body, ": heartbeat") {
		t.Fatalf("缺少 heartbeat: %q", body)
	}
	if strings.Contains(body, "id:") {
		t.Fatalf("heartbeat 占用了业务 event id: %q", body)
	}
}

func TestGetRunRequiresTrustedPrincipal(t *testing.T) {
	ts := newGatewayHTTPServer(t, &fakeRunClient{})
	resp, err := http.Get(
		ts.URL + "/v1/runs/00000000-0000-0000-0000-000000000001",
	)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("status = %d", resp.StatusCode)
	}
}

func TestConfirmProfileReturnsRunIDThroughHTTP(t *testing.T) {
	client := &fakeRunClient{}
	ts := newGatewayHTTPServer(t, client)
	body := bytes.NewBufferString(`{
		"profile_json":"{\"run_id\":\"00000000-0000-0000-0000-000000000001\",\"interrupt_id\":\"00000000-0000-0000-0000-000000000003\",\"expected_state_version\":4,\"profile_version\":\"00000000-0000-0000-0000-000000000004\",\"idempotency_key\":\"gateway-confirm\",\"answer\":{\"type\":\"CONFIRM_PROFILE\"}}"
	}`)
	req, err := http.NewRequest(
		http.MethodPost,
		ts.URL+"/v1/plans/00000000-0000-0000-0000-000000000002/profile:confirm",
		body,
	)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("x-principal-id", "principal-a")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var got map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatal(err)
	}
	if got["runId"] != "00000000-0000-0000-0000-000000000001" {
		t.Fatalf("runId = %#v", got["runId"])
	}
}

func TestBackfillProfileReturnsRunIDThroughHTTP(t *testing.T) {
	client := &fakeRunClient{}
	ts := newGatewayHTTPServer(t, client)
	body := bytes.NewBufferString(`{
		"backfill_json":"{\"run_id\":\"00000000-0000-0000-0000-000000000001\",\"interrupt_id\":\"00000000-0000-0000-0000-000000000003\",\"expected_state_version\":4,\"idempotency_key\":\"gateway-backfill\",\"answer\":{\"type\":\"ANSWER_FIELDS\",\"fields\":{\"foreign_language\":{\"state\":\"PROVIDED\",\"source\":\"USER_FORM\",\"value\":\"英语\"}}}}"
	}`)
	req, err := http.NewRequest(
		http.MethodPost,
		ts.URL+"/v1/plans/00000000-0000-0000-0000-000000000002/profile:backfill",
		body,
	)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("x-principal-id", "principal-a")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var got map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatal(err)
	}
	if got["runId"] != "00000000-0000-0000-0000-000000000001" {
		t.Fatalf("runId = %#v", got["runId"])
	}
}

func TestDoubleConfirmUsesOneIdempotencyKeyThroughHTTP(t *testing.T) {
	client := &fakeRunClient{}
	ts := newGatewayHTTPServer(t, client)
	requestJSON := `{
		"profile_json":"{\"run_id\":\"00000000-0000-0000-0000-000000000001\",\"interrupt_id\":\"00000000-0000-0000-0000-000000000003\",\"expected_state_version\":4,\"profile_version\":\"00000000-0000-0000-0000-000000000004\",\"idempotency_key\":\"gateway-double-confirm\",\"answer\":{\"type\":\"CONFIRM_PROFILE\"}}"
	}`
	for i := 0; i < 2; i++ {
		req, err := http.NewRequest(
			http.MethodPost,
			ts.URL+"/v1/plans/00000000-0000-0000-0000-000000000002/profile:confirm",
			bytes.NewBufferString(requestJSON),
		)
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("x-principal-id", "principal-a")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		_ = resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("request %d status = %d", i+1, resp.StatusCode)
		}
	}
	if len(client.resumeKeys) != 2 {
		t.Fatalf("resume calls = %d", len(client.resumeKeys))
	}
	if client.resumeKeys[0] != "gateway-double-confirm" ||
		client.resumeKeys[1] != client.resumeKeys[0] {
		t.Fatalf("idempotency keys = %#v", client.resumeKeys)
	}
}
