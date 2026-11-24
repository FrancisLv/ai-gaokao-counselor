<script setup lang="ts">
import { onMounted, ref } from "vue";

import {
  changePassword,
  deleteAccount,
  GatewayError,
  getAccount,
  logoutAccount,
  regenerateRecoveryCodes,
  revokeSensitiveConsent,
  updateAccount,
} from "../api/gateway";
import type { Account } from "../api/gateway";
import WorkspaceSidebar from "../components/WorkspaceSidebar.vue";

const account = ref<Account | null>(null);
const nickname = ref("");
const currentPassword = ref("");
const newPassword = ref("");
const recoveryCodes = ref<string[]>([]);
const deletionConfirmation = ref("");
const busy = ref(false);
const sensitiveBusy = ref(false);
const error = ref("");
const notice = ref("");
const sensitiveFeedback = ref("");
const sensitiveFeedbackError = ref(false);

onMounted(async () => {
  try {
    account.value = (await getAccount()).account;
    nickname.value = account.value.nickname;
  } catch (cause) {
    if (cause instanceof GatewayError && cause.status === 401) {
      location.replace(`/login.html?return=${encodeURIComponent("/account.html")}`);
      return;
    }
    error.value = cause instanceof Error ? cause.message : String(cause);
  }
});

async function saveNickname(): Promise<void> {
  error.value = "";
  notice.value = "";
  busy.value = true;
  try {
    account.value = (await updateAccount(nickname.value)).account;
    notice.value = "昵称已更新。";
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    busy.value = false;
  }
}

async function savePassword(): Promise<void> {
  error.value = "";
  notice.value = "";
  busy.value = true;
  try {
    await changePassword(currentPassword.value, newPassword.value);
    currentPassword.value = "";
    newPassword.value = "";
    notice.value = "密码已修改，其他设备已退出。";
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    busy.value = false;
  }
}

async function logout(): Promise<void> {
  await logoutAccount().catch(() => undefined);
  location.assign("/login.html");
}

async function renewRecoveryCodes(): Promise<void> {
  if (!currentPassword.value) {
    error.value = "请先输入当前密码。";
    return;
  }
  error.value = "";
  try {
    recoveryCodes.value = await regenerateRecoveryCodes(currentPassword.value);
    notice.value = "旧恢复码已作废，请立即保存新恢复码。";
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  }
}

async function removeAccount(): Promise<void> {
  if (!currentPassword.value || deletionConfirmation.value !== "删除账号") {
    error.value = "请输入当前密码并键入“删除账号”。";
    return;
  }
  if (!confirm("账户删除不可恢复，确定继续吗？")) return;
  error.value = "";
  try {
    await deleteAccount(currentPassword.value, deletionConfirmation.value);
    location.assign("/login.html");
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  }
}

async function revokeSensitive(): Promise<void> {
  sensitiveFeedback.value = "";
  sensitiveFeedbackError.value = false;
  sensitiveBusy.value = true;
  try {
    const count = await revokeSensitiveConsent();
    sensitiveFeedback.value =
      count > 0
        ? `已撤销 ${count} 条敏感资格数据同意。`
        : "当前没有可撤销的敏感资格同意。";
  } catch (cause) {
    sensitiveFeedbackError.value = true;
    sensitiveFeedback.value =
      cause instanceof Error ? cause.message : String(cause);
  } finally {
    sensitiveBusy.value = false;
  }
}

</script>

<template>
  <main class="account-page">
    <WorkspaceSidebar
      active="account"
      note-title="账号边界"
      note-text="账号只保存用户名、昵称和登录安全信息。分数、选科与偏好仍属于具体规划。"
      foot-text="敏感资格信息使用前单独征得同意
账号及私有规划数据可随时删除"
    />

    <section class="account-main">
      <header class="account-topbar">
        <div>
          <strong>账户与安全</strong>
          <small>管理密码、恢复码与个人数据</small>
        </div>
        <nav class="session-actions" aria-label="账户操作">
          <a class="session-badge" href="/">返回对话</a>
          <button class="session-badge account-signout" type="button" @click="logout">
            退出登录
          </button>
        </nav>
      </header>

      <div class="account-scroll">
        <section class="account-content">
          <header class="account-heading">
            <div>
              <span class="eyebrow">账户与安全</span>
              <h1>管理你的知愿账号</h1>
              <p>
                在这里维护账号安全，管理密码、恢复码与个人数据。
              </p>
            </div>
            <div v-if="account" class="account-identity">
              <span>{{ account.nickname || account.username }}</span>
              <small>当前登录 · {{ account.username }}</small>
            </div>
          </header>

          <p v-if="error" class="form-error" role="alert">{{ error }}</p>
          <p v-if="notice" class="form-notice" role="status">{{ notice }}</p>

          <div class="account-settings-grid">
            <article v-if="account" class="settings-card">
              <h2>账号资料</h2>
              <p class="settings-description">用于识别当前登录账号，不会作为志愿画像的一部分。</p>
              <label>
                用户名
                <input :value="account.username" disabled />
              </label>
              <label>
                昵称
                <input v-model="nickname" maxlength="20" />
              </label>
              <button class="primary-button" :disabled="busy" @click="saveNickname">
                保存昵称
              </button>
            </article>

            <article class="settings-card">
              <h2>修改密码</h2>
              <p class="settings-description">修改后，其他设备上的登录会话会立即退出。</p>
              <label>
                当前密码
                <input
                  v-model="currentPassword"
                  type="password"
                  autocomplete="current-password"
                />
              </label>
              <label>
                新密码
                <input
                  v-model="newPassword"
                  type="password"
                  autocomplete="new-password"
                  minlength="12"
                />
              </label>
              <button class="secondary-button" :disabled="busy" @click="savePassword">
                修改密码
              </button>
            </article>

            <article class="settings-card">
              <h2>恢复码</h2>
              <p class="settings-description">
                忘记密码时，用恢复码重设密码；它不能用于日常登录。每个恢复码只能使用一次，仅在生成时展示。重新生成会立即作废旧恢复码；全部遗失后无法自助找回账号。
              </p>
              <button class="secondary-button" @click="renewRecoveryCodes">
                重新生成恢复码
              </button>
              <pre v-if="recoveryCodes.length" class="recovery-code-grid">{{
                recoveryCodes.join("\n")
              }}</pre>
            </article>

            <article class="settings-card">
              <h2>敏感资格数据</h2>
              <p class="settings-description">
                色觉、体检、性别、户籍或学籍等信息只在相关专业确有资格要求时询问。撤销后将停止用于新规划，既有报告仍可查看。
              </p>
              <div class="button-row">
                <button
                  class="secondary-button"
                  type="button"
                  :disabled="sensitiveBusy"
                  @click="revokeSensitive"
                >
                  {{ sensitiveBusy ? "正在撤销…" : "撤销敏感资格同意" }}
                </button>
              </div>
              <p
                v-if="sensitiveFeedback"
                class="sensitive-consent-feedback"
                :class="{ error: sensitiveFeedbackError }"
                :role="sensitiveFeedbackError ? 'alert' : 'status'"
              >
                {{ sensitiveFeedback }}
              </p>
            </article>

            <article class="settings-card account-card-wide danger-zone">
              <h2>删除账号</h2>
              <p>
                删除后无法恢复。对话、画像、报告和志愿表都会被清理。连续 24
                个月未登录的账号也会按同一规则清理。
              </p>
              <label>
                键入“删除账号”确认
                <input v-model="deletionConfirmation" autocomplete="off" />
              </label>
              <button class="secondary-button danger" @click="removeAccount">
                永久删除账号
              </button>
            </article>
          </div>
        </section>
      </div>
    </section>
  </main>
</template>
