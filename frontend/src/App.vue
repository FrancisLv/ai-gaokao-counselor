<script setup lang="ts">
import { computed } from "vue";

import type { FormalReportSection } from "./api/types";
import { useConversation } from "./composables/useConversation";

const chat = useConversation();
const cityOptions = ["长沙", "武汉", "广州", "不限城市"];
const majorOptions = ["计算机", "电子信息", "自动化", "还没想好"];
const riskOptions = ["均衡", "偏冲刺", "偏稳妥"];

const isWelcome = computed(() => chat.stage === "welcome");
const showComposer = computed(
  () =>
    !["welcome", "preferences", "running", "confirm", "done", "failed"].includes(
      chat.stage,
    ),
);
const reportSections = computed(
  () => chat.run?.formalResult?.report.sections ?? [],
);
const candidateSections = computed(() =>
  reportSections.value
    .filter((section) => section.sectionKey.startsWith("candidate:"))
    .slice(0, 4)
    .map(parseCandidateSection),
);
const profileStep = computed(() => {
  if (chat.stage === "welcome") return 0;
  if (chat.stage === "category") return 1;
  if (chat.stage === "score") return 2;
  if (chat.stage === "subjects") return 3;
  if (chat.stage === "preferences") return 4;
  if (chat.stage === "confirm") return 5;
  if (chat.stage === "done") return 6;
  return 5;
});

function parseCandidateSection(section: FormalReportSection) {
  return {
    ...section,
    probability:
      section.body.match(/录取概率约\s*(\d+)%/)?.[1] ?? "—",
    tier: section.body.match(/策略分档：([^\s，。]+)/)?.[1] ?? "方案",
    planCount:
      section.body.match(/招生计划数：(\d+)/)?.[1] ?? "待核验",
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
  }
}

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
    tuition_budget: "学费预算",
  };
  return labels[key] || key.replaceAll("_", " ");
}

function fieldValue(field: {
  stringValue?: string;
  intValue?: number;
  stringList?: { values: string[] };
}): string {
  if (field.stringList) return field.stringList.values.join(" / ");
  if (field.intValue !== undefined) return String(field.intValue);
  return field.stringValue || "未提供";
}
</script>

<template>
  <div class="chat-app">
    <aside class="chat-sidebar">
      <div class="chat-brand">
        <span class="brand-mark">愿</span>
        <div>
          <strong>知愿 · 志愿规划</strong>
          <small>对话式高考志愿助手</small>
        </div>
      </div>
      <button class="new-chat" type="button" @click="chat.resetConversation">
        ＋ 新对话
      </button>
      <p class="history-label">最近对话</p>
      <ul v-if="chat.historyEntries.length" class="history-list">
        <li v-for="entry in chat.historyEntries" :key="entry.runId">
          <button type="button" @click="chat.openHistory(entry)">
            {{ entry.title }}
          </button>
        </li>
      </ul>
      <p v-else class="history-empty">生成第一条志愿方案后，会在这里留下记录。</p>
      <div class="sidebar-foot">
        对话仅保存在当前浏览器<br />
        概率为历史数据估算，不构成录取承诺
      </div>
    </aside>

    <main class="chat-main">
      <header class="chat-topbar">
        <div>
          <strong>{{ isWelcome ? "新的志愿规划" : "湖南 2025 志愿方案" }}</strong>
          <small>湖南 · 2025 · 本科批</small>
        </div>
        <span class="session-badge">匿名演示会话</span>
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

      <div v-else class="thread-scroll">
        <div class="thread">
          <div
            v-for="message in chat.messages"
            :key="message.id"
            class="chat-message"
            :class="message.role"
          >
            <div class="avatar">{{ message.role === "user" ? "你" : "愿" }}</div>
            <div class="bubble">{{ message.text }}</div>
          </div>

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

          <div v-if="chat.stage === 'running'" class="chat-message assistant">
            <div class="avatar">愿</div>
            <div class="progress-card">
              <div class="progress-row"><span class="status-dot done"></span>画像信息已提交</div>
              <div class="progress-row"><span class="status-dot done"></span>正在核对选科与招生限制</div>
              <div class="progress-row"><span class="status-dot active"></span>正在计算冲稳保并生成报告</div>
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
                  v-for="(field, key) in chat.currentInterrupt.fields"
                  :key="key"
                  class="profile-field"
                >
                  <small>{{ fieldLabel(String(key)) }}</small>
                  <strong>{{ fieldValue(field) }}</strong>
                  <span>{{ field.state }}</span>
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

          <div v-if="chat.stage === 'done' && chat.run?.formalResult" class="chat-message assistant">
            <div class="avatar">愿</div>
            <div class="result-card">
              <div class="result-hero">
                <small>正式推荐报告 · {{ chat.run.formalResult.resultVersion }}</small>
                <h2>你的冲稳保方案已生成</h2>
                <p>
                  已通过 {{ chat.run.formalResult.verificationStatus }} 事实校验，
                  引用覆盖率 {{ Math.round(chat.run.formalResult.citationCoverage * 100) }}%。
                </p>
              </div>
              <div class="preview-list">
                <div v-for="candidate in candidateSections" :key="candidate.sectionKey" class="preview-row">
                  <span class="tier" :class="candidate.tier === '稳' ? 'stable' : candidate.tier === '保' ? 'safe' : ''">
                    {{ candidate.tier }}
                  </span>
                  <div>
                    <strong>{{ candidate.title.replace(/^\d+\.\s*/, "") }}</strong>
                    <small>计划数 {{ candidate.planCount }} · 已核验关键事实</small>
                  </div>
                  <span class="probability">{{ candidate.probability }}%</span>
                </div>
              </div>
              <div class="result-footer">
                <p>{{ chat.run.formalResult.riskNotice }}</p>
                <a class="primary-button" :href="`report.html?run=${chat.run.runId}`" target="_blank">
                  查看完整推荐报告 ↗
                </a>
              </div>
            </div>
          </div>

          <div v-if="chat.error" class="error-card">
            <strong>本次任务未完成</strong>
            <p>{{ chat.error }}</p>
            <button class="secondary-button" type="button" @click="chat.resetConversation">
              开始新对话
            </button>
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
          placeholder="继续回答问题，或补充你的偏好…"
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
