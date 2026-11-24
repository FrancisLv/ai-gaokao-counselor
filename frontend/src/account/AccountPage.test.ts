import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

import AccountPage from "./AccountPage.vue";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("account settings", () => {
  it("keeps logout visible and does not expose account data export", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/v1/account") {
          return jsonResponse({
            account: {
              accountId: "account-1",
              username: "candidate",
              nickname: "湖南考生",
            },
          });
        }
        throw new Error(`unexpected request: ${url}`);
      }),
    );

    const wrapper = mount(AccountPage);
    await flushPromises();

    expect(wrapper.get(".account-topbar .account-signout").text()).toBe(
      "退出登录",
    );
    expect(wrapper.get(".workspace-nav a.active").text()).toContain(
      "账户与安全",
    );
    expect(wrapper.text()).not.toContain("导出账号数据");
    expect(wrapper.text()).toContain("不能用于日常登录");
    expect(wrapper.text()).toContain("全部遗失后无法自助找回账号");
    expect(wrapper.text()).toContain("敏感资格数据");
    expect(wrapper.text()).toContain("撤销敏感资格同意");
    expect(wrapper.text()).not.toContain("登录设备");
    expect(wrapper.text()).not.toContain("隐私设置");
    wrapper.unmount();
  });

  it("shows revocation feedback next to the sensitive consent action", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/v1/account") {
          return jsonResponse({
            account: {
              accountId: "account-1",
              username: "candidate",
              nickname: "湖南考生",
            },
          });
        }
        if (url === "/v1/account/consents/sensitive:revoke") {
          return jsonResponse({ revokedCount: 0 });
        }
        throw new Error(`unexpected request: ${url}`);
      }),
    );

    const wrapper = mount(AccountPage);
    await flushPromises();
    const revoke = wrapper
      .findAll("button")
      .find((button) => button.text() === "撤销敏感资格同意")!;
    await revoke.trigger("click");
    await flushPromises();

    expect(wrapper.get(".sensitive-consent-feedback").text()).toContain(
      "当前没有可撤销的敏感资格同意",
    );
    wrapper.unmount();
  });
});
