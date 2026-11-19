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
