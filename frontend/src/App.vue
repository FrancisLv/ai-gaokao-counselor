<template>
  <main class="shell">
    <header class="hero">
      <div>
        <p class="eyebrow">M2 / M3 / M4a 内部验收</p>
        <h1>Run 验收控制台</h1>
        <p class="lead">
          通过 gateway 验证结构化画像、HITL 恢复、SSE 事件与
          ALGORITHM_PREVIEW，以及 POLICY_QA 的文档证据检索。
        </p>
      </div>
      <span class="principal">principal: {{ lifecycle.principalId }}</span>
    </header>

    <div class="mode-switch" role="tablist" aria-label="验收任务类型">
      <button
        :class="{ active: lifecycle.taskMode.value === 'plan' }"
        data-testid="mode-plan"
        type="button"
        @click="lifecycle.taskMode.value = 'plan'"
      >
        志愿方案预览
      </button>
      <button
        :class="{ active: lifecycle.taskMode.value === 'policy' }"
        data-testid="mode-policy"
        type="button"
        @click="lifecycle.taskMode.value = 'policy'"
      >
        政策证据检索
      </button>
    </div>

    <form
      v-if="lifecycle.taskMode.value === 'plan'"
      class="panel form-grid"
      @submit.prevent="lifecycle.submit"
    >
      <label>
        省份
        <select v-model="lifecycle.profile.province" data-testid="province">
          <option value="湖南">湖南</option>
        </select>
      </label>
      <label>
        报考年
        <input
          v-model.number="lifecycle.profile.applicationYear"
          data-testid="application-year"
          min="2020"
          type="number"
        />
      </label>
      <label>
        科类
        <select v-model="lifecycle.profile.category" data-testid="category">
          <option value="物理类">物理类</option>
          <option value="历史类">历史类</option>
        </select>
      </label>
      <label>
        批次
        <input v-model="lifecycle.profile.batch" data-testid="batch" />
      </label>
      <label>
        分数
        <input
          v-model.number="lifecycle.profile.score"
          data-testid="score"
          min="0"
          max="750"
          type="number"
        />
      </label>
      <fieldset class="subjects">
        <legend>选科组合</legend>
        <label v-for="subject in subjects" :key="subject">
          <input
            v-model="lifecycle.profile.subjects"
            :data-testid="`subject-${subjectKey(subject)}`"
            :value="subject"
            type="checkbox"
          />
          {{ subject }}
        </label>
      </fieldset>
      <button data-testid="submit-run" :disabled="lifecycle.submitting.value">
        {{ lifecycle.submitting.value ? "提交中…" : "提交任务" }}
      </button>
    </form>

    <form
      v-else
      class="panel form-grid policy-form"
      data-testid="policy-form"
      @submit.prevent="lifecycle.submitPolicy"
    >
      <label class="wide-field">
        政策问题
        <textarea
          v-model="lifecycle.policy.queryText"
          data-testid="policy-query"
          rows="3"
        />
      </label>
      <label>
        省份
        <select v-model="lifecycle.policy.province" data-testid="policy-province">
          <option value="湖南">湖南</option>
        </select>
      </label>
      <label>
        报考年
        <input
          v-model.number="lifecycle.policy.applicationYear"
          data-testid="policy-application-year"
          min="2020"
          type="number"
        />
      </label>
      <label>
        科类
        <select v-model="lifecycle.policy.category" data-testid="policy-category">
          <option value="物理类">物理类</option>
          <option value="历史类">历史类</option>
          <option value="不限">不限</option>
        </select>
      </label>
      <label>
        批次
        <select v-model="lifecycle.policy.batch" data-testid="policy-batch">
          <option value="本科批">本科批</option>
          <option value="不限">不限</option>
        </select>
      </label>
      <label>
        学校 ID（可选，逗号分隔）
        <input v-model="lifecycle.policy.schoolIds" data-testid="policy-schools" />
      </label>
      <label>
        文档类型（可选，逗号分隔）
        <input
          v-model="lifecycle.policy.documentTypes"
          data-testid="policy-document-types"
        />
      </label>
      <label>
        检索模式
        <select v-model="lifecycle.policy.mode" data-testid="policy-mode">
          <option value="hybrid">hybrid</option>
          <option value="vector">vector</option>
          <option value="keyword">keyword</option>
        </select>
      </label>
      <button
        data-testid="submit-policy"
        :disabled="lifecycle.submitting.value"
      >
        {{ lifecycle.submitting.value ? "检索中…" : "检索政策证据" }}
      </button>
    </form>

    <p v-if="lifecycle.error.value" class="error" role="alert">
      {{ lifecycle.error.value }}
    </p>

    <section v-if="lifecycle.run.value" class="panel">
      <h2>Run 状态</h2>
      <dl class="facts">
        <dt>runId</dt>
        <dd data-testid="run-id">{{ lifecycle.run.value.runId }}</dd>
        <dt>sessionId</dt>
        <dd>{{ lifecycle.run.value.sessionId }}</dd>
        <dt>状态</dt>
        <dd data-testid="run-state">{{ lifecycle.run.value.state }}</dd>
        <dt>阶段</dt>
        <dd>{{ lifecycle.run.value.stage }}</dd>
        <dt>releaseId</dt>
        <dd>{{ lifecycle.run.value.releaseId }}</dd>
        <dt>stateVersion</dt>
        <dd>{{ lifecycle.run.value.stateVersion }}</dd>
        <dt>过期时间</dt>
        <dd>{{ lifecycle.run.value.expiresAt ?? "-" }}</dd>
      </dl>
      <p
        v-if="lifecycle.run.value.terminalReason"
        class="terminal-reason"
      >
        terminal reason：
        <strong data-testid="terminal-reason">
          {{ lifecycle.run.value.terminalReason }}
        </strong>
      </p>
      <p
        v-if="lifecycle.run.value.state === 'FAILED'"
        class="error"
        data-testid="policy-failure"
      >
        {{ failureMessage(lifecycle.run.value.terminalReason) }}
      </p>
      <div class="actions">
        <button
          v-if="lifecycle.run.value.state !== 'CANCELLED'"
          class="secondary danger"
          data-testid="cancel-run"
          :disabled="lifecycle.cancelling.value"
          type="button"
          @click="lifecycle.cancel"
        >
          {{ lifecycle.cancelling.value ? "取消中…" : "取消 Run" }}
        </button>
      </div>
      <p
        v-if="lifecycle.cancelReceipt.value"
        class="cancel-receipt"
        data-testid="cancel-receipt"
      >
        {{
          lifecycle.cancelReceipt.value.accepted
            ? `已取消：${lifecycle.cancelReceipt.value.state}`
            : `未取消：${lifecycle.cancelReceipt.value.reason ?? "REJECTED"}；当前终态保持 ${lifecycle.cancelReceipt.value.state}`
        }}
      </p>
    </section>

    <section
      v-if="lifecycle.run.value?.currentInterrupt"
      class="panel confirm-card"
      data-testid="profile-card"
    >
      <div class="section-heading">
        <div>
          <p class="eyebrow">{{ lifecycle.run.value.currentInterrupt.kind }}</p>
          <h2>画像确认</h2>
        </div>
        <span class="badge open">OPEN</span>
      </div>
      <dl class="facts">
        <dt>interruptId</dt>
        <dd>{{ lifecycle.run.value.currentInterrupt.interruptId }}</dd>
        <dt>profileVersion</dt>
        <dd>{{ lifecycle.run.value.currentInterrupt.profileVersion }}</dd>
        <dt>stateVersion</dt>
        <dd>{{ lifecycle.run.value.stateVersion }}</dd>
      </dl>
      <div class="profile-fields">
        <article
          v-for="(field, key) in lifecycle.run.value.currentInterrupt.fields"
          :key="key"
          :data-testid="`profile-field-${key}`"
          class="profile-field"
        >
          <span class="field-key">{{ fieldLabel(String(key)) }}</span>
          <strong>{{ fieldValue(field) }}</strong>
          <span class="badge" :class="String(field.state).toLowerCase()">
            {{ field.state }}
          </span>
          <small>{{ field.source }}</small>
        </article>
      </div>
      <ul
        v-if="lifecycle.run.value.currentInterrupt.findings?.length"
        class="findings"
      >
        <li
          v-for="finding in lifecycle.run.value.currentInterrupt.findings"
          :key="finding.code"
        >
          {{ finding.code }} / {{ finding.messageKey }}
        </li>
      </ul>
      <div
        v-if="lifecycle.run.value.currentInterrupt.kind === 'PROFILE_CONFIRM'"
        class="actions"
      >
        <button
          data-testid="confirm-profile"
          :disabled="lifecycle.confirming.value"
          type="button"
          @click="lifecycle.confirm"
        >
          {{ lifecycle.confirming.value ? "确认中…" : "确认画像" }}
        </button>
      </div>
    </section>

    <section v-if="lifecycle.events.value.length" class="panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">SSE</p>
          <h2>事件时间线</h2>
        </div>
        <span class="badge" :class="lifecycle.connected.value ? 'open' : 'default'">
          {{ lifecycle.connected.value ? "CONNECTED" : "RECONNECTING" }}
        </span>
      </div>
      <ol class="event-list">
        <li
          v-for="event in lifecycle.events.value"
          :key="event.eventSeq"
          data-testid="event-row"
          class="event-row"
        >
          <div class="event-heading">
            <strong>#{{ event.eventSeq }}</strong>
            <span class="badge open">{{ event.type }}</span>
            <span>{{ event.stage }}</span>
            <small>stateVersion {{ event.stateVersion }}</small>
          </div>
          <details v-if="event.payload" class="event-payload">
            <summary>查看 payload</summary>
            <pre>{{ stringify(event.payload) }}</pre>
          </details>
        </li>
      </ol>
    </section>

    <AlgorithmPreview
      v-if="lifecycle.run.value?.resultPreview"
      :preview="lifecycle.run.value.resultPreview"
    />
    <PolicyEvidence
      v-if="lifecycle.run.value?.policyEvidence"
      :result="lifecycle.run.value.policyEvidence"
    />
  </main>
</template>

<script setup lang="ts">
import { onMounted } from "vue";

import AlgorithmPreview from "./components/AlgorithmPreview.vue";
import PolicyEvidence from "./components/PolicyEvidence.vue";
import { useRunLifecycle } from "./composables/useRunLifecycle";
import type { ProfileFieldView } from "./api/types";

const lifecycle = useRunLifecycle();
const subjects = ["物理", "历史", "化学", "生物", "政治", "地理"];
const fieldLabels: Record<string, string> = {
  province: "省份",
  application_year: "报考年",
  category: "科类",
  batch: "批次",
  score: "分数",
  subjects: "选科组合",
};

onMounted(() => {
  void lifecycle.restore();
});

function subjectKey(subject: string): string {
  return {
    物理: "physics",
    历史: "history",
    化学: "chemistry",
    生物: "biology",
    政治: "politics",
    地理: "geography",
  }[subject]!;
}

function fieldLabel(key: string): string {
  return fieldLabels[key] ?? key;
}

function fieldValue(field: ProfileFieldView): string {
  if (field.stringList) return field.stringList.values.join("、");
  if (field.intValue !== undefined) return String(field.intValue);
  if (field.stringValue !== undefined) return field.stringValue;
  return "UNKNOWN";
}

function stringify(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function failureMessage(reason?: string): string {
  switch (reason) {
    case "UNSUPPORTED_SCOPE":
      return "当前适用范围不受支持，请修正省份、报考年、科类或批次。";
    case "RELEASE_UNAVAILABLE":
    case "RELEASE_REVOKED":
      return "当前发布版本不可用，请稍后重试或联系管理员。";
    case "RETRIEVAL_UNAVAILABLE":
      return "检索服务暂不可用，本次没有生成可靠证据。";
    case "VECTOR_UNAVAILABLE":
      return "向量检索不可用，请切换为 hybrid 或 keyword 后重试。";
    case "KEYWORD_UNAVAILABLE":
      return "关键词检索不可用，请切换为 hybrid 或 vector 后重试。";
    case "INDEX_MODEL_MISMATCH":
      return "检索索引与查询模型版本不一致，当前不能返回证据。";
    case "RETRIEVAL_QUERY_REQUIRED":
      return "请输入需要检索的政策问题。";
    case "RETRIEVAL_SCOPE_REQUIRED":
      return "请补充完整的省份、报考年、科类和批次。";
    default:
      return reason ? `任务失败：${reason}` : "任务失败。";
  }
}
</script>
