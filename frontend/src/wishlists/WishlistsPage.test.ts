import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

import WishlistsPage from "./WishlistsPage.vue";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("my wishlists", () => {
  it("renders current and stale states and refetches by status", async () => {
    const current = {
      draftVersionId: "draft-2",
      resultVersion: "result-current",
      runId: "run-current",
      releaseId: "release-1",
      draftVersion: 2,
      title: "2025 物理类 本科批 志愿表",
      applicationYear: 2025,
      category: "物理类",
      batch: "本科批",
      groupCount: 3,
      majorCount: 8,
      status: "CURRENT",
      savedAt: "2026-11-23T02:00:00Z",
    };
    const stale = {
      ...current,
      draftVersionId: "draft-old",
      resultVersion: "result-old",
      runId: "run-old",
      draftVersion: 1,
      status: "STALE",
      staleReason: "RELEASE_REVOKED",
      title: "2024 历史类 本科批 志愿表",
    };
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("status=STALE")) {
          return jsonResponse({ wishlists: [stale] });
        }
        return jsonResponse({ wishlists: [current, stale] });
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(WishlistsPage);
    await flushPromises();

    expect(wrapper.get(".workspace-nav a.active").text()).toContain(
      "我的志愿表",
    );
    expect(wrapper.text()).toContain("当前可用");
    expect(wrapper.text()).toContain("已失效");
    expect(wrapper.text()).toContain("RELEASE_REVOKED");
    await wrapper
      .findAll(".filter-row button")
      .find((button) => button.text() === "已失效")!
      .trigger("click");
    await flushPromises();

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("status=STALE"),
      expect.any(Object),
    );
    expect(wrapper.text()).not.toContain("2025 物理类 本科批 志愿表");
    wrapper.unmount();
  });
});
