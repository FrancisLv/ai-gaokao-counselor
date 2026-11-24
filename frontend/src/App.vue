<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";

import type { FormalReportSection, ProfileFieldView } from "./api/types";
import ReportCard from "./components/ReportCard.vue";
import WorkspaceSidebar from "./components/WorkspaceSidebar.vue";
import { useConversation } from "./composables/useConversation";

const chat = useConversation();
const threadScroll = ref<HTMLDivElement | null>(null);
let stickToBottom = true;
const cityOptions = ["长沙", "武汉", "广州", "不限城市"];
const majorOptions = [
  "物理学",
  "物理学类",
  "应用物理学",
  "核物理",
  "材料物理",
  "地球物理学",
  "工程物理",
  "计算机科学与技术",
  "电子信息工程",
  "自动化",
  "还没想好",
];
const riskOptions = ["均衡", "偏冲刺", "偏稳妥"];

const isWelcome = computed(() => chat.stage === "welcome");
const hasReport = computed(() => Boolean(chat.reportRun?.formalResult));
const showComposer = computed(
  () =>
    !["welcome", "preferences", "region", "running", "confirm"].includes(
      chat.stage,
    ) &&
    (chat.stage !== "failed" || hasReport.value),
);
const reportSections = computed(
  () => chat.reportRun?.formalResult?.report.sections ?? [],
);
const candidateSections = computed(() =>
  reportSections.value
    .filter((section) => section.sectionKey.startsWith("candidate:"))
    .map(parseCandidateSection)
    .sort((left, right) => left.sectionKey.localeCompare(right.sectionKey)),
);
const candidateTierGroups = computed(() =>
  (
    [
      { tier: "冲", label: "冲刺", className: "rush" },
      { tier: "稳", label: "稳妥", className: "stable" },
      { tier: "保", label: "保底", className: "safe" },
    ] as const
  ).map(({ tier, label, className }) => {
    const schools = candidateSections.value
      .filter((candidate) => candidate.tier === tier)
      .sort(
        (left, right) =>
          left.probabilityValue - right.probabilityValue ||
          left.sectionKey.localeCompare(right.sectionKey),
      );
    return {
      tier,
      label,
      className,
      total: schools.length,
      schools: schools.slice(0, 2),
    };
  }),
);
const formalVersions = computed(() =>
  chat.sessionRuns
    .filter(
      (run) =>
        run.resultKind === "V1_FORMAL" &&
        run.state === "SUCCEEDED" &&
        run.resultVersion,
    )
    .slice()
    .reverse(),
);
const reportMessageId = computed(() => {
  const messages = chat.messages;
  const reportRunId = chat.reportRun?.runId;
  return (
    [...messages]
      .reverse()
      .find(
        (message) =>
          message.kind === "RESULT" &&
          (!reportRunId || message.runId === reportRunId),
      ) ??
    [...messages].reverse().find((message) => message.kind === "RESULT")
  )?.id ?? "";
});
const confirmationFields = computed(() =>
  Object.entries(chat.currentInterrupt?.fields ?? {}).filter(
    ([key, field]) => showConfirmationField(key, field),
  ),
);
const profileStep = computed(() => {
  if (chat.stage === "welcome") return 0;
  if (chat.stage === "category") return 1;
  if (chat.stage === "score") return 2;
  if (chat.stage === "subjects") return 3;
  if (chat.stage === "preferences") return 4;
  if (chat.stage === "region") return 4;
  if (chat.stage === "confirm") return 5;
  if (chat.stage === "done") return 6;
  return 5;
});

function parseCandidateSection(section: FormalReportSection) {
  const probability = section.body.match(/录取概率约\s*(\d+)%/)?.[1] ?? "—";
  return {
    ...section,
    probability,
    probabilityValue: Number(probability === "—" ? 101 : probability),
    tier: section.body.match(/策略分档：([^\s，。]+)/)?.[1] ?? "方案",
    planCount:
      section.body.match(/招生计划数：(\d+)/)?.[1] ?? "待核验",
    history: [...(section.history ?? [])]
      .sort((left, right) => left.year - right.year)
      .map((item) => ({
        year: item.year,
        minScore: item.minScore ?? null,
        minRank: item.minRank ?? null,
        mappingMethod: item.mappingMethod ?? "",
        mappingConfidence: item.mappingConfidence ?? 0,
      })),
  };
}

function subjectDisabled(subject: string): boolean {
  if (subject === "物理") return chat.category === "历史类";
  if (subject === "历史") return chat.category === "物理类";
  return false;
}

function submitPrompt(): void {
  if (chat.stage === "welcome") {
    chat.startConversation(chat.prompt.trim());
  } else {
    void chat.sendPrompt();
    scrollToLatest(true);
  }
}

function scrollToLatest(force = false): void {
  void nextTick(() => {
    const container = threadScroll.value;
    if (!container || (!force && !stickToBottom)) return;
    container.scrollTop = container.scrollHeight;
    stickToBottom = true;
  });
}

function updateThreadStickiness(): void {
  const container = threadScroll.value;
  if (!container) return;
  stickToBottom =
    container.scrollHeight - container.scrollTop - container.clientHeight <
    120;
}

watch(
  () => [chat.messages.length, chat.reportRun?.runId, chat.stage],
  () => scrollToLatest(),
  { flush: "post" },
);

function choosePrompt(text: string): void {
  chat.startConversation(text);
}

function fieldLabel(key: string): string {
  const labels: Record<string, string> = {
    province: "省份",
    application_year: "报考年份",
    category: "科类",
    batch: "批次",
    score: "高考分数",
    subjects: "选科组合",
    physical_exam: "体检条件",
    "physical_exam.color_vision": "色觉条件",
    tuition_budget: "学费预算",
    foreign_language: "外语语种",
    gender: "性别",
    graduate_status: "应届/往届",
    household_province: "户籍省份",
    school_registration_province: "学籍省份",
    "preferences.risk_appetite": "风险偏好",
    "preferences.priority_dims": "优先考虑",
    "preferences.major_interests": "专业兴趣",
    "preferences.preferred_cities": "偏好城市",
    "preferences.preferred_tiers": "偏好学校层次",
    "preferences.required_cities": "限定城市",
    "preferences.excluded_cities": "排除城市",
    "preferences.required_provinces": "限定省份",
    "preferences.excluded_provinces": "排除省份",
    "preferences.required_tiers": "限定学校层次",
    "preferences.required_major_names": "限定指定专业",
    "preferences.required_major_keywords": "限定专业",
    "preferences.excluded_major_keywords": "排除相关专业",
    "preferences.excluded_major_names": "排除指定专业",
    "preferences.excluded_universities": "排除院校",
  };
  return labels[key] || key.replaceAll("_", " ");
}

function fieldValue(key: string, field: ProfileFieldView): string {
  if (field.stringList) {
    return field.stringList.values.length
      ? field.stringList.values.join(" / ")
      : "不限";
  }
  if (field.intValue !== undefined) return String(field.intValue);
  if (field.stringValue) {
    if (key === "preferences.risk_appetite") {
      return (
        {
          conservative: "偏稳妥",
          balanced: "均衡",
          aggressive: "偏冲刺",
        }[field.stringValue] ?? field.stringValue
      );
    }
    return field.stringValue;
  }
  return "未提供";
}

function fieldStateLabel(state: string): string {
  return (
    {
      PROVIDED: "已提供",
      UNKNOWN: "待补充",
      DECLINED: "未提供",
      DERIVED: "系统推导",
      DEFAULT: "系统默认",
    }[state] ?? state
  );
}

function showConfirmationField(
  key: string,
  field: ProfileFieldView,
): boolean {
  if (!key.startsWith("preferences.")) return true;
  if (field.stringValue?.trim()) return true;
  if (field.intValue !== undefined) return true;
  return Boolean(field.stringList?.values.length);
}
</script>

<template>
  <div class="chat-app">
    <WorkspaceSidebar
      active="chat"
      foot-text="对话已同步到服务端，保留 30 天
概率为历史数据估算，不构成录取承诺"
    >
      <template #primary>
        <button class="new-chat" type="button" @click="chat.resetConversation">
          ＋ 新对话
        </button>
      </template>
      <template #history>
        <p class="history-label">最近对话</p>
        <ul v-if="chat.historyEntries.length" class="history-list">
          <li v-for="entry in chat.historyEntries" :key="entry.sessionId">
            <button type="button" @click="chat.openHistory(entry)">
              {{ entry.title }}
            </button>
          </li>
        </ul>
        <p v-else class="history-empty">
          生成第一条志愿方案后，会在这里留下记录。
        </p>
      </template>
    </WorkspaceSidebar>

    <main class="chat-main">
      <header class="chat-topbar">
        <div>
          <strong>{{ isWelcome ? "新的志愿规划" : "湖南 2025 志愿方案" }}</strong>
          <small>湖南 · 2025 · 本科批</small>
        </div>
        <nav class="session-actions" aria-label="账户与志愿表">
          <a class="session-badge" href="/wishlists.html">我的志愿表</a>
          <a class="session-badge" href="/account.html">账户设置</a>
        </nav>
      </header>

      <div v-if="isWelcome" class="welcome-stage">
        <div class="welcome-logo">愿</div>
        <h1>把复杂的志愿填报，聊明白。</h1>
        <p>我会先了解你的分数、选科和偏好，再逐步核验数据并生成推荐报告。</p>
        <div class="welcome-prompts">
          <button
            type="button"
            @click="choosePrompt('我物理类 612 分，选的是物化生，帮我看看能报什么学校')"
          >
            <b>帮我生成志愿方案</b>
            <span>从分数和选科开始</span>
          </button>
          <button
            type="button"
            @click="choosePrompt('我想先看看物理类能报哪些计算机专业')"
          >
            <b>我的分数能报什么学校？</b>
            <span>结合湖南 2025 招生数据</span>
          </button>
          <button
            type="button"
            @click="choosePrompt('我还没想好专业方向，先帮我梳理一下')"
          >
            <b>我还没想好专业方向</b>
            <span>先聊兴趣和城市偏好</span>
          </button>
        </div>
      </div>

      <div
        v-else
        ref="threadScroll"
        class="thread-scroll"
        @scroll.passive="updateThreadStickiness"
      >
        <div class="thread">
          <template v-for="message in chat.messages" :key="message.id">
            <div
              v-if="
                message.kind === 'RESULT' &&
                message.id === reportMessageId &&
                chat.reportRun
              "
              class="chat-message assistant"
            >
              <div class="avatar">愿</div>
              <ReportCard
                :report-run="chat.reportRun"
                :tier-groups="candidateTierGroups"
                :formal-versions="formalVersions"
              />
            </div>
            <div
              v-else-if="message.kind !== 'RESULT'"
              class="chat-message"
              :class="message.role"
            >
              <div class="avatar">{{ message.role === "user" ? "你" : "愿" }}</div>
              <div class="bubble">{{ message.text }}</div>
            </div>
          </template>

          <div v-if="chat.stage === 'category'" class="chat-message assistant">
            <div class="avatar">愿</div>
            <div class="bubble">
              <p>先确认你的科类。</p>
              <div class="choice-row">
                <button class="choice-chip primary" @click="chat.chooseCategory('物理类')">
                  物理类
                </button>
                <button class="choice-chip" @click="chat.chooseCategory('历史类')">
                  历史类
                </button>
              </div>
            </div>
          </div>

          <div v-if="chat.stage === 'score'" class="chat-message assistant">
            <div class="avatar">愿</div>
            <div class="inline-card">
              <label>
                高考分数
                <input v-model.number="chat.score" max="750" min="0" type="number" />
              </label>
              <button class="primary-button" type="button" @click="chat.submitScore">
                确认分数
              </button>
            </div>
          </div>

          <div v-if="chat.stage === 'subjects'" class="chat-message assistant">
            <div class="avatar">愿</div>
            <div class="inline-card">
              <p>选择你的 3+1+2 组合，已选 {{ chat.subjects.length }}/3。</p>
              <div class="choice-row">
                <button
                  v-for="subject in chat.allSubjects"
                  :key="subject"
                  class="choice-chip"
                  :class="{ primary: chat.subjects.includes(subject) }"
                  :disabled="subjectDisabled(subject)"
                  type="button"
                  @click="chat.toggleSubject(subject)"
                >
                  {{ subject }}
                </button>
              </div>
              <button class="primary-button" type="button" @click="chat.submitSubjects">
                下一步
              </button>
            </div>
          </div>

          <div v-if="chat.stage === 'preferences'" class="chat-message assistant">
            <div class="avatar">愿</div>
            <div class="preference-card">
              <section>
                <span>城市偏好</span>
                <div class="choice-row">
                  <button
                    v-for="city in cityOptions"
                    :key="city"
                    class="choice-chip"
                    :class="{ primary: chat.cityPreferences.includes(city) }"
                    type="button"
                    @click="chat.togglePreference('city', city)"
                  >
                    {{ city }}
                  </button>
                </div>
              </section>
              <section>
                <span>专业兴趣</span>
                <div class="choice-row">
                  <button
                    v-for="major in majorOptions"
                    :key="major"
                    class="choice-chip"
                    :class="{ primary: chat.majorPreferences.includes(major) }"
                    type="button"
                    @click="chat.togglePreference('major', major)"
                  >
                    {{ major }}
                  </button>
                </div>
              </section>
              <section>
                <span>风险偏好</span>
                <div class="choice-row">
                  <button
                    v-for="risk in riskOptions"
                    :key="risk"
                    class="choice-chip"
                    :class="{ primary: chat.riskPreference === risk }"
                    type="button"
                    @click="chat.riskPreference = risk"
                  >
                    {{ risk }}
                  </button>
                </div>
              </section>
              <button class="primary-button" type="button" @click="chat.finishPreferences">
                开始生成方案
              </button>
            </div>
          </div>

          <div v-if="chat.stage === 'region' && chat.regionClarification" class="chat-message assistant">
            <div class="avatar">愿</div>
            <div class="preference-card region-card">
              <div class="card-heading">
                <h3>确认“{{ chat.regionClarification.term }}”的范围</h3>
                <span class="session-badge">待确认</span>
              </div>
              <p>
                “{{ chat.regionClarification.term }}”没有唯一省份边界。
                请选择具体省份，或使用快捷组；快捷组只会填充省份，不会直接执行。
              </p>
              <section>
                <span>大区快捷组</span>
                <div class="choice-row">
                  <button
                    v-for="preset in chat.regionClarification.presets"
                    :key="preset.id"
                    class="choice-chip"
                    :class="{ primary: chat.selectedRegionPresetIds.includes(preset.id) }"
                    type="button"
                    @click="chat.toggleRegionPreset(preset.id)"
                  >
                    {{ preset.name }}
                  </button>
                </div>
              </section>
              <section>
                <span>具体省份</span>
                <div class="choice-row region-province-grid">
                  <button
                    v-for="province in chat.regionClarification.provinces"
                    :key="province"
                    class="choice-chip"
                    :class="{ primary: chat.selectedRegionProvinces.includes(province) }"
                    type="button"
                    @click="chat.toggleRegionProvince(province)"
                  >
                    {{ province }}
                  </button>
                </div>
              </section>
              <p class="muted">
                当前选择：
                {{ chat.selectedRegionProvinces.length
                  ? chat.selectedRegionProvinces.join("、")
                  : "尚未选择省份" }}
              </p>
              <div class="region-actions">
                <button
                  class="primary-button"
                  :disabled="chat.busy || !chat.selectedRegionProvinces.length"
                  type="button"
                  @click="chat.submitRegionClarification()"
                >
                  确认这些省份
                </button>
                <button
                  class="secondary-button"
                  :disabled="chat.busy"
                  type="button"
                  @click="chat.submitRegionClarification(true)"
                >
                  先不管这个地域限制
                </button>
              </div>
            </div>
          </div>

          <div v-if="chat.stage === 'running'" class="chat-message assistant">
            <div class="avatar">愿</div>
            <div class="progress-card">
              <template v-if="chat.run?.taskKind === 'EXPLAIN_RESULT'">
                <div class="progress-row"><span class="status-dot done"></span>已绑定当前报告版本</div>
                <div class="progress-row"><span class="status-dot done"></span>正在核对排序、概率与风险依据</div>
                <div class="progress-row"><span class="status-dot active"></span>正在生成解释</div>
              </template>
              <template v-else>
                <div class="progress-row"><span class="status-dot done"></span>画像信息已提交</div>
                <div class="progress-row"><span class="status-dot done"></span>正在核对选科与招生限制</div>
                <div class="progress-row"><span class="status-dot active"></span>正在计算冲稳保并生成报告</div>
              </template>
              <p class="muted">可以留在当前页面，结果会自动出现。</p>
            </div>
          </div>

          <div
            v-if="chat.stage === 'confirm' && chat.currentInterrupt"
            class="chat-message assistant"
          >
            <div class="avatar">愿</div>
            <div class="profile-confirm-card">
              <div class="card-heading">
                <h3>请确认我理解的画像</h3>
                <span class="session-badge">待确认</span>
              </div>
              <div class="profile-grid">
                <div
                  v-for="([key, field]) in confirmationFields"
                  :key="key"
                  class="profile-field"
                >
                  <small>{{ fieldLabel(String(key)) }}</small>
                  <strong>{{ fieldValue(String(key), field) }}</strong>
                  <span>{{ fieldStateLabel(field.state) }}</span>
                </div>
              </div>
              <button
                class="primary-button"
                :disabled="chat.busy"
                type="button"
                @click="chat.confirmProfileCard"
              >
                确认画像并继续
              </button>
            </div>
          </div>

          <div
            v-if="chat.reportRun?.formalResult && !reportMessageId"
            class="chat-message assistant"
          >
            <div class="avatar">愿</div>
            <ReportCard
              :report-run="chat.reportRun"
              :tier-groups="candidateTierGroups"
              :formal-versions="formalVersions"
            />
          </div>

          <div
            v-if="chat.error"
            class="chat-message assistant"
            role="status"
          >
            <div class="avatar">愿</div>
            <div class="bubble message-status">
              {{ chat.error }}
            </div>
          </div>
        </div>
      </div>

      <form v-if="isWelcome" class="composer welcome-composer" @submit.prevent="submitPrompt">
        <textarea
          v-model="chat.prompt"
          aria-label="输入消息"
          placeholder="描述你的分数、选科或想了解的问题…"
          rows="1"
          @keydown.enter.exact.prevent="submitPrompt"
        />
        <button aria-label="发送" type="submit">↑</button>
      </form>
      <form v-else-if="showComposer" class="composer" @submit.prevent="submitPrompt">
        <textarea
          v-model="chat.prompt"
          aria-label="输入消息"
          :placeholder="hasReport ? '继续询问报告，或输入「不要武汉」「只看计算机」「更稳一点」…' : '继续回答问题，或补充你的偏好…'"
          rows="1"
          @keydown.enter.exact.prevent="submitPrompt"
        />
        <button aria-label="发送" type="submit">↑</button>
      </form>
    </main>

    <aside class="chat-inspector">
      <h2>画像与进度</h2>
      <p>右侧展示系统当前理解，生成前必须由你确认。</p>
      <section class="inspector-section">
        <h3>当前画像</h3>
        <div class="inspector-kv"><span>地区</span><strong>湖南 · 2025</strong></div>
        <div class="inspector-kv"><span>科类</span><strong>{{ chat.profileFields.category }}</strong></div>
        <div class="inspector-kv"><span>分数</span><strong>{{ chat.profileFields.score }}</strong></div>
        <div class="inspector-kv"><span>选科</span><strong>{{ chat.profileFields.subjects }}</strong></div>
        <div class="inspector-kv"><span>城市</span><strong>{{ chat.profileFields.city }}</strong></div>
        <div class="inspector-kv"><span>专业</span><strong>{{ chat.profileFields.major }}</strong></div>
      </section>
      <section class="inspector-section">
        <h3>处理进度</h3>
        <div class="pipeline">
          <div class="pipeline-step" :class="{ done: profileStep > 0, active: profileStep === 0 }"><span>1</span><p>理解分数与选科</p></div>
          <div class="pipeline-step" :class="{ done: profileStep > 3, active: profileStep === 3 }"><span>2</span><p>整理偏好</p></div>
          <div class="pipeline-step" :class="{ done: profileStep > 4, active: profileStep === 4 }"><span>3</span><p>确认画像</p></div>
          <div class="pipeline-step" :class="{ done: profileStep > 5, active: profileStep === 5 }"><span>4</span><p>核验数据并生成</p></div>
        </div>
      </section>
      <section class="inspector-section">
        <h3>数据说明</h3>
        <p class="muted">
          固定使用湖南 2025 本科批。报告中的概率是历史数据估算，不构成录取承诺。
        </p>
      </section>
    </aside>
  </div>
</template>
