<script setup lang="ts">
import { computed, onMounted, ref } from "vue";

import {
  deleteWishlist,
  GatewayError,
  listWishlistHistory,
  listWishlists,
  restoreWishlistDraftVersion,
} from "../api/gateway";
import type {
  WishlistHistoryDraft,
  WishlistSummary,
} from "../api/gateway";
import WorkspaceSidebar from "../components/WorkspaceSidebar.vue";

const items = ref<WishlistSummary[]>([]);
const histories = ref<Record<string, WishlistHistoryDraft[]>>({});
const filter = ref("");
const busy = ref("");
const error = ref("");
const notice = ref("");
const filtered = computed(() => items.value);

const statusLabels: Record<WishlistSummary["status"], string> = {
  CURRENT: "当前可用",
  SUPERSEDED: "已被新方案替代",
  STALE: "已失效",
};

onMounted(() => void load());

async function load(): Promise<void> {
  error.value = "";
  try {
    items.value = await listWishlists(filter.value);
  } catch (cause) {
    if (cause instanceof GatewayError && cause.status === 401) {
      location.replace(
        `/login.html?return=${encodeURIComponent("/wishlists.html")}`,
      );
      return;
    }
    error.value = cause instanceof Error ? cause.message : String(cause);
  }
}

async function setFilter(value: string): Promise<void> {
  filter.value = value;
  await load();
}

async function toggleHistory(item: WishlistSummary): Promise<void> {
  if (histories.value[item.resultVersion]) {
    const next = { ...histories.value };
    delete next[item.resultVersion];
    histories.value = next;
    return;
  }
  histories.value = {
    ...histories.value,
    [item.resultVersion]: await listWishlistHistory(item.resultVersion),
  };
}

async function restore(
  item: WishlistSummary,
  draft: WishlistHistoryDraft,
): Promise<void> {
  busy.value = item.resultVersion;
  error.value = "";
  try {
    await restoreWishlistDraftVersion(
      item.resultVersion,
      draft.draftVersion,
      `restore-${item.resultVersion}-${draft.draftVersion}-${crypto.randomUUID()}`,
    );
    notice.value = `已从版本 ${draft.draftVersion} 创建新的最新版本。`;
    await load();
    await toggleHistory(item);
    await toggleHistory(item);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    busy.value = "";
  }
}

async function remove(item: WishlistSummary): Promise<void> {
  if (!confirm(`确定删除“${item.title}”及其全部版本吗？`)) return;
  busy.value = item.resultVersion;
  error.value = "";
  try {
    await deleteWishlist(
      item.resultVersion,
      `delete-${item.resultVersion}-${crypto.randomUUID()}`,
    );
    notice.value = "志愿表已删除。";
    await load();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    busy.value = "";
  }
}

function savedAt(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Asia/Shanghai",
  }).format(new Date(value));
}
</script>

<template>
  <main class="account-page wishlist-page">
    <WorkspaceSidebar
      active="wishlists"
      note-title="保存规则"
      note-text="这里只展示你显式保存过的志愿草稿，不会自动收录报告页里的临时修改。"
      foot-text="已被替代或失效的版本仍可追溯
删除后无法恢复"
    />

    <section class="account-main">
      <header class="account-topbar">
        <div>
          <strong>我的志愿表</strong>
          <small>查看、恢复和管理已保存的志愿方案</small>
        </div>
        <a class="session-badge" href="/">返回对话</a>
      </header>

      <div class="account-scroll">
        <section class="account-content">
          <header class="account-heading">
            <div>
              <span class="eyebrow">已保存方案</span>
              <h1>我的志愿表</h1>
              <p>按版本保留每次显式保存的志愿草稿，便于回看、恢复或继续修改。</p>
            </div>
            <a class="primary-button" href="/">继续规划</a>
          </header>

          <div class="wishlist-toolbar">
            <div class="filter-row" aria-label="按状态筛选志愿表">
              <button :class="{ active: filter === '' }" @click="setFilter('')">
                全部
              </button>
              <button
                :class="{ active: filter === 'CURRENT' }"
                @click="setFilter('CURRENT')"
              >
                当前可用
              </button>
              <button
                :class="{ active: filter === 'SUPERSEDED' }"
                @click="setFilter('SUPERSEDED')"
              >
                已被替代
              </button>
              <button
                :class="{ active: filter === 'STALE' }"
                @click="setFilter('STALE')"
              >
                已失效
              </button>
            </div>
            <span class="wishlist-count">共 {{ filtered.length }} 份方案</span>
          </div>

          <p v-if="error" class="form-error" role="alert">{{ error }}</p>
          <p v-if="notice" class="form-notice" role="status">{{ notice }}</p>

          <div v-if="filtered.length" class="wishlist-grid">
            <article
              v-for="item in filtered"
              :key="item.resultVersion"
              class="wishlist-card"
            >
              <div class="wishlist-card-head">
                <span class="status-pill" :class="item.status.toLowerCase()">
                  {{ statusLabels[item.status] }}
                </span>
                <span class="wishlist-version">草稿版本 {{ item.draftVersion }}</span>
              </div>

              <div class="wishlist-title-row">
                <div>
                  <h2>{{ item.title }}</h2>
                  <p>{{ item.applicationYear }} · {{ item.category }} · {{ item.batch }}</p>
                </div>
              </div>

              <div class="wishlist-metrics">
                <div>
                  <small>院校专业组</small>
                  <strong>{{ item.groupCount }}</strong>
                </div>
                <div>
                  <small>拟填专业</small>
                  <strong>{{ item.majorCount }}</strong>
                </div>
                <div>
                  <small>最后保存</small>
                  <strong>{{ savedAt(item.savedAt) }}</strong>
                </div>
              </div>

              <p v-if="item.status === 'STALE'" class="wishlist-warning">
                失效原因：{{ item.staleReason || "结果依据已撤销" }}
              </p>

              <div class="wishlist-actions">
                <a
                  class="secondary-button"
                  :href="`/reports/${encodeURIComponent(item.runId)}`"
                >
                  查看报告
                </a>
                <a
                  v-if="item.status === 'CURRENT'"
                  class="primary-button"
                  :href="`/reports/${encodeURIComponent(item.runId)}`"
                >
                  继续修改
                </a>
                <a
                  v-if="item.status === 'SUPERSEDED' && item.successorRunId"
                  class="secondary-button"
                  :href="`/reports/${encodeURIComponent(item.successorRunId)}`"
                >
                  查看新方案
                </a>
                <button class="text-button" @click="toggleHistory(item)">
                  {{ histories[item.resultVersion] ? "收起版本" : "版本历史" }}
                </button>
                <button
                  class="text-button danger"
                  :disabled="busy === item.resultVersion"
                  @click="remove(item)"
                >
                  删除
                </button>
              </div>

              <ol v-if="histories[item.resultVersion]" class="history-versions">
                <li
                  v-for="draft in histories[item.resultVersion]"
                  :key="draft.draftVersionId"
                >
                  <span>
                    <strong>版本 {{ draft.draftVersion }}</strong>
                    <small>{{ savedAt(draft.createdAt) }}</small>
                  </span>
                  <button
                    v-if="item.status === 'CURRENT'"
                    class="text-button"
                    :disabled="busy === item.resultVersion"
                    @click="restore(item, draft)"
                  >
                    恢复为最新
                  </button>
                </li>
              </ol>
            </article>
          </div>

          <div v-else class="empty-state">
            <span class="empty-state-mark">表</span>
            <h2>{{ filter ? "当前筛选下没有志愿表" : "还没有保存的志愿表" }}</h2>
            <p>
              {{
                filter
                  ? "切换其他状态查看，或回到对话继续调整方案。"
                  : "生成正式报告后，在报告中点击“保存草稿”，它就会出现在这里。"
              }}
            </p>
            <a class="primary-button" href="/">
              {{ filter ? "返回对话" : "开始规划" }}
            </a>
          </div>
        </section>
      </div>
    </section>
  </main>
</template>
