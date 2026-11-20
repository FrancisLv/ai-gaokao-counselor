import { computed, onMounted, onScopeDispose, reactive, ref } from "vue";

import {
  GatewayError,
  confirmProfile,
  createPlan,
  getRun,
} from "../api/gateway";
import type {
  CreatePlanReply,
  InterruptView,
  RunView,
  StructuredProfileForm,
} from "../api/types";

type Category = "物理类" | "历史类";
type Stage =
  | "welcome"
  | "category"
  | "score"
  | "subjects"
  | "preferences"
  | "running"
  | "confirm"
  | "done"
  | "failed";

type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
};

type HistoryEntry = {
  sessionId: string;
  runId: string;
  title: string;
  updatedAt: string;
};

const ALL_SUBJECTS = ["物理", "历史", "化学", "生物", "政治", "地理"];
const HISTORY_KEY = "counselor.chat.history";
const ACTIVE_RUN_KEY = "counselor.chat.activeRun";

export function useConversation() {
  const stage = ref<Stage>("welcome");
  const messages = ref<ChatMessage[]>([]);
  const prompt = ref("");
  const category = ref<Category | "">("");
  const score = ref<number | null>(null);
  const subjects = ref<string[]>([]);
  const cityPreferences = ref<string[]>(["长沙"]);
  const majorPreferences = ref<string[]>(["计算机", "电子信息"]);
  const riskPreference = ref("均衡");
  const receipt = ref<CreatePlanReply | null>(null);
  const run = ref<RunView | null>(null);
  const busy = ref(false);
  const error = ref("");
  const historyEntries = ref<HistoryEntry[]>([]);
  const currentInterrupt = computed(() => run.value?.currentInterrupt ?? null);
  const profileReady = computed(
    () =>
      category.value !== "" &&
      score.value !== null &&
      subjects.value.length === 3,
  );
  const profileFields = computed(() => ({
    province: "湖南",
    applicationYear: 2025,
    category: category.value || "待补充",
    batch: "本科批",
    score: score.value ?? "待补充",
    subjects: subjects.value.length
      ? subjects.value.join(" + ")
      : "待补充",
    city: cityPreferences.value.length
      ? cityPreferences.value.join("、")
      : "不限",
    major: majorPreferences.value.length
      ? majorPreferences.value.join("、")
      : "暂未确定",
    risk: riskPreference.value,
  }));

  let pollTimer: number | null = null;
  let submitKey = newKey("submit");
  let confirmKey = newKey("confirm");
  let lastInterruptId = "";

  function newKey(prefix: string): string {
    return `${prefix}-${crypto.randomUUID()}`;
  }

  function addMessage(role: ChatMessage["role"], text: string): void {
    messages.value.push({ id: newKey("message"), role, text });
  }

  function startConversation(initialText = ""): void {
    if (stage.value !== "welcome") return;
    history.replaceState({}, "", location.pathname);
    stage.value = "category";
    addMessage(
      "assistant",
      "你好，我会一步步了解你的分数、选科和偏好，再生成一份可查看的志愿报告。先确认：你是物理类还是历史类？",
    );
    if (initialText.trim()) {
      prompt.value = initialText.trim();
      void sendPrompt();
    }
  }

  async function sendPrompt(): Promise<void> {
    const text = prompt.value.trim();
    if (!text || busy.value) return;
    prompt.value = "";
    addMessage("user", text);
    const parsed = parseProfileText(text);
    if (parsed.category) category.value = parsed.category;
    if (parsed.score !== null) score.value = parsed.score;
    if (parsed.subjects.length) subjects.value = parsed.subjects;
    advanceCollection();
  }

  function chooseCategory(value: Category): void {
    category.value = value;
    addMessage("user", value);
    advanceCollection();
  }

  function submitScore(): void {
    if (score.value === null || score.value < 0 || score.value > 750) {
      error.value = "请输入 0–750 之间的高考分数。";
      return;
    }
    error.value = "";
    addMessage("user", `${score.value} 分`);
    advanceCollection();
  }

  function toggleSubject(subject: string): void {
    if (subjects.value.includes(subject)) {
      subjects.value = subjects.value.filter((item) => item !== subject);
      return;
    }
    if (subjects.value.length < 3) subjects.value = [...subjects.value, subject];
  }

  function submitSubjects(): void {
    if (subjects.value.length !== 3) {
      error.value = "请选择一门首选科目和两门再选科目。";
      return;
    }
    error.value = "";
    addMessage("user", `选科：${subjects.value.join(" + ")}`);
    stage.value = "preferences";
    addMessage(
      "assistant",
      "最后补充一下偏好。它们用于解释和筛选；算法给出的概率和候选身份不会由模型改写。",
    );
  }

  function togglePreference(
    target: "city" | "major",
    value: string,
  ): void {
    const current =
      target === "city" ? cityPreferences : majorPreferences;
    current.value = current.value.includes(value)
      ? current.value.filter((item) => item !== value)
      : [...current.value, value];
  }

  function advanceCollection(): void {
    if (!category.value) {
      stage.value = "category";
      addMessage("assistant", "先告诉我你是物理类还是历史类。");
      return;
    }
    if (score.value === null) {
      stage.value = "score";
      addMessage("assistant", "你的高考分数是多少？我会按湖南 2025 本科批处理。");
      return;
    }
    if (subjects.value.length !== 3) {
      stage.value = "subjects";
      addMessage("assistant", "再选择你的 3+1+2 选科组合，正好选择 3 门。");
      return;
    }
    if (stage.value !== "preferences") {
      stage.value = "preferences";
      addMessage(
        "assistant",
        "基本信息已经齐了。再补充城市、专业兴趣和风险偏好，我就开始整理画像。",
      );
    }
  }

  async function finishPreferences(): Promise<void> {
    if (!profileReady.value || busy.value) return;
    addMessage("user", "按当前偏好生成方案");
    stage.value = "running";
    busy.value = true;
    error.value = "";
    const profile: StructuredProfileForm = {
      province: "湖南",
      applicationYear: 2025,
      category: category.value as Category,
      batch: "本科批",
      score: score.value as number,
      subjects: [...subjects.value],
    };
    try {
      receipt.value = await createPlan(profile, submitKey);
      rememberRun(receipt.value, profile);
      schedulePoll(receipt.value.runId, 0);
    } catch (cause) {
      failWith(cause);
    } finally {
      busy.value = false;
    }
  }

  async function refreshRun(runId = receipt.value?.runId): Promise<void> {
    if (!runId) return;
    try {
      run.value = await getRun(runId);
      if (
        run.value.currentInterrupt &&
        run.value.currentInterrupt.interruptId !== lastInterruptId
      ) {
        lastInterruptId = run.value.currentInterrupt.interruptId;
        stage.value = "confirm";
        addMessage(
          "assistant",
          "画像已整理完成。请确认下面的信息，确认后我会继续核验招生要求并生成正式报告。",
        );
      }
      if (run.value.state === "SUCCEEDED") {
        clearPoll();
        stage.value = "done";
        addMessage("assistant", "正式推荐报告已经生成，可以打开静态报告查看。");
      } else if (
        run.value.state === "FAILED" ||
        run.value.state === "EXPIRED" ||
        run.value.state === "CANCELLED"
      ) {
        clearPoll();
        stage.value = "failed";
        error.value =
          run.value.terminalReason ||
          run.value.state ||
          "任务未完成";
      }
    } catch (cause) {
      clearPoll();
      failWith(cause);
    }
  }

  async function confirmProfileCard(): Promise<void> {
    if (
      busy.value ||
      !run.value ||
      !currentInterrupt.value?.profileVersion
    ) {
      return;
    }
    busy.value = true;
    error.value = "";
    try {
      await confirmProfile(run.value.sessionId, {
        runId: run.value.runId,
        interruptId: currentInterrupt.value.interruptId,
        expectedStateVersion: Number(run.value.stateVersion),
        profileVersion: currentInterrupt.value.profileVersion,
        idempotencyKey: confirmKey,
      });
      addMessage("user", "确认画像");
      addMessage(
        "assistant",
        "已确认。我正在核验招生计划、选科和关键限制，完成后会生成静态报告。",
      );
      stage.value = "running";
      schedulePoll(run.value.runId, 0);
    } catch (cause) {
      failWith(cause);
    } finally {
      busy.value = false;
    }
  }

  function resetConversation(): void {
    clearPoll();
    stage.value = "welcome";
    messages.value = [];
    prompt.value = "";
    category.value = "";
    score.value = null;
    subjects.value = [];
    cityPreferences.value = ["长沙"];
    majorPreferences.value = ["计算机", "电子信息"];
    riskPreference.value = "均衡";
    receipt.value = null;
    run.value = null;
    error.value = "";
    busy.value = false;
    submitKey = newKey("submit");
    confirmKey = newKey("confirm");
    lastInterruptId = "";
    sessionStorage.removeItem(ACTIVE_RUN_KEY);
    history.replaceState({}, "", location.pathname);
  }

  function openHistory(entry: HistoryEntry): void {
    clearPoll();
    receipt.value = {
      runId: entry.runId,
      sessionId: entry.sessionId,
      status: "accepted",
    };
    stage.value = "running";
    messages.value = [
      {
        id: newKey("message"),
        role: "assistant",
        text: `正在恢复对话：${entry.title}`,
      },
    ];
    const url = new URL(location.href);
    url.searchParams.set("run", entry.runId);
    history.replaceState({}, "", url);
    void refreshRun(entry.runId);
  }

  function failWith(cause: unknown): void {
    clearPoll();
    stage.value = "failed";
    error.value = errorMessage(cause);
    busy.value = false;
  }

  function schedulePoll(runId: string, delay = 650): void {
    clearPoll();
    pollTimer = window.setTimeout(async () => {
      await refreshRun(runId);
      if (stage.value === "running" || stage.value === "confirm") {
        schedulePoll(runId);
      }
    }, delay);
  }

  function clearPoll(): void {
    if (pollTimer !== null) {
      window.clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  function rememberRun(
    current: CreatePlanReply,
    profile: StructuredProfileForm,
  ): void {
    sessionStorage.setItem(ACTIVE_RUN_KEY, current.runId);
    const entry: HistoryEntry = {
      sessionId: current.sessionId,
      runId: current.runId,
      title: `${profile.category} ${profile.score} 分志愿方案`,
      updatedAt: new Date().toISOString(),
    };
    historyEntries.value = [
      entry,
      ...historyEntries.value.filter((item) => item.sessionId !== entry.sessionId),
    ].slice(0, 12);
    localStorage.setItem(HISTORY_KEY, JSON.stringify(historyEntries.value));
    const url = new URL(location.href);
    url.searchParams.set("run", current.runId);
    history.replaceState({}, "", url);
  }

  function loadHistory(): void {
    try {
      const parsed = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
      historyEntries.value = Array.isArray(parsed) ? parsed : [];
    } catch {
      historyEntries.value = [];
    }
  }

  async function restore(): Promise<void> {
    loadHistory();
    const runId =
      new URL(location.href).searchParams.get("run") ||
      sessionStorage.getItem(ACTIVE_RUN_KEY);
    if (!runId) return;
    stage.value = "running";
    receipt.value = {
      runId,
      sessionId: "",
      status: "accepted",
    };
    await refreshRun(runId);
  }

  onMounted(() => {
    void restore();
  });
  onScopeDispose(clearPoll);

  return reactive({
    stage,
    messages,
    prompt,
    category,
    score,
    subjects,
    cityPreferences,
    majorPreferences,
    riskPreference,
    receipt,
    run,
    busy,
    error,
    historyEntries,
    currentInterrupt,
    profileReady,
    profileFields,
    allSubjects: ALL_SUBJECTS,
    startConversation,
    sendPrompt,
    chooseCategory,
    submitScore,
    toggleSubject,
    submitSubjects,
    togglePreference,
    finishPreferences,
    confirmProfileCard,
    resetConversation,
    openHistory,
    refreshRun,
  });
}

function parseProfileText(text: string): {
  category: Category | null;
  score: number | null;
  subjects: string[];
} {
  const category = text.includes("历史")
    ? "历史类"
    : text.includes("物理")
      ? "物理类"
      : null;
  const scoreMatch = text.match(/(?:^|\D)(\d{2,3})(?:\s*分|\D|$)/);
  const parsedScore = scoreMatch ? Number(scoreMatch[1]) : null;
  const score =
    parsedScore !== null && parsedScore >= 0 && parsedScore <= 750
      ? parsedScore
      : null;
  const directCombination: Record<string, string[]> = {
    物化生: ["物理", "化学", "生物"],
    物化政: ["物理", "化学", "政治"],
    物化地: ["物理", "化学", "地理"],
    史政地: ["历史", "政治", "地理"],
    史政生: ["历史", "政治", "生物"],
    史地生: ["历史", "地理", "生物"],
  };
  let subjects: string[] = [];
  for (const [key, value] of Object.entries(directCombination)) {
    if (text.includes(key)) {
      subjects = value;
      break;
    }
  }
  if (subjects.length === 0) {
    subjects = ALL_SUBJECTS.filter((subject) => text.includes(subject));
  }
  return { category, score, subjects: subjects.slice(0, 3) };
}

function errorMessage(cause: unknown): string {
  if (cause instanceof GatewayError) {
    if (cause.status === 401) {
      return "当前演示缺少可信 principal，请配置 VITE_PRINCIPAL_ID。";
    }
    return `${cause.status}: ${cause.message}`;
  }
  return cause instanceof Error ? cause.message : String(cause);
}

export type ConversationState = ReturnType<typeof useConversation>;
export type { HistoryEntry, InterruptView };
