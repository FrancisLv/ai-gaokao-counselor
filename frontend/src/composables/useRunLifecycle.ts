import { computed, onScopeDispose, reactive, ref } from "vue";

import {
  GatewayError,
  cancelRun,
  confirmProfile,
  createPlan,
  createPolicyQuery,
  getRun,
  subscribeRunEvents,
} from "../api/gateway";
import type {
  CancelReceipt,
  CreatePlanReply,
  PolicyQueryForm,
  RunEvent,
  RunView,
} from "../api/types";

function newKey(prefix: string): string {
  return `${prefix}-${crypto.randomUUID()}`;
}

export function useRunLifecycle() {
  const profile = reactive({
    province: "湖南",
    applicationYear: 2025,
    category: "物理类" as "物理类" | "历史类",
    batch: "本科批",
    score: 600,
    subjects: [] as string[],
  });
  const policy = reactive<PolicyQueryForm>({
    queryText: "转专业政策是什么？",
    province: "湖南",
    applicationYear: new Date().getFullYear(),
    category: "物理类",
    batch: "本科批",
    schoolIds: "1001",
    documentTypes: "charter",
    mode: "hybrid",
  });
  const taskMode = ref<"plan" | "policy">("plan");
  const principalId =
    import.meta.env.VITE_PRINCIPAL_ID?.trim() || "未配置";
  const receipt = ref<CreatePlanReply | null>(null);
  const run = ref<RunView | null>(null);
  const events = ref<RunEvent[]>([]);
  const error = ref("");
  const submitting = ref(false);
  const confirming = ref(false);
  const cancelling = ref(false);
  const connected = ref(false);
  const cancelReceipt = ref<CancelReceipt | null>(null);
  const submitKey = ref(newKey("submit"));
  const confirmKey = ref(newKey("confirm"));
  const cancelKey = ref(newKey("cancel"));
  const lastEventSeq = computed(() =>
    events.value.reduce(
      (latest, event) => Math.max(latest, Number(event.eventSeq)),
      0,
    ),
  );
  let eventAbortController: AbortController | null = null;

  onScopeDispose(() => {
    eventAbortController?.abort();
  });

  async function submit(): Promise<void> {
    if (submitting.value) return;
    submitting.value = true;
    error.value = "";
    events.value = [];
    cancelReceipt.value = null;
    try {
      receipt.value = await createPlan(
        {
          province: profile.province,
          applicationYear: profile.applicationYear,
          category: profile.category,
          batch: profile.batch,
          score: profile.score,
          subjects: [...profile.subjects],
        },
        submitKey.value,
      );
      rememberRun(receipt.value.runId);
      run.value = {
        runId: receipt.value.runId,
        sessionId: receipt.value.sessionId,
        taskKind: "GENERATE_PLAN",
        state: "QUEUED",
        stage: "INTAKE",
        stateVersion: 1,
        releaseId: "pending",
      };
      void connectEvents(receipt.value.runId);
    } catch (cause) {
      error.value = errorMessage(cause);
    } finally {
      submitting.value = false;
    }
  }

  async function submitPolicy(): Promise<void> {
    if (submitting.value) return;
    submitting.value = true;
    error.value = "";
    events.value = [];
    cancelReceipt.value = null;
    try {
      receipt.value = await createPolicyQuery(
        {
          ...policy,
          schoolIds: policy.schoolIds,
          documentTypes: policy.documentTypes,
        },
        newKey("policy-submit"),
      );
      rememberRun(receipt.value.runId);
      run.value = {
        runId: receipt.value.runId,
        sessionId: receipt.value.sessionId,
        taskKind: "POLICY_QA",
        state: "QUEUED",
        stage: "RETRIEVAL",
        stateVersion: 1,
        releaseId: "pending",
      };
      void connectEvents(receipt.value.runId);
    } catch (cause) {
      error.value = errorMessage(cause);
    } finally {
      submitting.value = false;
    }
  }

  async function confirm(): Promise<void> {
    if (
      confirming.value ||
      !run.value?.currentInterrupt ||
      !run.value.currentInterrupt.profileVersion
    ) {
      return;
    }
    confirming.value = true;
    error.value = "";
    try {
      await confirmProfile(run.value.sessionId, {
        runId: run.value.runId,
        interruptId: run.value.currentInterrupt.interruptId,
        expectedStateVersion: Number(run.value.stateVersion),
        profileVersion: run.value.currentInterrupt.profileVersion,
        idempotencyKey: confirmKey.value,
      });
      await refreshRun(run.value.runId);
    } catch (cause) {
      error.value = errorMessage(cause);
    } finally {
      confirming.value = false;
    }
  }

  async function cancel(): Promise<void> {
    if (cancelling.value || !run.value) return;
    cancelling.value = true;
    error.value = "";
    try {
      cancelReceipt.value = await cancelRun(run.value.runId, cancelKey.value);
      await refreshRun(run.value.runId);
    } catch (cause) {
      error.value = errorMessage(cause);
    } finally {
      cancelling.value = false;
    }
  }

  async function refreshRun(runId: string): Promise<void> {
    try {
      run.value = await getRun(runId);
      if (run.value.taskKind === "POLICY_QA") {
        taskMode.value = "policy";
      }
      rememberRun(runId);
    } catch (cause) {
      error.value = errorMessage(cause);
    }
  }

  async function restore(): Promise<void> {
    const url = new URL(window.location.href);
    const runId =
      url.searchParams.get("run") ||
      sessionStorage.getItem("verification.runId");
    if (!runId) return;
    await refreshRun(runId);
  }

  async function connectEvents(runId: string): Promise<void> {
    eventAbortController?.abort();
    eventAbortController = new AbortController();
    await subscribeRunEvents({
      runId,
      afterEventSeq: lastEventSeq.value,
      signal: eventAbortController.signal,
      onConnectionChange(value) {
        connected.value = value;
      },
      onEvent(event) {
        if (events.value.some((item) => Number(item.eventSeq) === Number(event.eventSeq))) {
          return;
        }
        events.value = [...events.value, event].sort(
          (a, b) => Number(a.eventSeq) - Number(b.eventSeq),
        );
        if (event.type === "INTERRUPT" || event.type === "RESULT") {
          void refreshRun(runId);
        }
      },
      onError(cause) {
        error.value = errorMessage(cause);
      },
    });
  }

  return {
    principalId,
    taskMode,
    profile,
    policy,
    receipt,
    run,
    events,
    error,
    submitting,
    confirming,
    cancelling,
    connected,
    cancelReceipt,
    submit,
    submitPolicy,
    confirm,
    cancel,
    refreshRun,
    restore,
  };
}

function rememberRun(runId: string): void {
  sessionStorage.setItem("verification.runId", runId);
  const url = new URL(window.location.href);
  url.searchParams.set("run", runId);
  history.replaceState({}, "", url);
}

function errorMessage(cause: unknown): string {
  if (cause instanceof GatewayError) {
    if (cause.status === 401) return "未认证：缺少可信 principal。";
    return `${cause.status}: ${cause.message}`;
  }
  return cause instanceof Error ? cause.message : String(cause);
}
