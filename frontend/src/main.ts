import { createApp } from "vue";
import App from "./App.vue";
import { GatewayError, getAccount } from "./api/gateway";
import "./style.css";

async function bootstrap(): Promise<void> {
  const reportMatch = location.pathname.match(/^\/reports\/([^/]+)$/);
  if (reportMatch) {
    location.replace(
      `/report.html?run=${encodeURIComponent(decodeURIComponent(reportMatch[1]))}`,
    );
    return;
  }
  try {
    await getAccount();
    createApp(App).mount("#app");
  } catch (cause) {
    if (cause instanceof GatewayError && cause.status === 401) {
      const current = `${location.pathname}${location.search}`;
      location.replace(`/login.html?return=${encodeURIComponent(current)}`);
      return;
    }
    document.querySelector("#app")!.textContent =
      "无法连接服务，请稍后刷新重试。";
  }
}

void bootstrap();
