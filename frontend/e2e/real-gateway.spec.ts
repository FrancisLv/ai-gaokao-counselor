import { expect, test } from "@playwright/test";

test("completes the profile-to-report flow through the real gateway", async ({
  page,
}) => {
  test.skip(
    process.env.E2E_REAL_GATEWAY !== "1",
    "设置 E2E_REAL_GATEWAY=1 后运行真实后端联调",
  );

  await page.goto("/");
  await page
    .getByRole("button", { name: /帮我生成志愿方案/ })
    .click();
  await expect(page.getByText("城市偏好")).toBeVisible();
  await page.getByRole("button", { name: "开始生成方案" }).click();

  await expect(page.getByText("请确认我理解的画像")).toBeVisible({
    timeout: 15_000,
  });
  await page.getByRole("button", { name: "确认画像并继续" }).click();

  await expect(page.getByText("你的冲稳保方案已生成")).toBeVisible({
    timeout: 75_000,
  });
  await expect(page.locator(".preview-tier")).toHaveCount(3);
  await expect(page.locator(".tier-summary")).toContainText("冲");
  await expect(page.locator(".tier-summary")).toContainText("稳");
  await expect(page.locator(".tier-summary")).toContainText("保");
  const stableAllHref = await page
    .locator(".preview-tier")
    .nth(1)
    .getByRole("link", { name: /查看全部/ })
    .getAttribute("href");
  expect(decodeURIComponent(stableAllHref!)).toContain(
    "tier=稳#tier-稳",
  );
  await expect(page.locator(".preview-history").first()).toContainText("2022");
  await expect(page.locator(".preview-history").first()).toContainText("2024");
  await expect(page.locator(".preview-history").first()).toContainText(
    "最低分/最低位次",
  );
  const reportLink = page.getByRole("link", {
    name: "查看完整推荐报告 ↗",
  });
  const href = await reportLink.getAttribute("href");
  expect(href).toMatch(/^\/reports\//);

  await page.goto(href!);
  await expect(
    page.getByRole("heading", { name: "你的冲稳保志愿报告" }),
  ).toBeVisible({ timeout: 15_000 });
  if (process.env.E2E_EXPECT_LLM_FALLBACK === "1") {
    await expect(page.locator("#risk")).toContainText("生成质量降级");
  }
  await expect(page.locator(".history-table").first()).toContainText("2022");
  await expect(page.locator(".history-table").first()).toContainText("2024");
  await expect(page.getByRole("heading", { name: "资格核验" })).toHaveCount(0);
  await expect(page.getByText("跨年匹配依据")).toHaveCount(0);
  await expect(page.locator(".school-row h3 + p").first()).not.toContainText(
    /最低录取分|样本年份|2022年|2023年|2024年/,
  );
  for (const tier of ["冲", "稳", "保"]) {
    const probabilities = (
      await page.locator(`#tier-${tier} .probability`).allTextContents()
    ).map((value) => Number(value.replace("%", "")));
    expect(probabilities).toEqual(
      [...probabilities].sort((left, right) => left - right),
    );
  }
  const firstSchool = page.locator(".school-row").first();
  await expect(firstSchool).toBeVisible();
  await firstSchool.click();
  await expect(page).toHaveURL(/school\.html\?run=.*&section=/);
  await expect(page.locator(".school-detail-hero h1")).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.locator(".history-table").first()).toContainText("2022");
  await expect(page.locator(".history-table").first()).toContainText("2024");
});

test("explains a below-control-line score instead of reporting a dependency outage", async ({
  page,
}) => {
  test.skip(
    process.env.E2E_REAL_GATEWAY !== "1",
    "设置 E2E_REAL_GATEWAY=1 后运行真实后端联调",
  );

  await page.goto("/");
  await page
    .getByLabel("输入消息")
    .fill("我物理类 300 分，选的是物化生，帮我看看能报什么学校");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText("城市偏好")).toBeVisible();
  await page.getByRole("button", { name: "开始生成方案" }).click();

  await expect(page.getByText("请确认我理解的画像")).toBeVisible({
    timeout: 15_000,
  });
  await page.getByRole("button", { name: "确认画像并继续" }).click();

  await expect(page.getByText(/分数低于.*控制线/)).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.getByText(/svc-algo 不可用/)).toHaveCount(0);
});

test("continues after the report and restores the conversation from the server", async ({
  page,
}) => {
  test.skip(
    process.env.E2E_REAL_GATEWAY !== "1",
    "设置 E2E_REAL_GATEWAY=1 后运行真实后端联调",
  );

  await page.goto("/");
  await page
    .getByRole("button", { name: /帮我生成志愿方案/ })
    .click();
  await page.getByRole("button", { name: "开始生成方案" }).click();
  await expect(page.getByText("请确认我理解的画像")).toBeVisible({
    timeout: 15_000,
  });
  await page.getByRole("button", { name: "确认画像并继续" }).click();
  await expect(page.getByText("你的冲稳保方案已生成")).toBeVisible({
    timeout: 75_000,
  });

  const composer = page.getByLabel("输入消息");
  await composer.fill("为什么这个方案这样排序？");
  await page.getByRole("button", { name: "发送" }).click();
  const postReportQuestion = page
    .locator(".chat-message.user")
    .filter({ hasText: "为什么这个方案这样排序？" });
  await expect(postReportQuestion).toBeInViewport();
  const resultBeforeQuestion = await page.locator(".thread").evaluate((thread) => {
    const result = thread.querySelector(".result-card");
    const question = Array.from(
      thread.querySelectorAll<HTMLElement>(".chat-message.user"),
    ).find((item) => item.textContent?.includes("为什么这个方案这样排序？"));
    return Boolean(
      result &&
        question &&
        result.compareDocumentPosition(question) &
          Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });
  expect(resultBeforeQuestion).toBe(true);
  await expect(page.getByText(/确定性算法/).last()).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.getByText("你的冲稳保方案已生成")).toBeVisible();

  await composer.fill("我不想去北京上大学，太远了");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText("请确认我理解的画像")).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.getByText("排除城市", { exact: true })).toBeVisible();
  await expect(page.getByText("北京", { exact: true })).toBeVisible();
  await expect(page.getByText("已提供", { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/preferences\./)).toHaveCount(0);
  await expect(page.getByText("PROVIDED", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "确认画像并继续" }).click();
  await expect(page.getByText("历史版本")).toBeVisible({
    timeout: 75_000,
  });
  await expect(page.getByText("你的冲稳保方案已生成")).toBeVisible();

  await composer.fill("我不想学物理学专业呢？");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText("请确认我理解的画像")).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.getByText("排除指定专业", { exact: true })).toBeVisible();
  await expect(page.getByText("物理学", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "确认画像并继续" }).click();
  await expect(page.locator(".result-versions a")).toHaveCount(3, {
    timeout: 75_000,
  });

  const sessionUrl = page.url();
  await page.reload();
  await expect(page.getByText("你的冲稳保方案已生成")).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.getByLabel("输入消息")).toBeVisible();
  expect(page.url()).toBe(sessionUrl);
  await expect(
    page.locator(".history-list").getByRole("button").first(),
  ).toContainText("志愿方案");
});

test("caps low-sample probabilities and rejects reused group codes", async ({
  page,
}) => {
  test.skip(
    process.env.E2E_REAL_GATEWAY !== "1",
    "设置 E2E_REAL_GATEWAY=1 后运行真实后端联调",
  );

  await page.goto("/");
  await page
    .getByLabel("输入消息")
    .fill("我物理类 600 分，选的是物化生，帮我生成一份方案");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText("城市偏好")).toBeVisible();
  await page.getByRole("button", { name: "广州", exact: true }).click();
  await page.getByRole("button", { name: "开始生成方案" }).click();
  await expect(page.getByText("请确认我理解的画像")).toBeVisible({
    timeout: 15_000,
  });
  await page.getByRole("button", { name: "确认画像并继续" }).click();
  await expect(page.getByText("你的冲稳保方案已生成")).toBeVisible({
    timeout: 75_000,
  });

  const href = await page
    .getByRole("link", { name: "查看完整推荐报告 ↗" })
    .getAttribute("href");
  await page.goto(href!);
  await expect(page.locator("#tier-稳 .school-row")).toHaveCount(22);
  await expect(
    page.locator(".school-row").filter({ hasText: "兰州大学 （110）专业组" }),
  ).toHaveCount(0);
  const stableProbabilities = (
    await page.locator("#tier-稳 .probability").allTextContents()
  ).map((value) => Number(value.replace("%", "")));
  expect(stableProbabilities.every((value) => value <= 75)).toBe(true);
  const safeProbabilities = (
    await page.locator("#tier-保 .probability").allTextContents()
  ).map((value) => Number(value.replace("%", "")));
  expect(safeProbabilities.every((value) => value >= 95)).toBe(true);
});
