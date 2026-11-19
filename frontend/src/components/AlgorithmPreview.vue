<script setup lang="ts">
import type {
  AlgorithmPreview,
  CandidateWithProb,
} from "../api/types";

defineProps<{ preview: AlgorithmPreview }>();

const groups = [
  { key: "rushCandidates", label: "冲" },
  { key: "stableCandidates", label: "稳" },
  { key: "safeCandidates", label: "保" },
] as const;
const visibleCandidateLimit = 12;

function probability(candidate: CandidateWithProb): string {
  return `${Math.round((candidate.prob?.probability ?? 0) * 100)}%`;
}

function candidateName(candidate: CandidateWithProb): string {
  const school = candidate.candidate?.universityName ?? "未知院校";
  const group = candidate.candidate?.majorGroup ?? "-";
  return `${school} · 专业组 ${group}`;
}

function json(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function visibleCandidates(
  preview: AlgorithmPreview,
  key: (typeof groups)[number]["key"],
): CandidateWithProb[] {
  return preview[key].slice(0, visibleCandidateLimit);
}

function candidateTotal(
  preview: AlgorithmPreview,
  key: (typeof groups)[number]["key"],
): number {
  const totals = {
    rushCandidates: preview.rushTotal,
    stableCandidates: preview.stableTotal,
    safeCandidates: preview.safeTotal,
  };
  return totals[key] ?? preview[key].length;
}
</script>

<template>
  <section class="panel preview">
    <div class="preview-warning" data-testid="controlled-preview">
      <strong>仅用于内部受控验证</strong>
      <p>
        ALGORITHM_PREVIEW 不是正式志愿方案，正式推荐、资格与证据门禁尚未完成。
      </p>
    </div>

    <div class="section-heading">
      <div>
        <p class="eyebrow">RESULT</p>
        <h2 data-testid="preview-kind">{{ preview.resultKind }}</h2>
      </div>
      <span class="badge default">
        {{ preview.formalRecommendation ? "FORMAL" : "INTERNAL ONLY" }}
      </span>
    </div>

    <dl class="metrics">
      <div>
        <dt>位次</dt>
        <dd>{{ preview.rank }}</dd>
      </div>
      <div>
        <dt>百分位</dt>
        <dd>{{ preview.percentile }}</dd>
      </div>
      <div>
        <dt>release</dt>
        <dd>{{ preview.releaseId }}</dd>
      </div>
      <div>
        <dt>resultVersion</dt>
        <dd>{{ preview.resultVersion }}</dd>
      </div>
    </dl>

    <section
      v-for="group in groups"
      :key="group.key"
      class="candidate-section"
    >
      <h3>{{ group.label }}档候选</h3>
      <p v-if="preview[group.key].length === 0" class="muted">无候选</p>
      <template v-else>
        <p class="muted">
          共 {{ candidateTotal(preview, group.key) }} 条；验收页展示前
          {{ Math.min(candidateTotal(preview, group.key), visibleCandidateLimit) }}
          条。
        </p>
        <div class="candidate-grid">
        <article
          v-for="candidate in visibleCandidates(preview, group.key)"
          :key="`${candidate.candidate?.universityCode}-${candidate.candidate?.majorGroup}`"
          class="candidate"
        >
          <strong>{{ candidateName(candidate) }}</strong>
          <span>{{ candidate.candidate?.schoolTier }}</span>
          <span>{{ candidate.candidate?.location }}</span>
          <span class="probability">{{ probability(candidate) }}</span>
          <small>{{ candidate.prob?.method }} / {{ candidate.prob?.dataYears }} 年</small>
        </article>
        </div>
      </template>
    </section>

    <section class="detail-block">
      <h3>AuditBasis</h3>
      <pre>{{ json(preview.auditBasis) }}</pre>
    </section>

    <section class="detail-block">
      <h3>约束剔除</h3>
      <p v-if="preview.rejectedCandidates.length === 0" class="muted">无</p>
      <ul v-else>
        <li
          v-for="(rejected, index) in preview.rejectedCandidates"
          :key="`${rejected.rule}-${index}`"
        >
          {{ rejected.rule }}：{{ rejected.reason }}
        </li>
      </ul>
    </section>

    <section class="detail-block">
      <h3>优化后的志愿表</h3>
      <ol>
        <li
          v-for="item in preview.wishlist?.candidates ?? []"
          :key="`${item.finalRank}-${item.cwp.candidate?.universityCode}`"
        >
          {{ item.finalRank }}. {{ candidateName(item.cwp) }}
          （{{ probability(item.cwp) }}）
        </li>
      </ol>
    </section>

    <section class="detail-block">
      <h3>已检查项</h3>
      <ul>
        <li v-for="item in preview.verificationSummary" :key="item.key">
          {{ item.status }} · {{ item.key }}：{{ item.detail }}
        </li>
      </ul>
    </section>

    <section class="detail-block unverified">
      <h3>未核验条件</h3>
      <ul data-testid="unverified-conditions">
        <li
          v-for="item in preview.unverifiedConditions"
          :key="item.code"
        >
          {{ item.code }}：{{ item.message }}
        </li>
      </ul>
    </section>

    <aside class="risk-box">
      <strong>风险提示</strong>
      <p>{{ preview.riskNotice }}</p>
      <p>{{ preview.disclaimer }}</p>
    </aside>
  </section>
</template>
