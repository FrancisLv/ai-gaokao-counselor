import { computed, onMounted, onScopeDispose, reactive, ref } from "vue";

import {
  GatewayError,
  confirmProfile,
  createPlan,
  getSession,
  getRun,
  listSessions,
  sendConversationMessage,
  submitRegionAnswer,
} from "../api/gateway";
import type {
  ChatMessageView,
  CreatePlanReply,
  InterruptView,
  RegionAnswer,
  RegionClarification,
  RunView,
  SessionRun,
  SessionSummary,
  StructuredProfileForm,
} from "../api/types";

type Category = "物理类" | "历史类";
type Stage =
  | "welcome"
  | "category"
  | "score"
  | "subjects"
  | "preferences"
  | "region"
  | "running"
  | "confirm"
  | "done"
  | "failed";

type ChatMessage = {
  id: string;
  runId?: string;
  role: "user" | "assistant";
  text: string;
  kind: "TEXT" | "PROGRESS" | "RESULT" | "ERROR";
};

const ALL_SUBJECTS = ["物理", "历史", "化学", "生物", "政治", "地理"];
const PHYSICS_RELATED_MAJORS = [
  "物理学",
  "物理学类",
  "应用物理学",
  "核物理",
  "材料物理",
  "地球物理学",
  "工程物理",
];
const ACTIVE_RUN_KEY = "counselor.chat.activeRun";
const CRISIS_PATTERN =
  /自杀|轻生|不想活|结束生命|伤害自己|伤害他人|杀人|有生命危险|活不下去/;
const OUT_OF_SCOPE_PATTERN =
  /考研|研究生|留学|国外读研|艺术类|体育类|专科|北京高考|上海高考/;
const OFF_TOPIC_PATTERN =
  /天气|新闻|股票|Python|编程|游戏|电影|笑话|菜谱|彩票/;

export function useConversation() {
  const stage = ref<Stage>("welcome");
  const messages = ref<ChatMessage[]>([]);
  const prompt = ref("");
  const category = ref<Category | "">("");
  const score = ref<number | null>(null);
  const subjects = ref<string[]>([]);
  const cityPreferences = ref<string[]>(["长沙"]);
  const majorPreferences = ref<string[]>([]);
  const riskPreference = ref("均衡");
  const requiredProvinces = ref<string[]>([]);
  const excludedProvinces = ref<string[]>([]);
  const requiredCities = ref<string[]>([]);
  const excludedCities = ref<string[]>([]);
  const requiredTiers = ref<string[]>([]);
  const requiredMajorNames = ref<string[]>([]);
  const requiredMajorKeywords = ref<string[]>([]);
  const excludedMajorKeywords = ref<string[]>([]);
  const excludedMajorNames = ref<string[]>([]);
  const excludedUniversities = ref<string[]>([]);
  const sessionId = ref("");
  const receipt = ref<CreatePlanReply | null>(null);
  const run = ref<RunView | null>(null);
  const reportRun = ref<RunView | null>(null);
  const sessionRuns = ref<SessionRun[]>([]);
  const regionClarification = ref<RegionClarification | null>(null);
  const selectedRegionPresetIds = ref<string[]>([]);
  const selectedRegionProvinces = ref<string[]>([]);
  const regionMode = ref<"initial" | "report" | null>(null);
  const busy = ref(false);
  const error = ref("");
  const historyEntries = ref<SessionSummary[]>([]);
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
  let regionKey = newKey("region");
  let lastInterruptId = "";

  function newKey(prefix: string): string {
    return `${prefix}-${crypto.randomUUID()}`;
  }

  function addMessage(
    role: ChatMessage["role"],
    text: string,
    kind: ChatMessage["kind"] = "TEXT",
  ): void {
    messages.value.push({ id: newKey("message"), role, text, kind });
  }

  function currentProfile(): StructuredProfileForm {
    return {
      province: "湖南",
      applicationYear: 2025,
      category: category.value as Category,
      batch: "本科批",
      score: score.value as number,
      subjects: [...subjects.value],
      riskAppetite:
        riskPreference.value === "偏稳妥"
          ? "conservative"
          : riskPreference.value === "偏冲刺"
            ? "aggressive"
            : "balanced",
      priorityDims: ["major", "location"],
      interests: [...majorPreferences.value],
      preferredCities: cityPreferences.value.filter(
        (city) => city !== "不限城市",
      ),
      preferredTiers: [],
      requiredProvinces: [...requiredProvinces.value],
      excludedProvinces: [...excludedProvinces.value],
      requiredCities: [...requiredCities.value],
      excludedCities: [...excludedCities.value],
      requiredTiers: [...requiredTiers.value],
      requiredMajorNames: [...requiredMajorNames.value],
      requiredMajorKeywords: [...requiredMajorKeywords.value],
      excludedMajorKeywords: [...excludedMajorKeywords.value],
      excludedMajorNames: [...excludedMajorNames.value],
      excludedUniversities: [...excludedUniversities.value],
    };
  }

  function toChatMessages(items: ChatMessageView[]): ChatMessage[] {
    return items
      .filter((item) => item.role !== "SYSTEM")
      .map((item) => ({
        id: item.messageId,
        runId: item.runId,
        role: item.role === "USER" ? "user" : "assistant",
        text: item.content,
        kind: item.kind,
      }));
  }

  function restoreProfile(
    fields: Record<
      string,
      { stringValue?: string; intValue?: number; stringList?: { values: string[] } }
    >,
  ): void {
    const text = (key: string): string =>
      String(fields[key]?.stringValue ?? "");
    const integer = (key: string): number | null =>
      fields[key]?.intValue === undefined
        ? null
        : Number(fields[key].intValue);
    const list = (key: string): string[] =>
      fields[key]?.stringList?.values ?? [];
    const hasList = (key: string): boolean =>
      fields[key]?.stringList !== undefined;

    const restoredCategory = text("category");
    if (restoredCategory === "物理类" || restoredCategory === "历史类") {
      category.value = restoredCategory;
    }
    const restoredScore = integer("score");
    if (restoredScore !== null) score.value = restoredScore;
    const restoredSubjects = list("subjects");
    if (restoredSubjects.length) subjects.value = restoredSubjects;
    const restoredRisk = text("preferences.risk_appetite");
    riskPreference.value =
      restoredRisk === "conservative"
        ? "偏稳妥"
        : restoredRisk === "aggressive"
          ? "偏冲刺"
          : "均衡";
    const restoredInterests = list("preferences.major_interests");
    if (restoredInterests.length) majorPreferences.value = restoredInterests;
    const restoredCities = list("preferences.preferred_cities");
    if (hasList("preferences.preferred_cities")) {
      cityPreferences.value = restoredCities;
    }
    requiredProvinces.value = list("preferences.required_provinces");
    excludedProvinces.value = list("preferences.excluded_provinces");
    requiredCities.value = list("preferences.required_cities");
    excludedCities.value = list("preferences.excluded_cities");
    requiredTiers.value = list("preferences.required_tiers");
    requiredMajorNames.value = list(
      "preferences.required_major_names",
    );
    requiredMajorKeywords.value = list(
      "preferences.required_major_keywords",
    );
    excludedMajorKeywords.value = list(
      "preferences.excluded_major_keywords",
    );
    excludedMajorNames.value = list(
      "preferences.excluded_major_names",
    );
    excludedUniversities.value = list(
      "preferences.excluded_universities",
    );
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
    if (reportRun.value?.formalResult && sessionId.value) {
      await continueConversation(text);
      return;
    }
    if (CRISIS_PATTERN.test(text)) {
      addMessage(
        "assistant",
        "听起来你现在可能处在很危险的状态。请先确保自己安全，立即联系身边可信赖的人，并联系当地急救或心理援助。在中国大陆可拨打 120 或 110。你可以告诉我现在是否安全、身边是否有人；志愿填报问题可以稍后再处理。",
      );
      return;
    }
    if (OUT_OF_SCOPE_PATTERN.test(text)) {
      addMessage(
        "assistant",
        "当前仅支持湖南 2025 本科批物理类/历史类志愿规划，其他省份、年份、升学类型或艺术体育等范围还不能可靠处理。你可以继续问本范围内的院校专业、录取规则，或调整当前方案。",
      );
      advanceCollection();
      return;
    }
    if (OFF_TOPIC_PATTERN.test(text)) {
      addMessage(
        "assistant",
        "我主要负责湖南 2025 本科批的志愿规划、院校专业和报告解释，不能可靠回答这个问题。你可以继续问录取规则、专业方向，或让我调整当前志愿方案。",
      );
      advanceCollection();
      return;
    }
    const parsed = parseProfileText(text);
    if (parsed.category) category.value = parsed.category;
    if (parsed.score !== null) score.value = parsed.score;
    if (parsed.subjects.length) subjects.value = parsed.subjects;
    if (parsed.interests.length) {
      majorPreferences.value = parsed.interests;
      const categoryNote = parsed.interests.some((major) =>
        major.includes("物理"),
      )
        ? "这不等于“物理类”。"
        : "";
      addMessage(
        "assistant",
        `我理解为你想读本科专业“${parsed.interests.join("、")}”。${categoryNote}你是否也接受相关专业？`,
      );
    }
    if (parsed.requiredMajorNames.length) {
      requiredMajorNames.value = parsed.requiredMajorNames;
    }
    if (
      majorPreferences.value.includes("物理学") &&
      /(?:也|可以)?接受\s*(?:物理)?相关专业/.test(text)
    ) {
      majorPreferences.value = PHYSICS_RELATED_MAJORS;
      requiredMajorNames.value = [];
      addMessage("assistant", "已记录：接受物理学及相关专业，仍按软兴趣参与排序。");
    }
    advanceCollection();
  }

  async function continueConversation(text: string): Promise<void> {
    if (!reportRun.value?.formalResult || !sessionId.value) return;
    busy.value = true;
    error.value = "";
    const key = newKey("conversation");
    try {
      const reply = await sendConversationMessage(
        sessionId.value,
        currentProfile(),
        reportRun.value.formalResult.resultVersion,
        text,
        key,
      );
      sessionId.value = reply.sessionId;
      const url = new URL(location.href);
      url.searchParams.set("session", reply.sessionId);
      if (reply.runId) url.searchParams.set("run", reply.runId);
      else url.searchParams.delete("run");
      history.replaceState({}, "", url);
      if (reply.assistantMessage) {
        addMessage("assistant", reply.assistantMessage);
      }
      if (reply.regionClarification) {
        beginRegionClarification(reply.regionClarification, "report");
        stage.value = "region";
        return;
      }
      if (!reply.runId) {
        stage.value = "done";
        await syncSession();
        return;
      }
      receipt.value = {
        runId: reply.runId,
        sessionId: reply.sessionId,
        status: reply.status,
      };
      run.value = null;
      lastInterruptId = "";
      stage.value = "running";
      schedulePoll(reply.runId, 0);
    } catch (cause) {
      stage.value = "failed";
      error.value = errorMessage(cause);
    } finally {
      busy.value = false;
    }
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
    if (target === "major" && value === "还没想好") {
      majorPreferences.value = [];
      requiredMajorNames.value = [];
      return;
    }
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
    const profile = currentProfile();
    try {
      receipt.value = await createPlan(
        profile,
        submitKey,
        messages.value.map((message) => ({
          messageId: message.id,
          role: message.role === "user" ? "USER" : "ASSISTANT",
          kind: message.kind,
          content: message.text,
        })),
      );
      sessionId.value = receipt.value.sessionId;
      rememberRun(receipt.value, profile);
      schedulePoll(receipt.value.runId, 0);
      await loadHistory();
    } catch (cause) {
      failWith(cause);
    } finally {
      busy.value = false;
    }
  }

  async function refreshRun(runId = receipt.value?.runId): Promise<void> {
    if (!runId) return;
    try {
      const nextRun = await getRun(runId);
      run.value = nextRun;
      if (nextRun.sessionId) {
        sessionId.value = nextRun.sessionId;
      }
      if (nextRun.formalResult) {
        reportRun.value = nextRun;
      }
      if (
        nextRun.currentInterrupt &&
        nextRun.currentInterrupt.interruptId !== lastInterruptId
      ) {
        lastInterruptId = nextRun.currentInterrupt.interruptId;
        if (nextRun.currentInterrupt.regionClarification) {
          beginRegionClarification(
            nextRun.currentInterrupt.regionClarification,
            "initial",
          );
          stage.value = "region";
          return;
        }
        stage.value = "confirm";
        addMessage(
          "assistant",
          "画像已整理完成。请确认下面的信息，确认后我会继续核验招生要求并生成正式报告。",
        );
      }
      if (nextRun.state === "SUCCEEDED") {
        clearPoll();
        stage.value = "done";
        await syncSession();
        await loadHistory();
      } else if (nextRun.state === "RUNNING" || nextRun.state === "QUEUED") {
        stage.value = "running";
      } else if (
        nextRun.state === "FAILED" ||
        nextRun.state === "EXPIRED" ||
        nextRun.state === "CANCELLED"
      ) {
        clearPoll();
        stage.value = "failed";
        error.value = terminalErrorMessage(
          nextRun.terminalReason,
          nextRun.state,
        );
        await syncSession();
      }
    } catch (cause) {
      clearPoll();
      failWith(cause);
    }
  }

  async function syncSession(): Promise<void> {
    if (!sessionId.value) return;
    const detail = await getSession(sessionId.value);
    sessionRuns.value = detail.runs;
    restoreProfile(detail.profileFields);
    if (detail.messages.length) {
      messages.value = toChatMessages(detail.messages);
    }
    const latestFormal = [...detail.runs]
      .reverse()
      .find(
        (item) =>
          item.resultKind === "V1_FORMAL" &&
          item.state === "SUCCEEDED" &&
          item.resultVersion,
      );
    if (
      latestFormal &&
      reportRun.value?.runId !== latestFormal.runId
    ) {
      reportRun.value = await getRun(latestFormal.runId);
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
    majorPreferences.value = [];
    riskPreference.value = "均衡";
    requiredCities.value = [];
    excludedCities.value = [];
    requiredProvinces.value = [];
    excludedProvinces.value = [];
    requiredTiers.value = [];
    requiredMajorNames.value = [];
    requiredMajorKeywords.value = [];
    excludedMajorKeywords.value = [];
    excludedMajorNames.value = [];
    excludedUniversities.value = [];
    sessionId.value = "";
    receipt.value = null;
    run.value = null;
    reportRun.value = null;
    sessionRuns.value = [];
    regionClarification.value = null;
    selectedRegionPresetIds.value = [];
    selectedRegionProvinces.value = [];
    regionMode.value = null;
    error.value = "";
    busy.value = false;
    submitKey = newKey("submit");
    confirmKey = newKey("confirm");
    regionKey = newKey("region");
    lastInterruptId = "";
    sessionStorage.removeItem(ACTIVE_RUN_KEY);
    history.replaceState({}, "", location.pathname);
  }

  function beginRegionClarification(
    clarification: RegionClarification,
    mode: "initial" | "report",
  ): void {
    if (
      regionClarification.value?.clarificationId ===
        clarification.clarificationId &&
      regionMode.value === mode
    ) {
      return;
    }
    regionClarification.value = clarification;
    regionMode.value = mode;
    selectedRegionPresetIds.value = [];
    selectedRegionProvinces.value = [];
  }

  function toggleRegionPreset(presetId: string): void {
    const clarification = regionClarification.value;
    const preset = clarification?.presets.find(
      (item) => item.id === presetId,
    );
    if (!clarification || !preset) return;
    const selected = new Set(selectedRegionProvinces.value);
    if (selectedRegionPresetIds.value.includes(presetId)) {
      selectedRegionPresetIds.value = selectedRegionPresetIds.value.filter(
        (item) => item !== presetId,
      );
      const coveredElsewhere = new Set(
        clarification.presets
          .filter((item) =>
            selectedRegionPresetIds.value.includes(item.id),
          )
          .flatMap((item) => item.provinces),
      );
      preset.provinces.forEach((province) => {
        if (!coveredElsewhere.has(province)) selected.delete(province);
      });
    } else {
      selectedRegionPresetIds.value = [
        ...selectedRegionPresetIds.value,
        presetId,
      ];
      preset.provinces.forEach((province) => selected.add(province));
    }
    selectedRegionProvinces.value = clarification.provinces.filter(
      (province) => selected.has(province),
    );
  }

  function toggleRegionProvince(province: string): void {
    const clarification = regionClarification.value;
    if (!clarification) return;
    const selected = new Set(selectedRegionProvinces.value);
    if (selected.has(province)) selected.delete(province);
    else selected.add(province);
    selectedRegionProvinces.value = clarification.provinces.filter(
      (item) => selected.has(item),
    );
    selectedRegionPresetIds.value = clarification.presets
      .filter((preset) =>
        preset.provinces.every((item) => selected.has(item)),
      )
      .map((preset) => preset.id);
  }

  async function submitRegionClarification(
    cancelRegionRestriction = false,
  ): Promise<void> {
    const clarification = regionClarification.value;
    if (!clarification || busy.value) return;
    if (!cancelRegionRestriction && !selectedRegionProvinces.value.length) {
      error.value = "请至少选择一个省份，或明确取消该地域限制。";
      return;
    }
    const answer: RegionAnswer = {
      clarificationId: clarification.clarificationId,
      provinces: cancelRegionRestriction
        ? []
        : [...selectedRegionProvinces.value],
      presetIds: cancelRegionRestriction
        ? []
        : [...selectedRegionPresetIds.value],
      catalogVersion: clarification.catalogVersion,
      cancelRegionRestriction,
    };
    const selectedText =
      clarification.operation === "require"
        ? `只看以下省份：${answer.provinces.join("、")}`
        : clarification.operation === "withdraw"
          ? `以下省份也可以：${answer.provinces.join("、")}`
          : `确认排除以下省份：${answer.provinces.join("、")}`;
    busy.value = true;
    error.value = "";
    try {
      if (regionMode.value === "initial") {
        if (
          !run.value ||
          !currentInterrupt.value?.profileVersion
        ) {
          throw new Error("地域澄清状态已失效，请刷新后重试。");
        }
        await submitRegionAnswer(run.value.sessionId, {
          runId: run.value.runId,
          interruptId: currentInterrupt.value.interruptId,
          expectedStateVersion: Number(run.value.stateVersion),
          profileVersion: currentInterrupt.value.profileVersion,
          idempotencyKey: regionKey,
          answer,
        });
        addMessage(
          "user",
          cancelRegionRestriction
            ? "先不管这个地域限制"
            : selectedText,
        );
        regionClarification.value = null;
        regionMode.value = null;
        selectedRegionPresetIds.value = [];
        selectedRegionProvinces.value = [];
        stage.value = "running";
        schedulePoll(run.value.runId, 0);
        return;
      }
      const formalRun = reportRun.value;
      if (!formalRun?.formalResult || !sessionId.value) {
        throw new Error("当前报告已失效，请重新打开后再试。");
      }
      const text = cancelRegionRestriction
        ? "先不管这个地域限制"
        : selectedText;
      addMessage("user", text);
      const reply = await sendConversationMessage(
        sessionId.value,
        currentProfile(),
        formalRun.formalResult.resultVersion,
        text,
        regionKey,
        answer,
      );
      addMessage("assistant", reply.assistantMessage);
      regionClarification.value = null;
      regionMode.value = null;
      selectedRegionPresetIds.value = [];
      selectedRegionProvinces.value = [];
      regionKey = newKey("region");
      if (!reply.runId) {
        stage.value = "done";
        await syncSession();
        return;
      }
      receipt.value = {
        runId: reply.runId,
        sessionId: reply.sessionId,
        status: reply.status,
      };
      run.value = null;
      lastInterruptId = "";
      stage.value = "running";
      schedulePoll(reply.runId, 0);
    } catch (cause) {
      failWith(cause);
    } finally {
      busy.value = false;
    }
  }

  async function openHistory(entry: SessionSummary): Promise<void> {
    clearPoll();
    busy.value = true;
    error.value = "";
    sessionId.value = entry.sessionId;
    const url = new URL(location.href);
    url.searchParams.set("session", entry.sessionId);
    if (entry.latestRunId) url.searchParams.set("run", entry.latestRunId);
    else url.searchParams.delete("run");
    history.replaceState({}, "", url);
    try {
      const detail = await getSession(entry.sessionId);
      sessionRuns.value = detail.runs;
      messages.value = toChatMessages(detail.messages);
      restoreProfile(detail.profileFields);
      reportRun.value = null;

      const formalRuns = detail.runs.filter(
        (item) =>
          item.resultKind === "V1_FORMAL" &&
          item.state === "SUCCEEDED" &&
          item.resultVersion,
      );
      const latestFormal = formalRuns.at(-1);
      if (latestFormal) {
        reportRun.value = await getRun(latestFormal.runId);
      }
      const latestRun =
        detail.runs.find((item) => item.runId === entry.latestRunId) ??
        detail.runs.at(-1);
      if (!latestRun) {
        stage.value = "welcome";
        return;
      }
      receipt.value = {
        runId: latestRun.runId,
        sessionId: entry.sessionId,
        status: "accepted",
      };
      if (
        latestRun.state === "QUEUED" ||
        latestRun.state === "RUNNING" ||
        latestRun.state === "WAITING_HITL"
      ) {
        stage.value = "running";
        await refreshRun(latestRun.runId);
        if (
          stage.value === "running" ||
          stage.value === "confirm"
        ) {
          schedulePoll(latestRun.runId, 0);
        }
      } else if (latestRun.state === "SUCCEEDED") {
        stage.value = "done";
        await refreshRun(latestRun.runId);
      } else {
        if (run.value?.runId === latestRun.runId) {
          stage.value = "failed";
          error.value = terminalErrorMessage(
            run.value.terminalReason,
            run.value.state,
          );
        } else {
          await refreshRun(latestRun.runId);
        }
      }
    } catch (cause) {
      failWith(cause);
    } finally {
      busy.value = false;
    }
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
    const url = new URL(location.href);
    url.searchParams.set("session", current.sessionId);
    url.searchParams.set("run", current.runId);
    history.replaceState({}, "", url);
  }

  async function loadHistory(): Promise<void> {
    try {
      historyEntries.value = await listSessions();
    } catch (cause) {
      if (historyEntries.value.length === 0) {
        error.value = errorMessage(cause);
      }
    }
  }

  async function restore(): Promise<void> {
    await loadHistory();
    const runId =
      new URL(location.href).searchParams.get("run") ||
      sessionStorage.getItem(ACTIVE_RUN_KEY);
    const sessionFromUrl = new URL(location.href).searchParams.get("session");
    if (!runId && !sessionFromUrl) return;
    stage.value = "running";
    if (runId) {
      receipt.value = {
        runId,
        sessionId: sessionFromUrl || "",
        status: "accepted",
      };
    }
    if (sessionFromUrl) {
      const entry =
        historyEntries.value.find(
          (item) => item.sessionId === sessionFromUrl,
        ) ??
        ({
          sessionId: sessionFromUrl,
          title: "正在恢复对话",
          latestRunId: runId ?? undefined,
          latestState: "RUNNING",
          runCount: 0,
        } satisfies SessionSummary);
      await openHistory(entry);
      return;
    }
    if (!runId) return;
    const current = await getRun(runId);
    const entry =
      historyEntries.value.find(
        (item) => item.sessionId === current.sessionId,
      ) ??
      ({
        sessionId: current.sessionId,
        title: "正在恢复对话",
        latestRunId: runId,
        latestState: current.state,
        runCount: 1,
      } satisfies SessionSummary);
    await openHistory(entry);
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
    requiredCities,
    excludedCities,
    requiredProvinces,
    excludedProvinces,
    requiredTiers,
    requiredMajorNames,
    requiredMajorKeywords,
    excludedMajorKeywords,
    excludedMajorNames,
    excludedUniversities,
    sessionId,
    receipt,
    run,
    reportRun,
    sessionRuns,
    regionClarification,
    selectedRegionPresetIds,
    selectedRegionProvinces,
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
    toggleRegionPreset,
    toggleRegionProvince,
    submitRegionClarification,
    resetConversation,
    openHistory,
    refreshRun,
  });
}

function parseProfileText(text: string): {
  category: Category | null;
  score: number | null;
  subjects: string[];
  interests: string[];
  requiredMajorNames: string[];
} {
  const category = /(?:历史类|首选历史)/.test(text)
    ? "历史类"
    : /(?:物理类|首选物理)/.test(text)
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
  if (
    subjects.length === 0 &&
    /(?:选科|选的是|选了|组合|首选|再选)/.test(text)
  ) {
    subjects = ALL_SUBJECTS.filter((subject) => text.includes(subject));
  }
  const interests: string[] = [];
  const exactOnly = /只看|只想|想看|只学|只读|只考虑|仅限/.test(text);
  if (
    /想学|想读|想报|喜欢|感兴趣|兴趣|优先/.test(text) ||
    exactOnly
  ) {
    for (const major of [
      ...PHYSICS_RELATED_MAJORS,
      "康复物理治疗",
      "地球物理学类",
      "计算机科学与技术",
      "电子信息工程",
      "自动化",
    ]) {
      if (text.includes(major)) interests.push(major);
    }
    if (interests.length === 0 && text.includes("计算机")) {
      interests.push("计算机科学与技术");
    }
    if (interests.length === 0 && text.includes("电子信息")) {
      interests.push("电子信息工程");
    }
    if (interests.includes("物理")) {
      interests.splice(
        interests.indexOf("物理"),
        1,
        "物理学",
      );
    }
  }
  return {
    category,
    score,
    subjects: subjects.slice(0, 3),
    interests,
    requiredMajorNames: exactOnly ? interests : [],
  };
}

function errorMessage(cause: unknown): string {
  if (cause instanceof GatewayError) {
    if (cause.status === 401) {
      return "登录已失效，请重新登录。";
    }
    return `${cause.status}: ${cause.message}`;
  }
  return cause instanceof Error ? cause.message : String(cause);
}

function terminalErrorMessage(
  reason: string | undefined,
  state: string,
): string {
  switch (reason) {
    case "SCORE_BELOW_CONTROL_LINE":
      return "分数低于湖南 2025 本科批控制线。当前版本仅支持本科批，请调整分数后重新开始。";
    case "SCORE_ABOVE_MAX":
      return "分数超过 750 分上限，请检查输入后重新开始。";
    case "INVALID_SUBJECTS":
      return "选科组合与科类不一致，请重新选择一门首选和两门再选科目。";
    case "NO_RELIABLE_DATA":
      return "当前条件下没有可靠的历史数据，无法生成可核验方案。";
    case "TARGET_ALLOCATION_UNSATISFIED":
      return "当前画像下没有更多目标档候选，已保留原报告。请放宽城市、专业或其他硬筛选后重试。";
    case "VALIDATION_FAILED":
      return "结果未通过安全校验，本次不会展示未经核验的志愿方案。";
    case "DEPENDENCY_UNAVAILABLE":
      return "算法或数据处理服务暂时不可用，请稍后重新尝试。";
    case "RELEASE_REVOKED":
      return "本次使用的数据版本已撤销，请重新开始生成。";
    case "HITL_TIMEOUT":
      return "画像确认已超时，请重新开始生成。";
    default:
      return reason
        ? `任务未完成（${reason}）`
        : state === "CANCELLED"
          ? "任务已取消。"
          : "任务未完成，请重新开始。";
  }
}

export type ConversationState = ReturnType<typeof useConversation>;
export type { SessionSummary, InterruptView };
