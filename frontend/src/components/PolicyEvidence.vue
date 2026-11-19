<template>
  <section class="panel" data-testid="policy-evidence">
    <div class="section-heading">
      <div>
        <p class="eyebrow">M4a / POLICY_QA</p>
        <h2>政策文档证据</h2>
      </div>
      <span class="badge" :class="statusClass">
        {{ result.retrievalStatus }}
      </span>
    </div>

    <p class="policy-status" data-testid="policy-status">
      {{ statusMessage }}
    </p>
    <p
      v-if="result.degradedReason"
      class="degraded"
      data-testid="policy-degraded"
    >
      检索降级：{{ degradedMessage }}
    </p>

    <div v-if="result.evidence.length" class="evidence-grid">
      <article
        v-for="item in result.evidence"
        :key="item.evidenceId"
        class="evidence-card"
        data-testid="evidence-card"
      >
        <div class="evidence-heading">
          <div>
            <strong data-testid="evidence-title">{{ item.title }}</strong>
            <p>{{ item.issuer }} · {{ item.documentType }}</p>
          </div>
          <span class="badge">{{ item.releaseId }}</span>
        </div>

        <p class="evidence-section" data-testid="evidence-section">
          {{ item.sectionPath }}
        </p>
        <blockquote data-testid="evidence-excerpt">
          {{ item.excerpt }}
        </blockquote>

        <dl class="evidence-facts">
          <dt>适用域</dt>
          <dd>
            {{ item.regions.join("、") }} /
            {{ item.categories.join("、") }} /
            {{ item.batches.join("、") }}
          </dd>
          <dt>有效区间</dt>
          <dd>{{ item.validFrom }} — {{ item.validTo || "持续有效" }}</dd>
          <dt>字符定位</dt>
          <dd data-testid="evidence-locator">
            {{ locator(item) }}
          </dd>
          <dt> excerpt hash</dt>
          <dd>
            <span data-testid="evidence-hash">
              {{ shortHash(item.excerptSha256) }}
            </span>
            <details>
              <summary>完整 hash</summary>
              <code>{{ item.excerptSha256 }}</code>
            </details>
          </dd>
          <dt>doc / chunk</dt>
          <dd>{{ item.docId }} / {{ item.chunkId }}</dd>
        </dl>

        <a
          v-if="safeSourceUri(item.sourceUri)"
          :href="safeSourceUri(item.sourceUri)"
          class="source-link"
          data-testid="evidence-source"
          rel="noopener noreferrer"
          target="_blank"
        >
          查看原始来源
        </a>
        <p v-else class="muted">来源地址不可安全打开</p>
      </article>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from "vue";

import type { PolicyEvidenceItem, PolicyEvidenceResult } from "../api/types";

const props = defineProps<{
  result: PolicyEvidenceResult;
}>();

const statusMessage = computed(() => {
  switch (props.result.retrievalStatus) {
    case "FOUND":
      return "已找到可核验的文档证据。";
    case "NO_EVIDENCE":
      return "未检索到可靠证据。这不代表没有相关政策限制。";
    default:
      return `检索未完成：${props.result.retrievalStatus}`;
  }
});

const statusClass = computed(() =>
  props.result.retrievalStatus === "FOUND" ? "open" : "default",
);

const degradedMessage = computed(() => {
  switch (props.result.degradedReason) {
    case "VECTOR_UNAVAILABLE":
      return "向量路不可用，已使用相同适用域的关键词路。";
    case "KEYWORD_UNAVAILABLE":
      return "关键词路不可用，已使用相同适用域的向量路。";
    case "RERANK_TIMEOUT":
      return "重排超时，已使用 RRF 顺序。";
    case "RERANK_UNAVAILABLE":
      return "重排不可用，已使用 RRF 顺序。";
    default:
      return props.result.degradedReason ?? "";
  }
});

function shortHash(value: string): string {
  return value.slice(0, 14);
}

function locator(item: PolicyEvidenceItem): string {
  if (item.charStart === undefined || item.charEnd === undefined) {
    return "未提供字符定位，使用章节路径定位";
  }
  return `${item.charStart}–${item.charEnd}`;
}

function safeSourceUri(value: string): string | undefined {
  try {
    const parsed = new URL(value);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") {
      return parsed.toString();
    }
  } catch {
    return undefined;
  }
  return undefined;
}
</script>
