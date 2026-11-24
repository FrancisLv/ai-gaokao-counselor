<script setup lang="ts">
import { computed, ref } from "vue";

import {
  loginAccount,
  registerAccount,
  resetPassword,
} from "../api/gateway";

type Mode = "login" | "register" | "reset";

const mode = ref<Mode>("login");
const username = ref("");
const password = ref("");
const inviteCode = ref("");
const nickname = ref("");
const recoveryCode = ref("");
const guardianAck = ref(false);
const recoveryCodes = ref<string[]>([]);
const codesSaved = ref(false);
const busy = ref(false);
const error = ref("");
const notice = ref("");
const returnTo = computed(() => {
  const value = new URLSearchParams(location.search).get("return");
  return value?.startsWith("/") && !value.startsWith("//") ? value : "/";
});

function switchMode(next: Mode): void {
  mode.value = next;
  error.value = "";
  notice.value = "";
}

async function submit(): Promise<void> {
  error.value = "";
  notice.value = "";
  busy.value = true;
  try {
    if (mode.value === "login") {
      await loginAccount(username.value.trim(), password.value);
      location.assign(returnTo.value);
      return;
    }
    if (mode.value === "reset") {
      await resetPassword({
        username: username.value.trim(),
        recoveryCode: recoveryCode.value.trim(),
        newPassword: password.value,
      });
      notice.value = "密码已重设，请使用新密码登录。";
      mode.value = "login";
      return;
    }
    const result = await registerAccount({
      inviteCode: inviteCode.value.trim(),
      username: username.value.trim(),
      password: password.value,
      nickname: nickname.value.trim(),
      privacyPolicyVersion: "privacy-v2",
      guardianAckRecorded: guardianAck.value,
    });
    recoveryCodes.value = result.recoveryCodes;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    busy.value = false;
  }
}

function continueAfterRegistration(): void {
  if (!codesSaved.value) return;
  location.assign(returnTo.value);
}

function downloadRecoveryCodes(): void {
  const body = [
    "知愿账号恢复码",
    `用户名：${username.value.trim()}`,
    "",
    ...recoveryCodes.value,
    "",
    "每个恢复码只能使用一次。请离线保存，不要分享。",
  ].join("\n");
  const url = URL.createObjectURL(
    new Blob([body], { type: "text/plain;charset=utf-8" }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = "知愿-账号恢复码.txt";
  link.click();
  URL.revokeObjectURL(url);
  codesSaved.value = true;
}
</script>

<template>
  <main class="auth-page">
    <aside class="auth-context">
      <a class="auth-brand-link" href="/">
        <span class="brand-mark">愿</span>
        <span>
          <strong>知愿 · 志愿规划</strong>
          <small>对话式高考志愿助手</small>
        </span>
      </a>

      <div class="auth-context-copy">
        <span class="auth-kicker">湖南 2025 · 本科批</span>
        <h1>复杂志愿填报，<br />聊明白。</h1>
        <p>
          登录后继续之前的对话，或从分数、选科和偏好开始一份新的志愿规划。
        </p>
      </div>

      <ol class="auth-steps">
        <li>
          <span>1</span>
          <div>
            <strong>对话收集画像</strong>
            <small>用自然语言说清分数、选科和偏好</small>
          </div>
        </li>
        <li>
          <span>2</span>
          <div>
            <strong>确认后再生成</strong>
            <small>系统先展示当前理解，由你确认画像</small>
          </div>
        </li>
        <li>
          <span>3</span>
          <div>
            <strong>确定性数据核验</strong>
            <small>院校、专业和概率由算法计算，不由模型改写</small>
          </div>
        </li>
      </ol>

      <p class="auth-context-foot">
        对话已同步到服务端，保留 30 天<br />
        概率为历史数据估算，不构成录取承诺
      </p>
    </aside>

    <section class="auth-main">
      <header class="auth-topbar">
        <div>
          <strong>账号与安全</strong>
          <small>登录后继续你的志愿规划</small>
        </div>
        <span class="session-badge">受控内测 · 湖南 2025</span>
      </header>

      <div class="auth-main-scroll">
        <section class="auth-card">
      <template v-if="recoveryCodes.length">
        <span class="eyebrow">只展示这一次</span>
        <h1>保存你的恢复码</h1>
        <p class="auth-description">
          恢复码用于忘记密码时重设密码，不能用于日常登录。每个恢复码只能使用一次，遗失全部恢复码后，账号无法自助找回。
        </p>
        <pre class="recovery-code-grid">{{ recoveryCodes.join("\n") }}</pre>
        <button class="secondary-button" type="button" @click="downloadRecoveryCodes">
          下载恢复码
        </button>
        <label class="check-row">
          <input v-model="codesSaved" type="checkbox" />
          我已安全保存这 10 个恢复码
        </label>
        <button
          class="primary-button"
          type="button"
          :disabled="!codesSaved"
          @click="continueAfterRegistration"
        >
          进入知愿
        </button>
      </template>

      <template v-else>
        <div class="auth-tabs">
          <button :class="{ active: mode === 'login' }" @click="switchMode('login')">
            登录
          </button>
          <button :class="{ active: mode === 'register' }" @click="switchMode('register')">
            使用邀请码注册
          </button>
        </div>
        <h1>
          {{ mode === "login" ? "继续你的志愿规划" : mode === "register" ? "创建受控账号" : "重设密码" }}
        </h1>
        <p class="auth-description">
          {{
            mode === "login"
              ? "使用你的知愿账号继续对话，历史方案会自动恢复。"
              : mode === "register"
                ? "注册需要一次性邀请码。系统不收集手机号、邮箱或真实姓名。"
                : "输入用户名和一个未使用的恢复码。"
          }}
        </p>

        <form @submit.prevent="submit">
          <label v-if="mode === 'register'">
            邀请码
            <input v-model="inviteCode" autocomplete="off" required />
          </label>
          <label>
            用户名
            <input
              v-model="username"
              autocomplete="username"
              minlength="4"
              maxlength="32"
              required
            />
          </label>
          <label v-if="mode === 'register'">
            昵称（可选）
            <input v-model="nickname" maxlength="20" autocomplete="nickname" />
          </label>
          <label v-if="mode === 'reset'">
            恢复码
            <input v-model="recoveryCode" autocomplete="one-time-code" required />
          </label>
          <label>
            {{ mode === "reset" ? "新密码" : "密码" }}
            <input
              v-model="password"
              type="password"
              :autocomplete="mode === 'login' ? 'current-password' : 'new-password'"
              minlength="12"
              maxlength="128"
              required
            />
          </label>
          <label v-if="mode === 'register'" class="check-row">
            <input v-model="guardianAck" type="checkbox" required />
            我确认本人已成年，或已取得监护人同意
          </label>
          <p v-if="error" class="form-error">{{ error }}</p>
          <p v-if="notice" class="form-notice">{{ notice }}</p>
          <button class="primary-button" type="submit" :disabled="busy">
            {{ busy ? "处理中…" : mode === "login" ? "登录" : mode === "register" ? "注册" : "重设密码" }}
          </button>
        </form>

        <button
          v-if="mode !== 'reset'"
          class="text-button"
          type="button"
          @click="switchMode('reset')"
        >
          忘记密码，使用恢复码
        </button>
        <a v-else class="text-button" href="/login.html">返回登录</a>
      </template>
        </section>

        <p class="auth-legal">
          知愿只提供志愿决策辅助。最终填报请核对当年招生章程与官方计划。
        </p>
      </div>
    </section>
  </main>
</template>
