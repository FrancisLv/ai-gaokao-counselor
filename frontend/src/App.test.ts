import { flushPromises, mount } from "@vue/test-utils";
import { nextTick } from "vue";
import { describe, expect, it, vi } from "vitest";

import App from "./App.vue";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("Run verification console", () => {
  it("submits a typed policy query", async () => {
    vi.stubEnv("VITE_PRINCIPAL_ID", "principal-policy");
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/v1/policy-queries" && init?.method === "POST") {
        return jsonResponse({
          runId: "run-policy",
          sessionId: "session-policy",
          status: "accepted",
        });
      }
      if (url.startsWith("/v1/runs/run-policy/events")) {
        return new Response("", {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      throw new Error(`unexpected request: ${init?.method ?? "GET"} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(App);

    await wrapper.get('[data-testid="mode-policy"]').trigger("click");
    await wrapper.get('[data-testid="policy-query"]').setValue(
      "转专业政策是什么？",
    );
    await wrapper
      .get('[data-testid="policy-application-year"]')
      .setValue("2025");
    await wrapper.get('[data-testid="policy-form"]').trigger("submit");
    await flushPromises();

    const requestInit = fetchMock.mock.calls[0]?.[1];
    const request = JSON.parse(requestInit!.body as string);
    expect(request).toMatchObject({
      query_text: "转专业政策是什么？",
      province: "湖南",
      application_year: 2025,
      category: "物理类",
      batch: "本科批",
      school_ids: ["1001"],
      document_types: ["charter"],
      mode: "hybrid",
    });
    expect(request).not.toHaveProperty("user_text");
    wrapper.unmount();
  });

  it("uses a fresh idempotency key for a second policy query", async () => {
    vi.stubEnv("VITE_PRINCIPAL_ID", "principal-policy");
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/v1/policy-queries" && init?.method === "POST") {
        return jsonResponse({
          runId: `run-policy-${fetchMock.mock.calls.length}`,
          sessionId: "session-policy",
          status: "accepted",
        });
      }
      if (url.includes("/events")) {
        return new Response("", {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      throw new Error(`unexpected request: ${init?.method ?? "GET"} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(App);

    await wrapper.get('[data-testid="mode-policy"]').trigger("click");
    await wrapper.get('[data-testid="policy-form"]').trigger("submit");
    await flushPromises();
    await wrapper
      .get('[data-testid="policy-query"]')
      .setValue("全国学生资助政策");
    await wrapper.get('[data-testid="policy-form"]').trigger("submit");
    await flushPromises();

    const policyBodies = fetchMock.mock.calls
      .filter(([url]) => String(url) === "/v1/policy-queries")
      .map(([, init]) => JSON.parse(init!.body as string));
    expect(policyBodies).toHaveLength(2);
    expect(policyBodies[0].idempotency_key).not.toBe(
      policyBodies[1].idempotency_key,
    );
    wrapper.unmount();
  });

  it("resumes SSE for a running run restored from the URL", async () => {
    vi.stubEnv("VITE_PRINCIPAL_ID", "principal-policy");
    history.replaceState({}, "", "/?run=run-running");
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/v1/runs/run-running" && !init?.method) {
        return jsonResponse({
          ...policyEvidenceRunView(),
          state: "RUNNING",
          stage: "RETRIEVING",
          policyEvidence: undefined,
          result: undefined,
        });
      }
      if (url.startsWith("/v1/runs/run-running/events")) {
        return new Response("", {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      throw new Error(`unexpected request: ${init?.method ?? "GET"} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(App);
    await flushPromises();

    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).startsWith("/v1/runs/run-running/events"),
      ),
    ).toBe(true);
    wrapper.unmount();
  });

  it("renders policy evidence as untrusted plain text", async () => {
    vi.stubEnv("VITE_PRINCIPAL_ID", "principal-policy");
    history.replaceState({}, "", "/?run=run-policy");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(policyEvidenceRunView())),
    );
    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.find('[data-testid="policy-form"]').exists()).toBe(true);
    expect(wrapper.get('[data-testid="policy-status"]').text()).toContain(
      "已找到可核验的文档证据",
    );
    expect(wrapper.get('[data-testid="evidence-title"]').text()).toBe(
      "湖南测试大学2025年招生章程",
    );
    expect(wrapper.get('[data-testid="evidence-section"]').text()).toContain(
      "第二章 > 转专业",
    );
    expect(wrapper.get('[data-testid="evidence-excerpt"]').text()).toContain(
      "<img src=x onerror=alert(1)>",
    );
    expect(wrapper.find("img").exists()).toBe(false);
    expect(wrapper.get('[data-testid="evidence-hash"]').text()).toBe(
      "aaaaaaaaaaaaaa",
    );
    expect(wrapper.get('[data-testid="evidence-locator"]').text()).toBe("0–14");
    expect(wrapper.get('[data-testid="evidence-source"]').attributes()).toMatchObject(
      {
        href: "https://example.invalid/charter",
        rel: "noopener noreferrer",
        target: "_blank",
      },
    );
  });

  it("keeps NO_EVIDENCE distinct from no restriction", async () => {
    vi.stubEnv("VITE_PRINCIPAL_ID", "principal-policy");
    history.replaceState({}, "", "/?run=run-policy");
    const run = policyEvidenceRunView();
    run.policyEvidence.retrievalStatus = "NO_EVIDENCE";
    run.policyEvidence.evidence = [];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(run)));
    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.get('[data-testid="policy-status"]').text()).toContain(
      "未检索到可靠证据",
    );
    expect(wrapper.text()).toContain("不代表没有相关政策限制");
    expect(wrapper.text()).not.toContain("没有限制");
    expect(wrapper.find('[data-testid="evidence-card"]').exists()).toBe(false);
  });

  it("does not link to a non-http evidence source", async () => {
    vi.stubEnv("VITE_PRINCIPAL_ID", "principal-policy");
    history.replaceState({}, "", "/?run=run-policy");
    const run = policyEvidenceRunView();
    run.policyEvidence.evidence[0].sourceUri = "javascript:alert(1)";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(run)));
    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.find('[data-testid="evidence-source"]').exists()).toBe(false);
    expect(wrapper.text()).toContain("来源地址不可安全打开");
  });

  it("explains an unsupported retrieval scope without inventing evidence", async () => {
    vi.stubEnv("VITE_PRINCIPAL_ID", "principal-policy");
    history.replaceState({}, "", "/?run=run-policy");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({
          runId: "run-policy",
          sessionId: "session-policy",
          taskKind: "POLICY_QA",
          state: "FAILED",
          stage: "RETRIEVING",
          terminalReason: "UNSUPPORTED_SCOPE",
          stateVersion: 3,
          releaseId: "release-policy",
        }),
      ),
    );
    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.get('[data-testid="policy-failure"]').text()).toContain(
      "当前适用范围不受支持",
    );
    expect(wrapper.find('[data-testid="policy-evidence"]').exists()).toBe(false);
    expect(wrapper.text()).not.toContain("无限制");
  });

  it("submits a structured profile and displays the run id", async () => {
    vi.stubEnv("VITE_PRINCIPAL_ID", "principal-a");
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        runId: "run-1",
        sessionId: "session-1",
        status: "accepted",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(App);

    await wrapper.get('[data-testid="province"]').setValue("湖南");
    await wrapper.get('[data-testid="application-year"]').setValue("2025");
    await wrapper.get('[data-testid="category"]').setValue("物理类");
    await wrapper.get('[data-testid="batch"]').setValue("本科批");
    await wrapper.get('[data-testid="score"]').setValue("600");
    await wrapper.get('[data-testid="subject-physics"]').setValue(true);
    await wrapper.get('[data-testid="subject-chemistry"]').setValue(true);
    await wrapper.get('[data-testid="subject-biology"]').setValue(true);
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(wrapper.text()).toContain("run-1");
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/plans",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          "x-principal-id": "principal-a",
        }),
      }),
    );
    const request = JSON.parse(fetchMock.mock.calls[0][1].body);
    const payload = JSON.parse(request.user_text);
    expect(payload.profile_input).toEqual({
      province: "湖南",
      application_year: 2025,
      category: "物理类",
      batch: "本科批",
      score: 600,
      subjects: ["物理", "化学", "生物"],
    });
  });

  it("restores a waiting profile confirmation from the run URL", async () => {
    vi.stubEnv("VITE_PRINCIPAL_ID", "principal-a");
    history.replaceState({}, "", "/?run=run-1");
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(waitingRunView()),
    );
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(App);
    await flushPromises();

    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/runs/run-1",
      expect.objectContaining({
        headers: { "x-principal-id": "principal-a" },
      }),
    );
    expect(wrapper.get('[data-testid="run-state"]').text()).toBe(
      "WAITING_HITL",
    );
    expect(wrapper.get('[data-testid="profile-card"]').text()).toContain(
      "profile-version-1",
    );
    expect(wrapper.get('[data-testid="profile-field-batch"]').text()).toContain(
      "DEFAULT",
    );
    expect(wrapper.get('[data-testid="profile-field-province"]').text()).toContain(
      "湖南",
    );
  });

  it("confirms the exact waiting profile version", async () => {
    vi.stubEnv("VITE_PRINCIPAL_ID", "principal-a");
    history.replaceState({}, "", "/?run=run-1");
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/v1/runs/run-1" && !init?.method) {
        return jsonResponse(waitingRunView());
      }
      if (
        url === "/v1/plans/session-1/profile:confirm" &&
        init?.method === "POST"
      ) {
        return jsonResponse({
          runId: "run-1",
          sessionId: "session-1",
          status: "accepted",
        });
      }
      throw new Error(`unexpected request: ${init?.method ?? "GET"} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(App);
    await flushPromises();

    await wrapper.get('[data-testid="confirm-profile"]').trigger("click");
    await flushPromises();

    const confirmCall = fetchMock.mock.calls.find(
      ([url]) => String(url) === "/v1/plans/session-1/profile:confirm",
    );
    expect(confirmCall).toBeDefined();
    const request = JSON.parse(confirmCall![1]!.body as string);
    expect(JSON.parse(request.profile_json)).toMatchObject({
      run_id: "run-1",
      interrupt_id: "interrupt-1",
      expected_state_version: 3,
      profile_version: "profile-version-1",
      answer: { type: "CONFIRM_PROFILE" },
    });
  });

  it("sends only one confirm request while the first click is pending", async () => {
    vi.stubEnv("VITE_PRINCIPAL_ID", "principal-a");
    history.replaceState({}, "", "/?run=run-1");
    let resolveConfirm!: (response: Response) => void;
    const pendingConfirm = new Promise<Response>((resolve) => {
      resolveConfirm = resolve;
    });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/v1/runs/run-1" && !init?.method) {
        return jsonResponse(waitingRunView());
      }
      if (
        url === "/v1/plans/session-1/profile:confirm" &&
        init?.method === "POST"
      ) {
        return pendingConfirm;
      }
      throw new Error(`unexpected request: ${init?.method ?? "GET"} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(App);
    await flushPromises();

    const button = wrapper.get('[data-testid="confirm-profile"]');
    await button.trigger("click");
    await nextTick();
    await button.trigger("click");
    await nextTick();

    expect(
      fetchMock.mock.calls.filter(
        ([url]) => String(url) === "/v1/plans/session-1/profile:confirm",
      ),
    ).toHaveLength(1);

    resolveConfirm(
      jsonResponse({
        runId: "run-1",
        sessionId: "session-1",
        status: "accepted",
      }),
    );
    await flushPromises();
  });

  it("renders ordered, deduplicated SSE events and ignores heartbeats", async () => {
    vi.stubEnv("VITE_PRINCIPAL_ID", "principal-a");
    const event = {
      runId: "run-1",
      eventSeq: "2",
      type: "PROGRESS",
      stage: "PROFILE_CONFIRM",
      stateVersion: "2",
      occurredAt: "2026-11-19T10:00:00Z",
      payloadVersion: 1,
      payload: {
        progress: {
          stage: "PROFILE_CONFIRM",
          message: "正在检查画像",
        },
      },
    };
    const streamBody =
      `id: 2\nevent: PROGRESS\ndata: ${JSON.stringify(event)}\n\n` +
      ": heartbeat\n\n" +
      `id: 2\nevent: PROGRESS\ndata: ${JSON.stringify(event)}\n\n`;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/v1/plans" && init?.method === "POST") {
        return jsonResponse({
          runId: "run-1",
          sessionId: "session-1",
          status: "accepted",
        });
      }
      if (url.startsWith("/v1/runs/run-1/events")) {
        return new Response(streamBody, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      throw new Error(`unexpected request: ${init?.method ?? "GET"} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(App);

    await wrapper.get('[data-testid="province"]').setValue("湖南");
    await wrapper.get('[data-testid="application-year"]').setValue("2025");
    await wrapper.get('[data-testid="category"]').setValue("物理类");
    await wrapper.get('[data-testid="batch"]').setValue("本科批");
    await wrapper.get('[data-testid="score"]').setValue("600");
    await wrapper.get('[data-testid="subject-physics"]').setValue(true);
    await wrapper.get('[data-testid="subject-chemistry"]').setValue(true);
    await wrapper.get('[data-testid="subject-biology"]').setValue(true);
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    await vi.waitFor(() => {
      expect(wrapper.findAll('[data-testid="event-row"]')).toHaveLength(1);
    });

    const row = wrapper.get('[data-testid="event-row"]');
    expect(row.text()).toContain("2");
    expect(row.text()).toContain("PROGRESS");
    expect(row.text()).toContain("PROFILE_CONFIRM");
    expect(wrapper.text()).not.toContain("heartbeat");
  });

  it("renders a non-empty controlled ALGORITHM_PREVIEW", async () => {
    vi.stubEnv("VITE_PRINCIPAL_ID", "principal-a");
    history.replaceState({}, "", "/?run=run-1");
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(succeededRunView()),
    );
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.get('[data-testid="run-state"]').text()).toBe("SUCCEEDED");
    expect(wrapper.get('[data-testid="preview-kind"]').text()).toBe(
      "ALGORITHM_PREVIEW",
    );
    expect(wrapper.text()).toContain("15860");
    expect(wrapper.text()).toContain("测试大学");
    expect(wrapper.text()).toContain("完整当年资格规则未核验");
    expect(wrapper.text()).toContain("不得作为正式志愿方案");
    expect(wrapper.get('[data-testid="controlled-preview"]').text()).toContain(
      "仅用于内部受控验证",
    );
    expect(wrapper.text()).not.toContain("保证录取");
  });

  it("renders a stable failure reason without a success preview", async () => {
    vi.stubEnv("VITE_PRINCIPAL_ID", "principal-a");
    history.replaceState({}, "", "/?run=run-1");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({
          runId: "run-1",
          sessionId: "session-1",
          taskKind: "GENERATE_PLAN",
          state: "FAILED",
          stage: "PROFILE_CONFIRM",
          terminalReason: "VALIDATION_FAILED",
          stateVersion: 4,
          releaseId: "rel_legacy_v1",
          expiresAt: "2026-11-19T12:30:00Z",
        }),
      ),
    );
    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.get('[data-testid="run-state"]').text()).toBe("FAILED");
    expect(wrapper.get('[data-testid="terminal-reason"]').text()).toBe(
      "VALIDATION_FAILED",
    );
    expect(wrapper.find('[data-testid="preview-kind"]').exists()).toBe(false);
  });

  it("shows a stable terminal reason when cancelling a completed run", async () => {
    vi.stubEnv("VITE_PRINCIPAL_ID", "principal-a");
    history.replaceState({}, "", "/?run=run-1");
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/v1/runs/run-1" && !init?.method) {
        return jsonResponse(succeededRunView());
      }
      if (url === "/v1/runs/run-1:cancel" && init?.method === "POST") {
        return jsonResponse({
          accepted: false,
          reason: "RUN_ALREADY_TERMINAL",
          runId: "run-1",
          state: "SUCCEEDED",
          stage: "PREVIEW_READY",
        });
      }
      throw new Error(`unexpected request: ${init?.method ?? "GET"} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(App);
    await flushPromises();

    await wrapper.get('[data-testid="cancel-run"]').trigger("click");
    await flushPromises();

    expect(wrapper.get('[data-testid="cancel-receipt"]').text()).toContain(
      "RUN_ALREADY_TERMINAL",
    );
    const cancelCall = fetchMock.mock.calls.find(
      ([url]) => String(url) === "/v1/runs/run-1:cancel",
    );
    expect(cancelCall).toBeDefined();
    expect(JSON.parse(cancelCall![1]!.body as string)).toHaveProperty(
      "idempotency_key",
    );
  });

  it("shows an authentication error without exposing run details", async () => {
    vi.stubEnv("VITE_PRINCIPAL_ID", "");
    history.replaceState({}, "", "/?run=run-secret");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ message: "PRINCIPAL_REQUIRED" }, 401),
      ),
    );
    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.get('[role="alert"]').text()).toContain("未认证");
    expect(wrapper.find('[data-testid="run-id"]').exists()).toBe(false);
    expect(wrapper.text()).not.toContain("principal-version");
  });

  it("reconnects SSE from the last consumed event sequence", async () => {
    vi.stubEnv("VITE_PRINCIPAL_ID", "principal-a");
    const firstEvent = {
      runId: "run-1",
      eventSeq: "2",
      type: "PROGRESS",
      stage: "INTAKE",
      stateVersion: "2",
      payload: { progress: { stage: "INTAKE", message: "started" } },
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/v1/plans" && init?.method === "POST") {
        return jsonResponse({
          runId: "run-1",
          sessionId: "session-1",
          status: "accepted",
        });
      }
      if (url === "/v1/runs/run-1/events?after_event_seq=0") {
        return new Response(
          `id: 2\nevent: PROGRESS\ndata: ${JSON.stringify(firstEvent)}\n\n`,
          {
            status: 200,
            headers: { "Content-Type": "text/event-stream" },
          },
        );
      }
      if (url === "/v1/runs/run-1/events?after_event_seq=2") {
        return new Response("", {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      throw new Error(`unexpected request: ${init?.method ?? "GET"} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(App);

    await wrapper.get('[data-testid="province"]').setValue("湖南");
    await wrapper.get('[data-testid="application-year"]').setValue("2025");
    await wrapper.get('[data-testid="category"]').setValue("物理类");
    await wrapper.get('[data-testid="batch"]').setValue("本科批");
    await wrapper.get('[data-testid="score"]').setValue("600");
    await wrapper.get('[data-testid="subject-physics"]').setValue(true);
    await wrapper.get('[data-testid="subject-chemistry"]').setValue(true);
    await wrapper.get('[data-testid="subject-biology"]').setValue(true);
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    await vi.waitFor(
      () => {
        expect(
          fetchMock.mock.calls.some(
            ([url]) =>
              String(url) === "/v1/runs/run-1/events?after_event_seq=2",
          ),
        ).toBe(true);
      },
      { timeout: 2_000 },
    );
    expect(wrapper.findAll('[data-testid="event-row"]')).toHaveLength(1);
    wrapper.unmount();
  });
});

function waitingRunView() {
  return {
    runId: "run-1",
    sessionId: "session-1",
    taskKind: "GENERATE_PLAN",
    state: "WAITING_HITL",
    stage: "PROFILE_CONFIRM",
    stateVersion: 3,
    releaseId: "rel_legacy_v1",
    expiresAt: "2026-11-19T12:30:00Z",
    currentInterrupt: {
      interruptId: "interrupt-1",
      kind: "PROFILE_CONFIRM",
      state: "OPEN",
      profileVersion: "profile-version-1",
      fields: {
        province: {
          state: "PROVIDED",
          source: "USER_FORM",
          stringValue: "湖南",
        },
        application_year: {
          state: "PROVIDED",
          source: "USER_FORM",
          intValue: 2025,
        },
        category: {
          state: "PROVIDED",
          source: "USER_FORM",
          stringValue: "物理类",
        },
        batch: {
          state: "DEFAULT",
          source: "SYSTEM_DEFAULT",
          stringValue: "本科批",
        },
        score: {
          state: "PROVIDED",
          source: "USER_FORM",
          intValue: 600,
        },
        subjects: {
          state: "PROVIDED",
          source: "USER_FORM",
          stringList: { values: ["物理", "化学", "生物"] },
        },
      },
      findings: [],
    },
  };
}

function succeededRunView() {
  return {
    runId: "run-1",
    sessionId: "session-1",
    taskKind: "GENERATE_PLAN",
    state: "SUCCEEDED",
    stage: "PREVIEW_READY",
    stateVersion: 5,
    releaseId: "rel_legacy_v1",
    confirmedProfileVersion: "profile-version-1",
    expiresAt: "2026-11-19T12:30:00Z",
    resultPreview: {
      resultVersion: "result-1",
      runId: "run-1",
      releaseId: "rel_legacy_v1",
      confirmedProfileVersion: "profile-version-1",
      rank: 15860,
      percentile: 0.1234,
      rushCandidates: [],
      stableCandidates: [
        {
          candidate: {
            universityCode: "U001",
            universityName: "测试大学",
            majorGroup: "01",
            schoolTier: "985",
            location: "长沙",
          },
          prob: {
            probability: 0.67,
            method: "weighted_hit",
            dataYears: 3,
            category: "稳",
          },
        },
      ],
      safeCandidates: [],
      auditBasis: {
        userRank: "15860",
        method: "weighted_hit",
        province: "湖南",
        category: "物理类",
      },
      keptCandidates: [],
      rejectedCandidates: [
        {
          rule: "subject_req",
          reason: "选科不符",
        },
      ],
      wishlist: {
        candidates: [
          {
            cwp: {
              candidate: {
                universityCode: "U001",
                universityName: "测试大学",
                majorGroup: "01",
              },
              prob: {
                probability: 0.67,
                method: "weighted_hit",
                dataYears: 3,
                category: "稳",
              },
            },
            finalRank: 1,
            utility: { score: 0.8 },
          },
        ],
        report: {
          stableCount: 1,
          safeGuardOk: false,
        },
      },
      verificationSummary: [
        {
          key: "release_binding",
          status: "CHECKED",
          detail: "rel_legacy_v1",
        },
      ],
      unverifiedConditions: [
        {
          code: "FULL_ELIGIBILITY_RULES",
          message: "完整当年资格规则未核验",
        },
      ],
      riskNotice: "仅供内部受控验收。",
      disclaimer: "结果仅供决策参考，不得作为正式志愿方案。",
      internalControlledOnly: true,
      formalRecommendation: false,
      resultKind: "ALGORITHM_PREVIEW",
    },
  };
}

function policyEvidenceRunView() {
  return {
    runId: "run-policy",
    sessionId: "session-policy",
    taskKind: "POLICY_QA",
    state: "SUCCEEDED",
    stage: "EVIDENCE_READY",
    stateVersion: 3,
    releaseId: "release-policy",
    result: {
      resultVersion: "result-policy",
      resultKind: "POLICY_EVIDENCE",
    },
    policyEvidence: {
      resultVersion: "result-policy",
      resultKind: "POLICY_EVIDENCE",
      runId: "run-policy",
      releaseId: "release-policy",
      retrievalStatus: "FOUND",
      vectorCandidates: 2,
      keywordCandidates: 1,
      rerankEnabled: false,
      evidence: [
        {
          evidenceId: "evidence-policy",
          docId: "doc-charter",
          chunkId: "chunk-transfer",
          title: "湖南测试大学2025年招生章程",
          documentType: "charter",
          issuer: "湖南测试大学",
          sourceUri: "https://example.invalid/charter",
          fetchedAt: "2025-01-02T00:00:00Z",
          sectionPath: "第二章 > 转专业",
          excerpt: "<img src=x onerror=alert(1)>",
          excerptSha256: "a".repeat(64),
          charStart: 0,
          charEnd: 14,
          regions: ["湖南"],
          categories: ["物理类"],
          batches: ["本科批"],
          validFrom: "2025-01-01",
          validTo: "2025-12-31",
          releaseId: "release-policy",
        },
      ],
    },
  };
}
