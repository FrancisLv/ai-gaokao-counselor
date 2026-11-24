<script setup lang="ts">
import type { RunView } from "../api/types";

type CandidateHistory = {
  year: number;
  minScore: number | null;
  minRank: number | null;
};

type CandidatePreview = {
  sectionKey: string;
  title: string;
  probability: string;
  planCount: string;
  history: CandidateHistory[];
};

type TierPreview = {
  tier: "冲" | "稳" | "保";
  label: string;
  className: string;
  total: number;
  schools: CandidatePreview[];
};

type FormalVersion = {
  runId: string;
  resultVersion?: string;
};

defineProps<{
  reportRun: RunView;
  tierGroups: TierPreview[];
  formalVersions: FormalVersion[];
}>();

function availableHistoryYears(candidate: CandidatePreview): number {
  return candidate.history.filter(
    (history) => history.minScore !== null && history.minRank !== null,
  ).length;
}

function historyCompletenessLabel(candidate: CandidatePreview): string {
  const years = availableHistoryYears(candidate);
  if (years === 0) return "暂无有效历史，样本不足";
  if (years === 1) return "仅 1 年有效历史，样本有限";
  if (years === 2) return "2 年历史参考";
  return "已核验关键事实";
}
</script>

<template>
  <div class="result-card">
    <div class="result-hero">
      <small>
        正式推荐报告 ·
        {{ reportRun.formalResult?.resultVersion }}
      </small>
      <h2>你的冲稳保方案已生成</h2>
      <p>报告已完成数据与规则校验。</p>
    </div>

    <div class="tier-summary">
      <span
        v-for="group in tierGroups"
        :key="group.tier"
        :class="group.className"
      >
        <b>{{ group.tier }}</b>
        {{ group.total }}
      </span>
    </div>

    <div
      v-for="group in tierGroups"
      :key="group.tier"
      class="preview-tier"
    >
      <div class="preview-tier-heading">
        <div>
          <span class="tier" :class="group.className">{{ group.tier }}</span>
          <strong>{{ group.label }}</strong>
        </div>
        <a
          v-if="group.total"
          :href="`/reports/${encodeURIComponent(reportRun.runId)}?tier=${encodeURIComponent(group.tier)}#tier-${encodeURIComponent(group.tier)}`"
          target="_blank"
        >
          查看全部 {{ group.total }} 个 ↗
        </a>
      </div>
      <div v-if="group.schools.length" class="preview-list">
        <a
          v-for="candidate in group.schools"
          :key="candidate.sectionKey"
          class="preview-row preview-school-link"
          :href="`/school.html?run=${encodeURIComponent(reportRun.runId)}&section=${encodeURIComponent(candidate.sectionKey)}`"
          target="_blank"
        >
          <div>
            <strong>{{ candidate.title.replace(/^\d+\.\s*/, "") }}</strong>
            <small
              :class="{ 'history-limited': availableHistoryYears(candidate) < 2 }"
            >
              计划数 {{ candidate.planCount }} ·
              {{ historyCompletenessLabel(candidate) }}
            </small>
          </div>
          <span class="probability">{{ candidate.probability }}%</span>
          <div v-if="candidate.history.length" class="preview-history">
            <span
              v-for="history in candidate.history"
              :key="history.year"
              :class="{
                missing:
                  history.minScore === null || history.minRank === null,
              }"
            >
              <b>{{ history.year }}</b>
              {{
                history.minScore !== null && history.minRank !== null
                  ? `最低分/最低位次 ${history.minScore}/${history.minRank}`
                  : "暂无数据"
              }}
            </span>
          </div>
        </a>
      </div>
      <p v-else class="preview-empty">该梯度暂无正式候选。</p>
    </div>

    <div class="result-footer">
      <p>{{ reportRun.formalResult?.riskNotice }}</p>
      <a
        class="primary-button"
        :href="`/reports/${encodeURIComponent(reportRun.runId)}`"
        target="_blank"
      >
        查看完整推荐报告 ↗
      </a>
    </div>
    <div v-if="formalVersions.length > 1" class="result-versions">
      <strong>历史版本</strong>
      <a
        v-for="version in formalVersions"
        :key="version.runId"
        :class="{ current: version.runId === reportRun.runId }"
        :href="`/reports/${encodeURIComponent(version.runId)}`"
        target="_blank"
      >
        {{ version.resultVersion?.slice(0, 8) }}
      </a>
    </div>
  </div>
</template>
