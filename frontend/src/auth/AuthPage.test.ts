import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

import AuthPage from "./AuthPage.vue";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("invite account registration", () => {
  it("shows recovery codes once and requires explicit confirmation", async () => {
    const codes = Array.from({ length: 10 }, (_, index) =>
      `CODE${index}-VALUE`,
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          account: {
            accountId: "account-1",
            username: "candidate",
            nickname: "考生",
          },
          recoveryCodes: codes,
        }),
      ),
    );
    const wrapper = mount(AuthPage);
    await wrapper
      .findAll(".auth-tabs button")
      .find((button) => button.text().includes("邀请码"))!
      .trigger("click");

    const inputs = wrapper.findAll("input");
    await inputs[0].setValue("INVITE-1");
    await inputs[1].setValue("candidate");
    await inputs[2].setValue("考生");
    await inputs[3].setValue("correct horse battery staple");
    const guardian = wrapper
      .findAll('input[type="checkbox"]')
      .at(-1)!;
    await guardian.setValue(true);
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(wrapper.text()).toContain(codes[0]);
    expect(wrapper.text()).toContain(codes[9]);
    const continueButton = wrapper
      .findAll("button")
      .find((button) => button.text() === "进入知愿")!;
    expect(continueButton.attributes("disabled")).toBeDefined();
    await wrapper
      .findAll('input[type="checkbox"]')
      .at(-1)!
      .setValue(true);
    expect(continueButton.attributes("disabled")).toBeUndefined();
    wrapper.unmount();
  });
});
