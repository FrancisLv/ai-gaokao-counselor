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
