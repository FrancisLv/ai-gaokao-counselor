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
  it("groups wishlist and account settings in the conversation top bar", () => {
    const wrapper = mount(App);
    const actions = wrapper.get(".chat-topbar .session-actions");

    expect(actions.findAll(".session-badge").map((link) => link.text())).toEqual([
      "我的志愿表",
      "账户设置",
    ]);
    expect(actions.get('a[href="/wishlists.html"]')).toBeTruthy();
    expect(actions.get('a[href="/account.html"]')).toBeTruthy();
    expect(wrapper.get(".workspace-nav a.active").text()).toContain("对话规划");
    expect(wrapper.find(".workspace-sidebar a[href='/account.html']").exists()).toBe(
      false,
    );
    wrapper.unmount();
  });

  it("starts from the A welcome page and asks for preferences", async () => {
    const wrapper = mount(App);

    expect(wrapper.text()).toContain("把复杂的志愿填报，聊明白");
    await wrapper.findAll(".welcome-prompts button")[0].trigger("click");

    expect(wrapper.text()).toContain("再补充城市、专业兴趣和风险偏好");
    expect(wrapper.text()).toContain("物理类");
    expect(wrapper.text()).toContain("612");
    expect(wrapper.text()).toContain("物理 + 化学 + 生物");
    wrapper.unmount();
  });

  it("interprets wanting to study physics as a major interest, not a category", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ sessions: [] })),
    );
    const wrapper = mount(App);
    const textarea = wrapper.get('textarea[aria-label="输入消息"]');

    await textarea.setValue("我想学物理学");
    await textarea.trigger("keydown", { key: "Enter" });
    await flushPromises();

    expect(wrapper.text()).toContain("我理解为你想读本科专业“物理学”");
    expect(wrapper.text()).toContain("是否也接受相关专业");
    const category = wrapper
      .findAll(".chat-inspector .inspector-kv")
      .find((item) => item.text().includes("科类"));
    expect(category?.get("strong").text()).toBe("待补充");
    const subjects = wrapper
      .findAll(".chat-inspector .inspector-kv")
      .find((item) => item.text().includes("选科"));
    expect(subjects?.get("strong").text()).toBe("待补充");
    expect(wrapper.text()).toContain("先确认你的科类");

    await textarea.setValue("也接受相关专业");
    await textarea.trigger("keydown", { key: "Enter" });
    await flushPromises();

    expect(wrapper.text()).toContain("物理学类");
    expect(wrapper.text()).toContain("应用物理学");
    expect(wrapper.text()).toContain("核物理");
    wrapper.unmount();
  });

  it("does not preselect computer or electronic information as major interests", async () => {
    const wrapper = mount(App);

    await wrapper.findAll(".welcome-prompts button")[0].trigger("click");
    const majorSection = wrapper
      .findAll(".preference-card section")
      .find((item) => item.text().includes("专业兴趣"));

    expect(majorSection).toBeTruthy();
    expect(
      majorSection!
        .findAll("button.primary")
        .map((button) => button.text()),
    ).toEqual([]);
    expect(majorSection!.text()).toContain("物理学");

    const physics = majorSection!
      .findAll("button")
      .find((button) => button.text() === "物理学")!;
    await physics.trigger("click");
    expect(physics.classes()).toContain("primary");
    const undecided = majorSection!
      .findAll("button")
      .find((button) => button.text() === "还没想好")!;
    await undecided.trigger("click");
    expect(
      majorSection!
        .findAll("button.primary")
        .map((button) => button.text()),
    ).toEqual([]);
    wrapper.unmount();
  });

  it("redirects intake off-topic messages and prioritizes crisis support", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ sessions: [] })),
    );
    const wrapper = mount(App);

    await wrapper.findAll(".welcome-prompts button")[2].trigger("click");
    const textarea = wrapper.get('textarea[aria-label="输入消息"]');
    await textarea.setValue("今天天气怎么样？");
    await textarea.trigger("keydown", { key: "Enter" });
    await flushPromises();

    expect(wrapper.text()).toContain("不能可靠回答这个问题");
    expect(wrapper.text()).toContain("湖南 2025 本科批");

    await textarea.setValue("我不想活了");
    await textarea.trigger("keydown", { key: "Enter" });
    await flushPromises();

    expect(wrapper.text()).toContain("请先确保自己安全");
    expect(wrapper.text()).toContain("120");
    wrapper.unmount();
  });

  it("submits the collected structured profile to the existing plan API", async () => {
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
      if (url === "/v1/sessions?limit=20") {
        return jsonResponse({ sessions: [] });
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
          "Content-Type": "application/json",
        }),
      }),
    );
    const planCall = fetchMock.mock.calls.find(
      ([input]) => String(input) === "/v1/plans",
    );
    const request = JSON.parse(planCall![1]!.body as string);
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

  it("submits an explicit exact physics restriction from conversation", async () => {
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, _init?: RequestInit) => {
        const url = String(input);
        if (url === "/v1/plans") {
          return jsonResponse({
            runId: "run-physics",
            sessionId: "session-physics",
            status: "accepted",
          });
        }
        if (url === "/v1/sessions?limit=20") {
          return jsonResponse({ sessions: [] });
        }
        throw new Error(`unexpected request: ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    vi.useFakeTimers();
    const wrapper = mount(App);
    const textarea = wrapper.get('textarea[aria-label="输入消息"]');

    await textarea.setValue("我只想看物理学");
    await textarea.trigger("keydown", { key: "Enter" });
    await flushPromises();
    await wrapper.get(".choice-chip.primary").trigger("click");
    const scoreInput = wrapper.get('input[type="number"]');
    await scoreInput.setValue("612");
    await wrapper.get(".inline-card .primary-button").trigger("click");
    const subjectButton = (label: string) =>
      wrapper
        .findAll(".inline-card .choice-chip")
        .find((button) => button.text() === label)!;
    await subjectButton("物理").trigger("click");
    await subjectButton("化学").trigger("click");
    await subjectButton("生物").trigger("click");
    await wrapper.get(".inline-card .primary-button").trigger("click");
    await wrapper.get(".preference-card .primary-button").trigger("click");
    await flushPromises();

    const planCall = fetchMock.mock.calls.find(
      ([input]) => String(input) === "/v1/plans",
    );
    const request = JSON.parse(planCall![1]!.body as string);
    expect(JSON.parse(request.user_text).profile_input).toMatchObject({
      category: "物理类",
      interests: ["物理学"],
      required_major_names: ["物理学"],
    });
    wrapper.unmount();
  });

  it("restores a waiting backend profile confirmation", async () => {
    history.replaceState({}, "", "/?run=run-1");
    vi.stubGlobal("fetch", routeFetch({ run: waitingRunView() }));
    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.text()).toContain("请确认我理解的画像");
    expect(wrapper.text()).toContain("物理类");
    expect(wrapper.text()).toContain("612");
    expect(wrapper.text()).toContain("本科批");
    expect(wrapper.text()).toContain("限定城市");
    expect(wrapper.text()).toContain("长沙");
    expect(wrapper.text()).toContain("已提供");
    expect(wrapper.text()).toContain("系统默认");
    expect(wrapper.text()).toContain("排除省份");
    expect(wrapper.text()).toContain("广东");
    expect(wrapper.text()).not.toContain("preferences.required cities");
    expect(wrapper.text()).not.toContain("PROVIDED");
    expect(wrapper.text()).not.toContain("排除城市");
    wrapper.unmount();
  });

  it("resolves an ambiguous macro region before confirmation", async () => {
    history.replaceState({}, "", "/?run=run-region");
    const fetchMock = routeFetch({ run: regionRunView() });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.text()).toContain("确认“北方”的范围");
    expect(wrapper.text()).toContain("没有唯一省份边界");
    const northChina = wrapper
      .findAll(".region-card .choice-chip")
      .find((button) => button.text() === "华北");
    expect(northChina).toBeTruthy();
    await northChina!.trigger("click");
    expect(wrapper.text()).toContain(
      "北京、天津、河北、山西、内蒙古",
    );

    const confirm = wrapper
      .findAll(".region-card button")
      .find((button) => button.text() === "确认这些省份");
    await confirm!.trigger("click");
    await flushPromises();

    const confirmCall = fetchMock.mock.calls.find(
      ([input]) =>
        String(input) === "/v1/plans/session-1/profile:confirm",
    );
    expect(confirmCall).toBeTruthy();
    const body = JSON.parse(confirmCall![1]!.body as string);
    const profileCommand = JSON.parse(body.profile_json);
    expect(profileCommand.answer).toMatchObject({
      type: "SELECT_PROVINCES",
      clarification_id: "region-clarification-1",
      provinces: ["北京", "天津", "河北", "山西", "内蒙古"],
      selected_preset_ids: ["north_china"],
      region_catalog_version: "region-seven-v1",
      cancel_region_restriction: false,
    });
    wrapper.unmount();
  });

  it("opens the immutable report from a completed formal result", async () => {
    history.replaceState({}, "", "/?run=run-formal");
    vi.stubGlobal("fetch", routeFetch({ run: formalRunView() }));
    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.text()).toContain("你的冲稳保方案已生成");
    expect(wrapper.text()).not.toContain("引用覆盖率");
    expect(wrapper.text()).toContain("中南大学");
    expect(wrapper.text()).toContain("2024");
    expect(wrapper.text()).toContain("最低分/最低位次 624/6798");
    expect(wrapper.text()).toContain("2023");
    expect(wrapper.text()).toContain("暂无数据");
    expect(wrapper.text()).toContain("仅 1 年有效历史，样本有限");
    expect(wrapper.findAll(".preview-tier")).toHaveLength(3);
    expect(wrapper.text()).toContain("冲刺");
    expect(wrapper.text()).toContain("稳妥");
    expect(wrapper.text()).toContain("保底");
    expect(
      wrapper.findAll(".preview-tier .probability").map((item) => item.text()),
    ).toEqual(["34%", "55%", "86%"]);
    const reportLink = wrapper.get('.result-footer a[href^="/reports/"]');
    expect(reportLink.attributes("href")).toBe(
      "/reports/run-formal",
    );
    expect(reportLink.attributes("target")).toBe("_blank");
    wrapper.unmount();
  });

  it("explains a below-control-line terminal reason", async () => {
    history.replaceState({}, "", "/?run=run-low-score");
    vi.stubGlobal(
      "fetch",
      routeFetch({
        run: {
          runId: "run-low-score",
          sessionId: "session-low-score",
          taskKind: "GENERATE_PLAN",
          state: "FAILED",
          stage: "PROFILE_CONFIRMED",
          terminalReason: "SCORE_BELOW_CONTROL_LINE",
          stateVersion: 9,
          releaseId: "release-2025",
        },
      }),
    );
    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.text()).toContain("分数低于湖南 2025 本科批控制线");
    expect(wrapper.text()).not.toContain("SCORE_BELOW_CONTROL_LINE");
    wrapper.unmount();
  });

  it("explains an unsatisfied target-allocation revision", async () => {
    history.replaceState({}, "", "/?run=run-allocation-failed");
    vi.stubGlobal(
      "fetch",
      routeFetch({
        run: {
          runId: "run-allocation-failed",
          sessionId: "session-allocation-failed",
          taskKind: "REVISE_PLAN",
          state: "FAILED",
          stage: "PROFILE_CONFIRMED",
          terminalReason: "TARGET_ALLOCATION_UNSATISFIED",
          stateVersion: 11,
          releaseId: "release-2025",
        },
      }),
    );
    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.text()).toContain("没有更多目标档候选");
    expect(wrapper.text()).toContain("已保留原报告");
    expect(wrapper.text()).not.toContain("TARGET_ALLOCATION_UNSATISFIED");
    expect(wrapper.find(".error-card").exists()).toBe(false);
    expect(wrapper.get(".chat-message.assistant .message-status").text()).toContain(
      "没有更多目标档候选",
    );
    expect(wrapper.text()).not.toContain("开始新对话");
    wrapper.unmount();
  });

  it("keeps the report open and sends a post-report filter message", async () => {
    history.replaceState({}, "", "/?run=run-formal");
    const fetchMock = routeFetch({
      run: formalRunView(),
      conversation: {
        sessionId: "session-1",
        runId: "run-revision",
        taskKind: "REVISE_PLAN",
        status: "accepted",
        assistantMessage: "已收到筛选条件，正在重新计算方案。",
      },
    });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.text()).toContain("你的冲稳保方案已生成");
    const textarea = wrapper.get('textarea[aria-label="输入消息"]');
    await textarea.setValue("不要武汉，只看计算机");
    await textarea.trigger("keydown", { key: "Enter" });
    await flushPromises();

    const conversationCall = fetchMock.mock.calls.find(
      ([input]) => String(input) === "/v1/sessions/session-1/messages",
    );
    expect(conversationCall).toBeTruthy();
    const body = JSON.parse(conversationCall![1]!.body as string);
    expect(body).toMatchObject({
      parent_result_version: "result-formal",
      message_text: "不要武汉，只看计算机",
      profile_input: {
        required_provinces: [],
        excluded_provinces: ["广东"],
      },
    });
    expect(wrapper.text()).toContain("你的冲稳保方案已生成");
    const threadHtml = wrapper.get(".thread").html();
    expect(threadHtml.indexOf("你的冲稳保方案已生成")).toBeLessThan(
      threadHtml.indexOf("不要武汉，只看计算机"),
    );
    wrapper.unmount();
  });

  it("asks for exact provinces when a post-report message uses a macro region", async () => {
    history.replaceState({}, "", "/?run=run-formal");
    const region = regionRunView().currentInterrupt.regionClarification;
    const fetchMock = routeFetch({
      run: formalRunView(),
      conversation: {
        sessionId: "session-1",
        status: "clarification_required",
        assistantMessage: "“北方”的覆盖范围并不唯一。",
        regionClarification: region,
      },
    });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(App);
    await flushPromises();

    const textarea = wrapper.get('textarea[aria-label="输入消息"]');
    await textarea.setValue("我不想去北方读大学");
    await textarea.trigger("keydown", { key: "Enter" });
    await flushPromises();

    expect(wrapper.text()).toContain("你的冲稳保方案已生成");
    expect(wrapper.text()).toContain("确认“北方”的范围");
    expect(wrapper.text()).toContain("华北");
    expect(wrapper.text()).toContain("先不管这个地域限制");
    wrapper.unmount();
  });

  it("restores empty preferred cities before sending a report revision", async () => {
    history.replaceState({}, "", "/?run=run-formal");
    const profileFields: Record<string, unknown> = structuredClone(
      waitingRunView().currentInterrupt.fields,
    );
    profileFields["preferences.risk_appetite"] = {
      state: "PROVIDED",
      source: "USER",
      stringValue: "aggressive",
    };
    profileFields["preferences.preferred_cities"] = {
      state: "PROVIDED",
      source: "USER",
      stringList: { values: [] },
    };
    profileFields["preferences.excluded_provinces"] = {
      state: "PROVIDED",
      source: "USER",
      stringList: { values: ["湖南"] },
    };
    const fetchMock = routeFetch({
      run: formalRunView(),
      profileFields,
      conversation: {
        sessionId: "session-1",
        runId: "run-more-rush",
        taskKind: "REVISE_PLAN",
        status: "accepted",
        assistantMessage: "已收到增加冲档请求。",
      },
    });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(App);
    await flushPromises();

    const textarea = wrapper.get('textarea[aria-label="输入消息"]');
    await textarea.setValue("冲的比例太少了，增加多几个");
    await textarea.trigger("keydown", { key: "Enter" });
    await flushPromises();

    const conversationCall = fetchMock.mock.calls.find(
      ([input]) => String(input) === "/v1/sessions/session-1/messages",
    );
    expect(conversationCall).toBeTruthy();
    const body = JSON.parse(conversationCall![1]!.body as string);
    expect(body.profile_input).toMatchObject({
      risk_appetite: "aggressive",
      preferred_cities: [],
      excluded_provinces: ["湖南"],
    });
    wrapper.unmount();
  });
});

function routeFetch({
  run,
  conversation,
  profileFields,
}: {
  run: Record<string, unknown>;
  conversation?: Record<string, unknown>;
  profileFields?: Record<string, unknown>;
}) {
  return vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input);
    if (url === "/v1/sessions?limit=20") {
      return jsonResponse({
        sessions: [
          {
            sessionId: String(run.sessionId || "session-1"),
            title: "物理类 612 分志愿方案",
            latestRunId: String(run.runId),
            latestState: run.state,
            runCount: 1,
          },
        ],
      });
    }
    if (url === `/v1/runs/${String(run.runId)}`) {
      return jsonResponse(run);
    }
    if (url === `/v1/sessions/${String(run.sessionId || "session-1")}`) {
      return jsonResponse({
        summary: {
          sessionId: String(run.sessionId || "session-1"),
          title: "物理类 612 分志愿方案",
          latestRunId: run.runId,
          latestState: run.state,
          runCount: run.formalResult ? 1 : 0,
        },
        messages: [
          {
            messageId: "message-user",
            role: "USER",
            kind: "TEXT",
            content: "我物理类 612 分，选的是物化生",
          },
          {
            messageId: "message-assistant",
            role: "ASSISTANT",
            kind: "TEXT",
            content: "画像已整理完成。",
          },
          ...(run.formalResult
            ? [
                {
                  messageId: "message-result",
                  runId: String(run.runId),
                  role: "ASSISTANT",
                  kind: "RESULT",
                  content: "正式推荐方案已生成。",
                },
              ]
            : []),
        ],
        runs: [
          {
            runId: String(run.runId),
            taskKind: String(run.taskKind || "GENERATE_PLAN"),
            state: run.state,
            stage: run.stage,
            resultVersion: run.formalResult
              ? "result-formal"
              : undefined,
            resultKind: run.formalResult ? "V1_FORMAL" : undefined,
          },
        ],
        profileFields:
          profileFields ?? waitingRunView().currentInterrupt.fields,
      });
    }
    if (
      conversation &&
      url === `/v1/sessions/${String(run.sessionId || "session-1")}/messages`
    ) {
      return jsonResponse(conversation);
    }
    if (
      url === `/v1/plans/${String(run.sessionId || "session-1")}/profile:confirm`
    ) {
      return jsonResponse({
        sessionId: String(run.sessionId || "session-1"),
        runId: String(run.runId),
        status: "accepted",
      });
    }
    throw new Error(`unexpected request: ${url}`);
  });
}

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
        "preferences.risk_appetite": {
          state: "PROVIDED",
          source: "USER",
          stringValue: "balanced",
        },
        "preferences.required_cities": {
          state: "PROVIDED",
          source: "USER",
          stringList: { values: ["长沙"] },
        },
        "preferences.excluded_cities": {
          state: "PROVIDED",
          source: "USER",
          stringList: { values: [] },
        },
        "preferences.excluded_provinces": {
          state: "PROVIDED",
          source: "USER",
          stringList: { values: ["广东"] },
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
            history: [
              {
                year: 2022,
                minScore: 630,
                minRank: 6100,
                totalCount: 313685,
                mappingMethod: "EXACT_CODE",
                mappingConfidence: 1,
              },
              { year: 2023 },
              {
                year: 2024,
                minScore: 624,
                minRank: 6798,
                totalCount: 313685,
                mappingMethod: "MAJOR_NAME_JACCARD",
                mappingConfidence: 0.75,
              },
            ],
          },
          {
            sectionKey: "candidate:1002|01",
            title: "2. 湖南大学 01专业组",
            body:
              "院校：湖南大学 01专业组。专业：软件工程（080902）。录取概率约 55%，策略分档：稳，招生计划数：20，样本年份为 2022、2023、2024。",
            citations: [],
            history: [
              { year: 2022 },
              { year: 2023 },
              {
                year: 2024,
                minScore: 592,
                minRank: 17398,
                totalCount: 313685,
                mappingMethod: "MAJOR_NAME_JACCARD",
                mappingConfidence: 0.7273,
              },
            ],
          },
          {
            sectionKey: "candidate:1003|01",
            title: "3. 湘潭大学 01专业组",
            body:
              "院校：湘潭大学 01专业组。专业：计算机科学与技术（080901）。录取概率约 86%，策略分档：保，招生计划数：30，样本年份为 2022、2023、2024。",
            citations: [],
          },
        ],
      },
    },
  };
}

function regionRunView() {
  return {
    ...waitingRunView(),
    runId: "run-region",
    state: "WAITING_HITL",
    stage: "INTAKE_ASK",
    stateVersion: 4,
    currentInterrupt: {
      interruptId: "region-interrupt-1",
      kind: "INTAKE_ASK",
      state: "OPEN",
      profileVersion: "profile-1",
      fields: waitingRunView().currentInterrupt.fields,
      regionClarification: {
        clarificationId: "region-clarification-1",
        clarificationKind: "SELECT_PROVINCES",
        reasonCode: "MACRO_REGION_SCOPE_UNRESOLVED",
        term: "北方",
        operation: "exclude",
        catalogVersion: "region-seven-v1",
        provinces: [
          "北京",
          "天津",
          "河北",
          "山西",
          "内蒙古",
          "辽宁",
          "吉林",
          "黑龙江",
          "广东",
        ],
        presets: [
          {
            id: "north_china",
            name: "华北",
            provinces: ["北京", "天津", "河北", "山西", "内蒙古"],
          },
          {
            id: "northeast",
            name: "东北",
            provinces: ["辽宁", "吉林", "黑龙江"],
          },
          {
            id: "south_china",
            name: "华南",
            provinces: ["广东"],
          },
        ],
      },
    },
  };
}
