import { expect, test } from "@playwright/test";

test("switches the conversation prototype from A to B", async ({ page }) => {
  await page.goto("/prototype-chat.html?variant=A&step=confirm");

  await expect(page.locator(".prototype-switcher span")).toHaveText(
    "A · 聊天主界面",
  );
  await page.locator('[data-variant-step="1"]').click();

  await expect(page).toHaveURL(/variant=B/);
  await expect(page.locator(".prototype-switcher span")).toHaveText(
    "B · 聊天 + 信息面板",
  );
  await expect(page.locator(".app-b")).toBeVisible();
});

test("switches the static report prototype from A to B", async ({ page }) => {
  await page.goto("/prototype-report.html?variant=A");

  await page.locator('[data-variant-step="1"]').click();

  await expect(page).toHaveURL(/variant=B/);
  await expect(page.locator(".prototype-switcher span")).toHaveText(
    "B · 筛选型仪表台",
  );
  await expect(page.locator(".report-b-layout")).toBeVisible();
});
