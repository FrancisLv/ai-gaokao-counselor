import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App.vue";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
  history.replaceState({}, "", "/");
});

describe("conversation-first plan flow", () => {
  it("starts from the A welcome page and asks for preferences", async () => {
    vi.stubEnv("VITE_PRINCIPAL_ID", "principal-a");
    const wrapper = mount(App);

    expect(wrapper.text()).toContain("把复杂的志愿填报，聊明白");
    await wrapper.findAll(".welcome-prompts button")[0].trigger("click");

    expect(wrapper.text()).toContain("再补充城市、专业兴趣和风险偏好");
    expect(wrapper.text()).toContain("物理类");
    expect(wrapper.text()).toContain("612");
    expect(wrapper.text()).toContain("物理 + 化学 + 生物");
    wrapper.unmount();
  });

  it("submits the collected structured profile to the existing plan API", async () => {
    vi.stubEnv("VITE_PRINCIPAL_ID", "principal-a");
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url === "/v1/plans") {
        return jsonResponse({
          runId: "run-1",
          sessionId: "session-1",
          status: "accepted",
        });
      }
      throw new Error(`unexpected request: ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    vi.useFakeTimers();
    const wrapper = mount(App);

    await wrapper.findAll(".welcome-prompts button")[0].trigger("click");
    await wrapper.get(".preference-card .primary-button").trigger("click");
    await flushPromises();

    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/plans",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          "x-principal-id": "principal-a",
        }),
      }),
    );
    const request = JSON.parse(fetchMock.mock.calls[0][1]!.body as string);
    expect(JSON.parse(request.user_text)).toMatchObject({
      task_kind: "GENERATE_PLAN",
      profile_input: {
        province: "湖南",
        application_year: 2025,
        category: "物理类",
        batch: "本科批",
        score: 612,
        subjects: ["物理", "化学", "生物"],
      },
    });
    expect(wrapper.text()).toContain("正在计算冲稳保");
    wrapper.unmount();
  });

  it("restores a waiting backend profile confirmation", async () => {
    vi.stubEnv("VITE_PRINCIPAL_ID", "principal-a");
    history.replaceState({}, "", "/?run=run-1");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(waitingRunView())),
    );
    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.text()).toContain("请确认我理解的画像");
    expect(wrapper.text()).toContain("物理类");
    expect(wrapper.text()).toContain("612");
    expect(wrapper.text()).toContain("本科批");
    wrapper.unmount();
  });

  it("opens the immutable report from a completed formal result", async () => {
    vi.stubEnv("VITE_PRINCIPAL_ID", "principal-a");
    history.replaceState({}, "", "/?run=run-formal");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(formalRunView())),
    );
    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.text()).toContain("你的冲稳保方案已生成");
    expect(wrapper.text()).toContain("中南大学");
    const reportLink = wrapper.get('.result-footer a[href^="report.html?run="]');
    expect(reportLink.attributes("href")).toBe(
      "report.html?run=run-formal",
    );
    expect(reportLink.attributes("target")).toBe("_blank");
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
    releaseId: "release-2025",
    currentInterrupt: {
      interruptId: "interrupt-1",
      kind: "PROFILE_CONFIRM",
      state: "OPEN",
      profileVersion: "profile-1",
      fields: {
        province: {
          state: "PROVIDED",
          source: "USER",
          stringValue: "湖南",
        },
        application_year: {
          state: "PROVIDED",
          source: "USER",
          intValue: 2025,
        },
        category: {
          state: "PROVIDED",
          source: "USER",
          stringValue: "物理类",
        },
        batch: {
          state: "DEFAULT",
          source: "SYSTEM",
          stringValue: "本科批",
        },
        score: {
          state: "PROVIDED",
          source: "USER",
          intValue: 612,
        },
        subjects: {
          state: "PROVIDED",
          source: "USER",
          stringList: { values: ["物理", "化学", "生物"] },
        },
      },
    },
  };
}

function formalRunView() {
  return {
    ...waitingRunView(),
    runId: "run-formal",
    state: "SUCCEEDED",
    stage: "FORMAL_READY",
    stateVersion: 5,
    currentInterrupt: undefined,
    formalResult: {
      resultVersion: "result-formal",
      resultKind: "V1_FORMAL",
      releaseId: "release-2025",
      confirmedProfileVersion: "profile-1",
      verificationStatus: "PASS",
      citationCoverage: 1,
      resultState: "ACTIVE",
      disclaimer: "本方案仅供志愿参考。",
      riskNotice: "概率为历史估计。",
      noAdmissionGuarantee: "不保证录取。",
      report: {
        reportVersion: "m5b-report-v1",
        generationMode: "DETERMINISTIC",
        claimIds: [],
        unverifiedFacts: [],
        sections: [
          {
            sectionKey: "plan",
            title: "方案概况",
            body: "湖南物理类考生位次 8640 名，百分位 0.1200。",
            citations: [],
          },
          {
            sectionKey: "candidate:1001|03",
            title: "1. 中南大学 03专业组",
            body:
              "院校：中南大学 03专业组。专业：计算机科学（080901）。录取概率约 34%，策略分档：冲，招生计划数：12，样本年份为 2022、2023、2024。",
            citations: [],
          },
        ],
      },
    },
  };
}
