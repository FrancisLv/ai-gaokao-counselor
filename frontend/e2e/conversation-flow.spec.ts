import { expect, test } from "@playwright/test";

type DraftSavePayload = {
  groups?: Array<{
    university_code: string;
    major_group: string;
    major_codes: string[];
  }>;
};

test("collects a profile, confirms it and opens the formal report", async ({
  page,
}) => {
  let confirmed = false;
  await page.route("**/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/v1/account" && request.method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          account: {
            accountId: "account-e2e",
            username: "e2e",
            nickname: "E2E",
          },
        }),
      });
      return;
    }
    if (url.pathname === "/v1/sessions" && request.method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ sessions: [] }),
      });
      return;
    }
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
      url.pathname === "/v1/sessions/session-conversation" &&
      request.method() === "GET"
    ) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          summary: {
            sessionId: "session-conversation",
            title: "物理类 612 分志愿方案",
            latestRunId: "run-conversation",
            latestState: confirmed ? "SUCCEEDED" : "WAITING_HITL",
            runCount: 1,
          },
          messages: [
            {
              messageId: "message-user",
              role: "USER",
              kind: "TEXT",
              content: "我物理类 612 分，选的是物化生",
            },
          ],
          runs: [
            {
              runId: "run-conversation",
              taskKind: "GENERATE_PLAN",
              state: confirmed ? "SUCCEEDED" : "WAITING_HITL",
              stage: confirmed ? "FORMAL_READY" : "PROFILE_CONFIRM",
              resultVersion: confirmed ? "result-formal" : undefined,
              resultKind: confirmed ? "V1_FORMAL" : undefined,
            },
          ],
          profileFields: waitingRunView().currentInterrupt.fields,
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
    "/reports/run-conversation",
  );
});

test("renders the standalone report and navigates to a school detail", async ({
  page,
}) => {
  let savedDraftPayload: DraftSavePayload | null = null;
  await page.addInitScript(() => {
    const state = window as typeof window & { __printCalls: number };
    state.__printCalls = 0;
    window.print = () => {
      state.__printCalls += 1;
    };
  });
  await page.route("**/v1/runs/run-formal", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(formalRunView()),
    });
  });
  await page.route(
    "**/v1/results/result-formal/wishlist-draft",
    async (route) => {
      if (route.request().method() === "GET") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ draft: null }),
        });
        return;
      }
      savedDraftPayload = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          draft: {
            draftVersionId: "draft-1",
            resultVersion: "result-formal",
            runId: "run-formal",
            releaseId: "release-2025",
            draftVersion: 1,
            createdAt: "2026-11-22T00:00:00Z",
            groups: savedDraftPayload?.groups?.map((group) => ({
              universityCode: group.university_code,
              majorGroup: group.major_group,
              majorCodes: group.major_codes,
            })),
            risk: {
              groupCount: 1,
              rushCount: 1,
              stableCount: 0,
              safeTierCount: 0,
              safeGuardCount: 0,
              safeGuardTarget: 9,
              safeGuardOk: false,
              slipRate: 0.66,
              expectedSatisfaction: 0.8,
            },
          },
        }),
      });
    },
  );

  await page.goto("/reports/run-formal");
  await expect(
    page.getByRole("heading", { name: "你的冲稳保志愿报告" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "中南大学 03专业组" }),
  ).toBeVisible();
  const [reportArticleBox, reportTocBox] = await Promise.all([
    page.locator(".report-article").boundingBox(),
    page.locator(".report-toc").boundingBox(),
  ]);
  expect(reportArticleBox).not.toBeNull();
  expect(reportTocBox).not.toBeNull();
  expect(reportArticleBox?.width ?? 0).toBeGreaterThan(1_000);
  expect(reportTocBox?.x ?? Number.POSITIVE_INFINITY).toBeLessThan(24);
  expect(
    await page.locator(".major-table-wrap").first().evaluate(
      (table) => table.scrollWidth <= table.clientWidth + 1,
    ),
  ).toBe(true);
  await expect(page.locator(".major-table").first()).not.toContainText(
    /\d+\s*年历史/,
  );
  const actionButtonTops = await page
    .locator(".major-table tbody tr")
    .first()
    .locator(".major-actions button")
    .evaluateAll((buttons) =>
      buttons.map((button) => Math.round(button.getBoundingClientRect().top)),
    );
  expect(Math.max(...actionButtonTops) - Math.min(...actionButtonTops)).toBeLessThan(
    2,
  );
  await expect(page.locator(".report-summary")).toContainText("物理类排位前 12%");
  await expect(page.locator(".report-lede")).toContainText("排位前 12%");
  const reportTopbar = page.locator(".report-summary-topbar");
  await expect(reportTopbar).toContainText("湖南 2025 · 物理类 · 本科批");
  await expect(reportTopbar).toContainText("结果版本 result-f");
  await expect(reportTopbar).toContainText("报告有效");
  await expect(
    reportTopbar.getByRole("link", { name: "我的志愿表" }),
  ).toBeVisible();
  await expect(
    reportTopbar.getByRole("link", { name: "返回对话" }),
  ).toBeVisible();
  await expect(reportTopbar).not.toContainText("result-formal");
  await expect(page.locator(".report-summary")).not.toContainText("历史百分位");
  await expect(page.locator(".report-lede")).not.toContainText("0.1200");
  await expect(page.getByText("引用覆盖率").first()).toBeVisible();
  await expect(
    page.getByText("专业：计算机科学（080901）。招生计划数：18 人。"),
  ).toBeVisible();
  await expect(
    page.locator(".school-row .school-probability").first(),
  ).toContainText("录取概率");
  await expect(
    page.locator(".school-row .school-probability .probability").first(),
  ).toHaveCSS("text-align", "center");
  await expect(
    page.locator(".school-row .school-probability .probability").first(),
  ).toHaveText(/^\d+%$/);
  await expect(page.locator(".school-row .history-note")).toHaveCount(0);
  await expect(page.locator(".report-history-note")).toHaveCount(1);
  await expect(
    page.getByRole("columnheader", { name: "同科类百分位" }),
  ).toBeVisible();
  await expect(
    page.getByRole("columnheader", { name: "相对当前位次" }),
  ).toBeVisible();
  await expect(page.getByText("2024年计划 23 人")).toBeVisible();
  await expect(page.getByText("计划数减少 5 人")).toBeVisible();
  await expect(
    page.locator(".wishlist-tiers").getByText("2024", { exact: true }).first(),
  ).toBeVisible();
  await expect(
    page.locator(".wishlist-tiers").getByText("37600").first(),
  ).toBeVisible();
  await expect(
    page.locator(".wishlist-tiers").getByText("临界").first(),
  ).toBeVisible();
  await expect(page.getByText("数据依据")).toHaveCount(0);
  await expect(page.getByText("湖南 2025 招生计划")).toHaveCount(0);
  await expect(page.getByText("跨年匹配依据")).toHaveCount(0);
  await expect(page.getByText("资格核验")).toHaveCount(0);
  await expect(page.getByText("资格与数据")).toHaveCount(0);
  await expect(page.getByText("1001:03=PASS")).toHaveCount(0);
  await expect(page.getByText("样本年份为", { exact: false })).toHaveCount(0);
  await expect(page.locator(".report-aside")).toHaveCount(0);
  await expect(page.getByText("报告身份")).toHaveCount(0);
  await expect(page.getByText("验证状态")).toHaveCount(0);
  await expect(page.getByText("重要说明")).toHaveCount(0);
  await expect(page.locator("#risk")).toContainText(
    "本方案仅供志愿参考，不构成录取保证。",
  );
  const exportButton = page.getByRole("button", {
    name: "保存并导出 PDF",
  });
  await expect(exportButton).toBeEnabled();
  await page.emulateMedia({ media: "print" });
  await page.evaluate(() => window.dispatchEvent(new Event("beforeprint")));
  await expect(page.locator("body")).toHaveClass(/print-blocked/);
  await expect(page.locator(".print-block-message")).toBeVisible();
  await page.evaluate(() => window.dispatchEvent(new Event("afterprint")));
  await page.emulateMedia({ media: "screen" });

  await exportButton.click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as typeof window & { __printCalls: number }).__printCalls,
      ),
    )
    .toBe(1);
  expect(savedDraftPayload).toMatchObject({
    result_version: "result-formal",
    expected_version: 0,
    groups: [
      {
        university_code: "1001",
        major_group: "03",
        major_codes: [
          "080901",
          "080902",
          "080903",
          "080904",
          "080905",
          "080906",
        ],
      },
    ],
  });
  await expect(
    page.getByRole("button", { name: "导出 PDF" }),
  ).toBeVisible();
  await page.emulateMedia({ media: "print" });
  await page.evaluate(() => window.dispatchEvent(new Event("beforeprint")));
  await expect(page.locator(".draft-toolbar")).toBeHidden();
  await expect(page.locator(".print-footer")).toBeVisible();
  await expect(page.locator("body")).not.toHaveClass(/print-blocked/);
  const printedProbability = page
    .locator(".print-school-probability")
    .first();
  await expect(printedProbability).toBeVisible();
  await expect(page.locator(".school-probability").first()).toBeHidden();
  await expect(page.locator(".print-wishlist-item").first()).toHaveCSS(
    "break-inside",
    "avoid",
  );
  await expect(page.locator(".history-table").first()).toHaveCSS(
    "break-inside",
    "avoid",
  );
  const [printedTitleBox, printedProbabilityBox] = await Promise.all([
    page.locator(".print-wishlist-item h3").first().boundingBox(),
    printedProbability.boundingBox(),
  ]);
  expect(printedTitleBox).not.toBeNull();
  expect(printedProbabilityBox).not.toBeNull();
  expect(
    Math.abs(
      (printedTitleBox?.y ?? 0) +
        (printedTitleBox?.height ?? 0) / 2 -
        ((printedProbabilityBox?.y ?? 0) +
          (printedProbabilityBox?.height ?? 0) / 2),
    ),
  ).toBeLessThan(12);
  await expect(
    page.locator(".print-selected-wishlist .print-major-list"),
  ).toBeVisible();
  await expect(
    page.locator(".print-selected-wishlist .print-major"),
  ).toHaveCount(6);
  const firstMajorCode = page
    .locator(".print-selected-wishlist .print-major-code")
    .first();
  await expect(firstMajorCode).toBeVisible();
  await expect(firstMajorCode).toContainText("080901");
  await expect(firstMajorCode).toHaveCSS("font-weight", "850");
  const [printedMajorNameBox, printedMajorCodeBox] = await Promise.all([
    page
      .locator(".print-selected-wishlist .print-major-name-line > strong")
      .first()
      .boundingBox(),
    firstMajorCode.boundingBox(),
  ]);
  expect(printedMajorNameBox).not.toBeNull();
  expect(printedMajorCodeBox).not.toBeNull();
  expect(
    Math.abs(
      (printedMajorNameBox?.y ?? 0) +
        (printedMajorNameBox?.height ?? 0) / 2 -
        ((printedMajorCodeBox?.y ?? 0) +
          (printedMajorCodeBox?.height ?? 0) / 2),
    ),
  ).toBeLessThan(8);
  await expect(
    page.locator(".major-selection .major-table-wrap").first(),
  ).toBeHidden();
  expect(
    await page.locator(".print-selected-wishlist .print-major-list").evaluate(
      (list) => list.scrollWidth <= list.clientWidth + 1,
    ),
  ).toBe(true);
  await expect(
    page.locator(".print-selected-wishlist .print-major-list"),
  ).not.toContainText(/\d+\s*年历史/);
  const pdf = await page.pdf({ format: "A4", printBackground: true });
  expect(pdf.subarray(0, 4).toString()).toBe("%PDF");
  await page.evaluate(() => window.dispatchEvent(new Event("afterprint")));
  await page.emulateMedia({ media: "screen" });

  await page.getByRole("link", { name: /中南大学/ }).click();
  await expect(page).toHaveURL(/school\.html\?run=run-formal/);
  await expect(
    page.getByRole("heading", { name: "中南大学 03专业组" }),
  ).toBeVisible();
  await expect(page.getByText("数据出处")).toHaveCount(0);
  await expect(page.getByText("湖南 2025 招生计划")).toHaveCount(0);
  await expect(page.getByText("跨年匹配依据")).toHaveCount(0);
  await expect(
    page.getByText("专业：计算机科学（080901）。招生计划数：18 人。"),
  ).toBeVisible();
});

test("moves a professional group out, saves a subset, and can add it back", async ({
  page,
}) => {
  let savedDraftPayload: DraftSavePayload | null = null;
  await page.route("**/v1/runs/run-groups", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(twoGroupFormalRunView()),
    });
  });
  await page.route(
    "**/v1/results/result-formal/wishlist-draft",
    async (route) => {
      if (route.request().method() === "GET") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ draft: null }),
        });
        return;
      }
      savedDraftPayload = route.request().postDataJSON();
      const groupCount = savedDraftPayload?.groups?.length ?? 0;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          draft: {
            draftVersionId: "draft-groups-1",
            resultVersion: "result-formal",
            runId: "run-groups",
            releaseId: "release-2025",
            draftVersion: 1,
            createdAt: "2026-11-23T00:00:00Z",
            groups: savedDraftPayload?.groups?.map((group) => ({
              universityCode: group.university_code,
              majorGroup: group.major_group,
              majorCodes: group.major_codes,
            })),
            risk: {
              groupCount,
              rushCount: groupCount,
              stableCount: 0,
              safeTierCount: 0,
              safeGuardCount: 0,
              safeGuardTarget: 9,
              safeGuardOk: false,
              slipRate: 0.1,
              expectedSatisfaction: 0.7,
            },
          },
        }),
      });
    },
  );

  await page.goto("/reports/run-groups");
  await expect(page.locator(".group-actions-toolbar").first()).toBeVisible();
  const selectedCards = page.locator(
    "#tier-冲 .wishlist-group:not(.wishlist-group-excluded)",
  );
  await expect(selectedCards).toHaveCount(2);
  const [groupTitleBox, groupActionsBox] = await Promise.all([
    page
      .locator("#tier-冲 [data-group-key='1001|03'] .school-title-link h3")
      .boundingBox(),
    page
      .locator("#tier-冲 [data-group-key='1001|03'] .wishlist-group-actions")
      .boundingBox(),
  ]);
  expect(groupTitleBox).not.toBeNull();
  expect(groupActionsBox).not.toBeNull();
  expect(
    Math.abs(
      (groupTitleBox?.y ?? 0) +
        (groupTitleBox?.height ?? 0) / 2 -
        ((groupActionsBox?.y ?? 0) + (groupActionsBox?.height ?? 0) / 2),
    ),
  ).toBeLessThan(8);
  await page
    .locator(
      "#tier-冲 [data-group-key='1001|03'] .wishlist-group-actions",
    )
    .getByRole("button", { name: "下移" })
    .click();
  await expect(
    page.locator(
      "#tier-冲 .wishlist-group:not(.wishlist-group-excluded) .school-title-link h3",
    ),
  ).toHaveText(["第二大学 04专业组", "中南大学 03专业组"]);
  await page
    .locator("#tier-冲 [data-group-key='1001|03'] .wishlist-group-actions")
    .getByRole("button", { name: "移除" })
    .click();
  await expect(selectedCards).toHaveCount(1);
  await expect(page.locator(".report-summary")).toContainText("1 / 2");
  await expect(page.locator(".draft-summary")).not.toContainText(
    "重新加入会恢复本次编辑中的专业选择",
  );
  await expect(page.locator("#excluded-groups")).toContainText(
    "未纳入志愿表的推荐专业组",
  );
  await expect(page.locator(".wishlist-group-excluded")).toContainText(
    "已移出志愿表",
  );
  await page.getByRole("button", { name: "保存草稿" }).click();
  await expect
    .poll(() => savedDraftPayload?.groups?.length ?? 0)
    .toBe(1);
  expect(savedDraftPayload?.groups?.[0]).toMatchObject({
    university_code: "1002",
    major_group: "04",
  });
  await expect(page.locator(".draft-risk-card").first()).toContainText(
    "保底不足",
  );
  await page
    .locator(".wishlist-group-excluded")
    .getByRole("button", { name: "重新加入" })
    .first()
    .click();
  await expect(
    page.locator(
      "#tier-冲 .wishlist-group:not(.wishlist-group-excluded) .school-title-link h3",
    ),
  ).toHaveText(["第二大学 04专业组", "中南大学 03专业组"]);
  await expect(page.locator("#excluded-groups")).toHaveCount(0);
});

test("explains when a report run is missing or belongs to another account", async ({
  page,
}) => {
  await page.route("**/v1/runs/missing-run", async (route) => {
    await route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({
        code: 404,
        reason: "RUN_NOT_FOUND",
        message: "RUN_NOT_FOUND",
      }),
    });
  });

  await page.goto("/report.html?run=missing-run");

  await expect(
    page.getByRole("heading", { name: "无法打开推荐报告" }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "该报告不存在，或不属于当前登录账号。链接可能来自认证上线前的匿名会话。",
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "打开我的志愿表" }),
  ).toHaveAttribute("href", "/wishlists.html");
  await expect(
    page.getByRole("link", { name: "返回对话" }),
  ).toHaveAttribute("href", "/");
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
              "院校：中南大学 03专业组。专业：计算机科学（080901）。录取概率约 34%，策略分档：冲，招生计划数：18，样本年份为 2022、2023、2024。",
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
            universityCode: "1001",
            universityName: "中南大学",
            majorGroup: "03",
            planCount: 18,
            previousPlanCount: 23,
            planAdjustFactor: 0.8913043478,
            probability: 0.34,
            strategyTier: "冲",
            finalRank: 1,
            majors: [
              "计算机科学",
              "软件工程",
              "网络工程",
              "信息安全",
              "物联网工程",
              "数字媒体技术",
            ].map((majorName, index) => ({
              majorCode: `08090${index + 1}`,
              majorName,
              subjectReq: "首选物理，再选化学",
              duration: "四年",
              minScore: 624 - index * 3,
              minRank: 6798 + index * 730,
              referenceYear: 2024,
              admissionProbability: 0.34 - index * 0.03,
              probabilityDataYears: 3,
              planCount: 12 + index,
              tuition: 6500 + index * 500,
              recommended: true,
              recommendationRank: index + 1,
              selectable: true,
              unavailableReason: "",
            })),
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
                minRank: 37600,
                totalCount: 313685,
                mappingMethod: "MAJOR_NAME_JACCARD",
                mappingConfidence: 0.75,
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

function twoGroupFormalRunView() {
  const view = formalRunView("run-groups");
  const first = JSON.parse(
    JSON.stringify(view.formalResult.report.sections[1]),
  );
  first.sectionKey = "candidate:1002|04";
  first.title = "2. 第二大学 04专业组";
  first.body =
    "院校：第二大学 04专业组。专业：数据科学（090901）。录取概率约 39%，策略分档：冲，招生计划数：20，样本年份为 2022、2023、2024。";
  first.universityCode = "1002";
  first.universityName = "第二大学";
  first.majorGroup = "04";
  first.probability = 0.39;
  first.strategyTier = "冲";
  first.finalRank = 2;
  first.history = [];
  first.majors = [
    {
      majorCode: "090901",
      majorName: "数据科学",
      subjectReq: "首选物理，再选不限",
      duration: "四年",
      minScore: 612,
      minRank: 15000,
      referenceYear: 2024,
      admissionProbability: 0.71,
      probabilityDataYears: 3,
      planCount: 20,
      tuition: 6000,
      recommended: true,
      recommendationRank: 1,
      selectable: true,
      unavailableReason: "",
    },
  ];
  view.formalResult.report.sections.splice(2, 0, first);
  view.formalResult.report.sections[0].body =
    "湖南物理类考生位次 8640 名，百分位 0.1200，共 2 个院校专业组。";
  view.formalResult.report.claimIds.push("claim:candidate:1002|04");
  return view;
}
