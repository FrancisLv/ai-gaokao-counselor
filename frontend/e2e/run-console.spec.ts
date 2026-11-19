import { expect, test } from "@playwright/test";

const waitingRun = {
  runId: "run-e2e",
  sessionId: "session-e2e",
  taskKind: "GENERATE_PLAN",
  state: "WAITING_HITL",
  stage: "PROFILE_CONFIRM",
  stateVersion: 3,
  releaseId: "rel_legacy_v1",
  expiresAt: "2026-11-19T12:30:00Z",
  currentInterrupt: {
    interruptId: "interrupt-e2e",
    kind: "PROFILE_CONFIRM",
    state: "OPEN",
    profileVersion: "profile-e2e",
    fields: {
      province: {
        state: "PROVIDED",
        source: "USER_FORM",
        stringValue: "湖南",
      },
      batch: {
        state: "DEFAULT",
        source: "SYSTEM_DEFAULT",
        stringValue: "本科批",
      },
    },
    findings: [],
  },
};

const succeededRun = {
  ...waitingRun,
  state: "SUCCEEDED",
  stage: "PREVIEW_READY",
  stateVersion: 5,
  currentInterrupt: undefined,
  resultPreview: {
    resultVersion: "result-e2e",
    runId: "run-e2e",
    releaseId: "rel_legacy_v1",
    confirmedProfileVersion: "profile-e2e",
    rank: 15860,
    percentile: 0.1234,
    rushCandidates: [],
    stableCandidates: [
      {
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
    ],
    safeCandidates: [],
    auditBasis: {
      userRank: "15860",
      method: "weighted_hit",
      province: "湖南",
      category: "物理类",
    },
    keptCandidates: [],
    rejectedCandidates: [],
    wishlist: { candidates: [], report: {} },
    verificationSummary: [],
    unverifiedConditions: [
      {
        code: "FULL_ELIGIBILITY_RULES",
        message: "完整当年资格规则未核验",
      },
    ],
    riskNotice: "仅供内部受控验收。",
    disclaimer: "不得作为正式志愿方案。",
    internalControlledOnly: true,
    formalRecommendation: false,
    resultKind: "ALGORITHM_PREVIEW",
  },
};

test("restores, confirms and renders the controlled preview", async ({ page }) => {
  let confirmed = false;
  await page.route("**/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (url.pathname === "/v1/runs/run-e2e" && request.method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(confirmed ? succeededRun : waitingRun),
      });
      return;
    }
    if (
      url.pathname === "/v1/plans/session-e2e/profile:confirm" &&
      request.method() === "POST"
    ) {
      confirmed = true;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          runId: "run-e2e",
          sessionId: "session-e2e",
          status: "accepted",
        }),
      });
      return;
    }
    throw new Error(`unexpected request: ${request.method()} ${url.pathname}`);
  });

  await page.goto("/?run=run-e2e");
  await expect(page.getByTestId("run-state")).toHaveText("WAITING_HITL");
  await expect(page.getByTestId("profile-field-batch")).toContainText("DEFAULT");

  await page.getByTestId("confirm-profile").click();

  await expect(page.getByTestId("run-state")).toHaveText("SUCCEEDED");
  await expect(page.getByTestId("preview-kind")).toHaveText(
    "ALGORITHM_PREVIEW",
  );
  await expect(page.getByTestId("unverified-conditions")).toContainText(
    "完整当年资格规则未核验",
  );
});

test("submits policy query and renders document evidence", async ({ page }) => {
  await page.route("**/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/v1/policy-queries" && request.method() === "POST") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          runId: "run-policy-e2e",
          sessionId: "session-policy-e2e",
          status: "accepted",
        }),
      });
      return;
    }
    if (
      url.pathname === "/v1/runs/run-policy-e2e/events" &&
      request.method() === "GET"
    ) {
      const event = {
        runId: "run-policy-e2e",
        eventSeq: "2",
        type: "RESULT",
        stage: "EVIDENCE_READY",
        stateVersion: "3",
      };
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: `id: 2\nevent: RESULT\ndata: ${JSON.stringify(event)}\n\n`,
      });
      return;
    }
    if (
      url.pathname === "/v1/runs/run-policy-e2e" &&
      request.method() === "GET"
    ) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(policyRunView("FOUND")),
      });
      return;
    }
    throw new Error(`unexpected request: ${request.method()} ${url.pathname}`);
  });

  await page.goto("/");
  await page.getByTestId("mode-policy").click();
  await page.getByTestId("policy-query").fill("转专业政策是什么？");
  await page.getByTestId("submit-policy").click();

  await expect(page.getByTestId("run-state")).toHaveText("SUCCEEDED");
  await expect(page.getByTestId("policy-status")).toContainText(
    "已找到可核验的文档证据",
  );
  await expect(page.getByTestId("evidence-card")).toHaveCount(1);
  await expect(page.getByTestId("evidence-excerpt")).toContainText(
    "学生可以申请转专业",
  );
});

test("shows NO_EVIDENCE without claiming there is no restriction", async ({
  page,
}) => {
  await page.route("**/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/v1/policy-queries" && request.method() === "POST") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          runId: "run-policy-e2e",
          sessionId: "session-policy-e2e",
          status: "accepted",
        }),
      });
      return;
    }
    if (
      url.pathname === "/v1/runs/run-policy-e2e/events" &&
      request.method() === "GET"
    ) {
      const event = {
        runId: "run-policy-e2e",
        eventSeq: "2",
        type: "RESULT",
        stage: "EVIDENCE_READY",
        stateVersion: "3",
      };
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: `id: 2\nevent: RESULT\ndata: ${JSON.stringify(event)}\n\n`,
      });
      return;
    }
    if (
      url.pathname === "/v1/runs/run-policy-e2e" &&
      request.method() === "GET"
    ) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(policyRunView("NO_EVIDENCE")),
      });
      return;
    }
    throw new Error(`unexpected request: ${request.method()} ${url.pathname}`);
  });

  await page.goto("/");
  await page.getByTestId("mode-policy").click();
  await page.getByTestId("submit-policy").click();

  await expect(page.getByTestId("policy-status")).toContainText(
    "未检索到可靠证据",
  );
  await expect(page.getByTestId("policy-evidence")).toContainText(
    "不代表没有相关政策限制",
  );
  await expect(page.getByTestId("evidence-card")).toHaveCount(0);
});

function policyRunView(status: "FOUND" | "NO_EVIDENCE") {
  return {
    runId: "run-policy-e2e",
    sessionId: "session-policy-e2e",
    taskKind: "POLICY_QA",
    state: "SUCCEEDED",
    stage: "EVIDENCE_READY",
    stateVersion: 3,
    releaseId: "release-policy-e2e",
    result: {
      resultVersion: "result-policy-e2e",
      resultKind: "POLICY_EVIDENCE",
    },
    policyEvidence: {
      resultVersion: "result-policy-e2e",
      resultKind: "POLICY_EVIDENCE",
      runId: "run-policy-e2e",
      releaseId: "release-policy-e2e",
      retrievalStatus: status,
      vectorCandidates: status === "FOUND" ? 1 : 0,
      keywordCandidates: status === "FOUND" ? 1 : 0,
      rerankEnabled: false,
      evidence:
        status === "FOUND"
          ? [
              {
                evidenceId: "evidence-policy-e2e",
                docId: "doc-policy-e2e",
                chunkId: "chunk-policy-e2e",
                title: "湖南测试大学2025年招生章程",
                documentType: "charter",
                issuer: "湖南测试大学",
                sourceUri: "https://example.invalid/charter",
                fetchedAt: "2025-01-02T00:00:00Z",
                sectionPath: "第二章 > 转专业",
                excerpt: "学生可以申请转专业。",
                excerptSha256: "a".repeat(64),
                charStart: 0,
                charEnd: 12,
                regions: ["湖南"],
                categories: ["物理类"],
                batches: ["本科批"],
                validFrom: "2025-01-01",
                validTo: "2025-12-31",
                releaseId: "release-policy-e2e",
              },
            ]
          : [],
    },
  };
}
