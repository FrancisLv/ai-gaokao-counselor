import { expect, test } from "@playwright/test";

test("collects a profile, confirms it and opens the formal report", async ({
  page,
}) => {
  let confirmed = false;
  await page.route("**/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/v1/plans" && request.method() === "POST") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          runId: "run-conversation",
          sessionId: "session-conversation",
          status: "accepted",
        }),
      });
      return;
    }
    if (
      url.pathname === "/v1/plans/session-conversation/profile:confirm" &&
      request.method() === "POST"
    ) {
      confirmed = true;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          runId: "run-conversation",
          sessionId: "session-conversation",
          status: "accepted",
        }),
      });
      return;
    }
    if (
      url.pathname === "/v1/runs/run-conversation" &&
      request.method() === "GET"
    ) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          confirmed ? formalRunView("run-conversation") : waitingRunView(),
        ),
      });
      return;
    }
    throw new Error(`unexpected request: ${request.method()} ${url.pathname}`);
  });

  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "把复杂的志愿填报，聊明白。" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: /帮我生成志愿方案/ })
    .click();
  await expect(page.getByText("城市偏好")).toBeVisible();
  await page.getByRole("button", { name: "开始生成方案" }).click();

  await expect(page.getByText("请确认我理解的画像")).toBeVisible({
    timeout: 3_000,
  });
  await page.getByRole("button", { name: "确认画像并继续" }).click();

  await expect(page.getByText("你的冲稳保方案已生成")).toBeVisible({
    timeout: 3_000,
  });
  const reportLink = page.getByRole("link", {
    name: "查看完整推荐报告 ↗",
  });
  await expect(reportLink).toHaveAttribute(
    "href",
    "report.html?run=run-conversation",
  );
});

test("renders the standalone report and navigates to a school detail", async ({
  page,
}) => {
  await page.route("**/v1/runs/run-formal", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(formalRunView()),
    });
  });

  await page.goto("/report.html?run=run-formal");
  await expect(
    page.getByRole("heading", { name: "你的冲稳保志愿报告" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "中南大学 03专业组" }),
  ).toBeVisible();

  await page.getByRole("link", { name: /中南大学/ }).click();
  await expect(page).toHaveURL(/school\.html\?run=run-formal/);
  await expect(
    page.getByRole("heading", { name: "中南大学 03专业组" }),
  ).toBeVisible();
  await expect(page.getByText("湖南 2025 招生计划")).toBeVisible();
});

function waitingRunView() {
  return {
    runId: "run-conversation",
    sessionId: "session-conversation",
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

function formalRunView(runId = "run-formal") {
  return {
    ...waitingRunView(),
    runId,
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
      disclaimer: "本方案仅供志愿参考，不构成录取保证。",
      riskNotice: "录取概率为历史估计，存在滑档风险。",
      noAdmissionGuarantee: "不保证录取。",
      report: {
        reportVersion: "m5b-report-v1",
        generationMode: "DETERMINISTIC",
        claimIds: ["claim:rank", "claim:candidate:1001|03"],
        unverifiedFacts: [],
        sections: [
          {
            sectionKey: "plan",
            title: "方案概况",
            body: "湖南物理类考生位次 8640 名，百分位 0.1200，共 1 个院校专业组。",
            citations: [],
          },
          {
            sectionKey: "candidate:1001|03",
            title: "1. 中南大学 03专业组",
            body:
              "院校：中南大学 03专业组。专业：计算机科学（080901）。录取概率约 34%，策略分档：冲，招生计划数：12，样本年份为 2022、2023、2024。",
            citations: [
              {
                evidenceId: "derived:candidate:1001|03:probability",
                evidenceType: "DERIVED",
                contentSha256: "a".repeat(64),
                label: "湖南 2025 招生计划",
                sourceUri: "https://example.invalid/plan",
                sectionPath: "招生计划 > 专业组 03",
                releaseId: "release-2025",
              },
            ],
          },
          {
            sectionKey: "qualification",
            title: "资格核验",
            body: "1001:03=PASS",
            citations: [],
          },
          {
            sectionKey: "risk",
            title: "风险提示",
            body: "概率为历史估计，不构成录取保证。",
            citations: [],
          },
          {
            sectionKey: "data_gaps",
            title: "待核验数据",
            body: "无。",
            citations: [],
          },
        ],
      },
    },
  };
}
