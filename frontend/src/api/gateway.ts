import type {
  CancelReceipt,
  ChatMessageView,
  ChatSessionDetail,
  ConversationReply,
  CreatePlanReply,
  PolicyQueryForm,
  RegionAnswer,
  RunEvent,
  RunView,
  StructuredProfileForm,
  SessionSummary,
  WishlistDraft,
  WishlistDraftGroup,
} from "./types";

export class GatewayError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

function headers(contentType = true): Record<string, string> {
  const result: Record<string, string> = {};
  if (contentType) result["Content-Type"] = "application/json";
  const csrf = document.cookie
    .split(";")
    .map((item) => item.trim())
    .find((item) => item.startsWith("counselor_csrf="))
    ?.split("=")
    .slice(1)
    .join("=");
  if (csrf) result["X-CSRF-Token"] = decodeURIComponent(csrf);
  return result;
}

async function parseResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let message = `gateway ${response.status}`;
    try {
      const body = await response.json();
      message = body.message || body.reason || message;
    } catch {
      // Keep the stable HTTP status fallback.
    }
    throw new GatewayError(message, response.status);
  }
  return response.json() as Promise<T>;
}

export type Account = {
  accountId: string;
  username: string;
  nickname: string;
};

export type WishlistSummary = {
  draftVersionId: string;
  resultVersion: string;
  runId: string;
  releaseId: string;
  draftVersion: number;
  title: string;
  applicationYear: number;
  category: string;
  batch: string;
  groupCount: number;
  majorCount: number;
  status: "CURRENT" | "SUPERSEDED" | "STALE";
  staleReason?: string;
  successorRunId?: string;
  savedAt: string;
};

export type WishlistHistoryDraft = {
  draftVersionId: string;
  resultVersion: string;
  runId: string;
  releaseId: string;
  draftVersion: number;
  groups: WishlistDraftGroup[];
  createdAt: string;
};

export async function registerAccount(input: {
  inviteCode: string;
  username: string;
  password: string;
  nickname: string;
  privacyPolicyVersion: string;
  guardianAckRecorded: boolean;
}): Promise<{ account: Account; recoveryCodes: string[] }> {
  const response = await fetch("/v1/auth/register", {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({
      invite_code: input.inviteCode,
      username: input.username,
      password: input.password,
      nickname: input.nickname,
      privacy_policy_version: input.privacyPolicyVersion,
      guardian_ack_recorded: input.guardianAckRecorded,
    }),
  });
  return parseResponse(response);
}

export async function loginAccount(
  username: string,
  password: string,
): Promise<{ account: Account }> {
  const response = await fetch("/v1/auth/login", {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ username, password }),
  });
  return parseResponse(response);
}

export async function resetPassword(input: {
  username: string;
  recoveryCode: string;
  newPassword: string;
}): Promise<void> {
  const response = await fetch("/v1/auth/password:reset", {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({
      username: input.username,
      recovery_code: input.recoveryCode,
      new_password: input.newPassword,
    }),
  });
  await parseResponse(response);
}

export async function logoutAccount(): Promise<void> {
  const response = await fetch("/v1/auth/logout", {
    method: "POST",
    headers: headers(),
    body: "{}",
  });
  await parseResponse(response);
}

export async function getAccount(): Promise<{ account: Account }> {
  const response = await fetch("/v1/account", {
    headers: headers(false),
  });
  return parseResponse(response);
}

export async function updateAccount(
  nickname: string,
): Promise<{ account: Account }> {
  const response = await fetch("/v1/account", {
    method: "PATCH",
    headers: headers(),
    body: JSON.stringify({ nickname }),
  });
  return parseResponse(response);
}

export async function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<void> {
  const response = await fetch("/v1/account/password:change", {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
    }),
  });
  await parseResponse(response);
}

export async function regenerateRecoveryCodes(
  currentPassword: string,
): Promise<string[]> {
  const response = await fetch("/v1/account/recovery-codes:regenerate", {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ current_password: currentPassword }),
  });
  const payload = await parseResponse<{ recoveryCodes?: string[] }>(response);
  return payload.recoveryCodes ?? [];
}

export async function deleteAccount(
  currentPassword: string,
  confirmation: string,
): Promise<void> {
  const response = await fetch("/v1/account:delete", {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({
      current_password: currentPassword,
      confirmation,
    }),
  });
  await parseResponse(response);
}

export async function revokeSensitiveConsent(): Promise<number> {
  const response = await fetch("/v1/account/consents/sensitive:revoke", {
    method: "POST",
    headers: headers(),
    body: "{}",
  });
  const payload = await parseResponse<{ revokedCount?: number }>(response);
  return payload.revokedCount ?? 0;
}

export async function listWishlists(
  status = "",
  offset = 0,
  limit = 20,
): Promise<WishlistSummary[]> {
  const query = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  if (status) query.set("status", status);
  const response = await fetch(`/v1/wishlists?${query}`, {
    headers: headers(false),
  });
  const payload = await parseResponse<{ wishlists?: WishlistSummary[] }>(
    response,
  );
  return payload.wishlists ?? [];
}

export async function listWishlistHistory(
  resultVersion: string,
): Promise<WishlistHistoryDraft[]> {
  const response = await fetch(
    `/v1/results/${encodeURIComponent(resultVersion)}/wishlist-history`,
    { headers: headers(false) },
  );
  const payload = await parseResponse<{ drafts?: WishlistHistoryDraft[] }>(
    response,
  );
  return payload.drafts ?? [];
}

export async function restoreWishlistDraftVersion(
  resultVersion: string,
  draftVersion: number,
  idempotencyKey: string,
): Promise<WishlistDraft> {
  const response = await fetch(
    `/v1/results/${encodeURIComponent(resultVersion)}/wishlist-draft:restore`,
    {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({
        result_version: resultVersion,
        draft_version: draftVersion,
        idempotency_key: idempotencyKey,
      }),
    },
  );
  const payload = await parseResponse<{ draft?: WishlistDraft }>(response);
  const draft = normalizeWishlistDraft(payload.draft);
  if (!draft) throw new Error("DRAFT_RESTORE_EMPTY");
  return draft;
}

export async function deleteWishlist(
  resultVersion: string,
  requestId: string,
): Promise<void> {
  const response = await fetch(
    `/v1/results/${encodeURIComponent(resultVersion)}/wishlist:delete`,
    {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({
        result_version: resultVersion,
        request_id: requestId,
      }),
    },
  );
  await parseResponse(response);
}

export async function createPlan(
  profile: StructuredProfileForm,
  idempotencyKey: string,
  messages: ChatMessageView[] = [],
): Promise<CreatePlanReply> {
  const response = await fetch("/v1/plans", {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({
      user_text: JSON.stringify({
        task_kind: "GENERATE_PLAN",
        profile_input: {
          province: profile.province,
          application_year: profile.applicationYear,
          category: profile.category,
          batch: profile.batch,
          score: profile.score,
          subjects: profile.subjects,
          risk_appetite: profile.riskAppetite,
          priority_dims: profile.priorityDims,
          interests: profile.interests,
          preferred_cities: profile.preferredCities,
          preferred_tiers: profile.preferredTiers,
          required_provinces: profile.requiredProvinces,
          excluded_provinces: profile.excludedProvinces,
          required_cities: profile.requiredCities,
          excluded_cities: profile.excludedCities,
          required_tiers: profile.requiredTiers,
          required_major_names: profile.requiredMajorNames,
          required_major_keywords: profile.requiredMajorKeywords,
          excluded_major_keywords: profile.excludedMajorKeywords,
          excluded_major_names: profile.excludedMajorNames,
          excluded_universities: profile.excludedUniversities,
        },
        messages: messages.map((message) => ({
          client_message_id: message.messageId,
          role: message.role,
          kind: message.kind,
          content: message.content,
        })),
        idempotency_key: idempotencyKey,
      }),
    }),
  });
  return parseResponse<CreatePlanReply>(response);
}

export async function listSessions(): Promise<SessionSummary[]> {
  const response = await fetch("/v1/sessions?limit=20", {
    headers: headers(false),
  });
  const payload = await parseResponse<{ sessions?: SessionSummary[] }>(response);
  return payload.sessions ?? [];
}

export async function getSession(sessionId: string): Promise<ChatSessionDetail> {
  const response = await fetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}`,
    { headers: headers(false) },
  );
  return parseResponse<ChatSessionDetail>(response);
}

export async function sendConversationMessage(
  sessionId: string,
  profile: StructuredProfileForm,
  parentResultVersion: string,
  messageText: string,
  idempotencyKey: string,
  regionAnswer?: RegionAnswer,
): Promise<ConversationReply> {
  const response = await fetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/messages`,
    {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({
        session_id: sessionId,
        profile_input: {
          province: profile.province,
          application_year: profile.applicationYear,
          category: profile.category,
          batch: profile.batch,
          score: profile.score,
          subjects: profile.subjects,
          risk_appetite: profile.riskAppetite,
          priority_dims: profile.priorityDims,
          interests: profile.interests,
          preferred_cities: profile.preferredCities,
          preferred_tiers: profile.preferredTiers,
        required_provinces: profile.requiredProvinces,
        excluded_provinces: profile.excludedProvinces,
          required_cities: profile.requiredCities,
          excluded_cities: profile.excludedCities,
          required_tiers: profile.requiredTiers,
        required_major_names: profile.requiredMajorNames,
          required_major_keywords: profile.requiredMajorKeywords,
          excluded_major_keywords: profile.excludedMajorKeywords,
          excluded_major_names: profile.excludedMajorNames,
          excluded_universities: profile.excludedUniversities,
        },
        parent_result_version: parentResultVersion,
        message_text: messageText,
        idempotency_key: idempotencyKey,
        region_answer: regionAnswer
          ? {
              clarification_id: regionAnswer.clarificationId,
              provinces: regionAnswer.provinces,
              preset_ids: regionAnswer.presetIds,
              catalog_version: regionAnswer.catalogVersion,
              cancel_region_restriction:
                regionAnswer.cancelRegionRestriction,
            }
          : undefined,
      }),
    },
  );
  return parseResponse<ConversationReply>(response);
}

export async function submitRegionAnswer(
  sessionId: string,
  command: {
    runId: string;
    interruptId: string;
    expectedStateVersion: number;
    profileVersion: string;
    idempotencyKey: string;
    answer: RegionAnswer;
  },
): Promise<{ runId: string; sessionId: string; status: string }> {
  const response = await fetch(
    `/v1/plans/${encodeURIComponent(sessionId)}/profile:confirm`,
    {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({
        profile_json: JSON.stringify({
          run_id: command.runId,
          interrupt_id: command.interruptId,
          expected_state_version: command.expectedStateVersion,
          profile_version: command.profileVersion,
          idempotency_key: command.idempotencyKey,
          answer: {
            type: "SELECT_PROVINCES",
            clarification_id: command.answer.clarificationId,
            provinces: command.answer.provinces,
            selected_preset_ids: command.answer.presetIds,
            region_catalog_version: command.answer.catalogVersion,
            cancel_region_restriction:
              command.answer.cancelRegionRestriction,
          },
        }),
      }),
    },
  );
  return parseResponse(response);
}

export async function createPolicyQuery(
  query: PolicyQueryForm,
  idempotencyKey: string,
): Promise<CreatePlanReply> {
  const response = await fetch("/v1/policy-queries", {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({
      query_text: query.queryText,
      province: query.province,
      application_year: query.applicationYear,
      category: query.category,
      batch: query.batch,
      school_ids: query.schoolIds
        .split(",")
        .map((value) => value.trim())
        .filter(Boolean),
      document_types: query.documentTypes
        .split(",")
        .map((value) => value.trim())
        .filter(Boolean),
      mode: query.mode,
      idempotency_key: idempotencyKey,
    }),
  });
  return parseResponse<CreatePlanReply>(response);
}

export async function getRun(runId: string): Promise<RunView> {
  const response = await fetch(`/v1/runs/${encodeURIComponent(runId)}`, {
    headers: headers(false),
  });
  return parseResponse<RunView>(response);
}

export async function getWishlistDraft(
  resultVersion: string,
): Promise<WishlistDraft | null> {
  const response = await fetch(
    `/v1/results/${encodeURIComponent(resultVersion)}/wishlist-draft`,
    { headers: headers(false) },
  );
  const payload = await parseResponse<{ draft?: WishlistDraft }>(response);
  return normalizeWishlistDraft(payload.draft);
}

export async function saveWishlistDraft(
  resultVersion: string,
  expectedVersion: number,
  idempotencyKey: string,
  groups: WishlistDraftGroup[],
): Promise<WishlistDraft> {
  const response = await fetch(
    `/v1/results/${encodeURIComponent(resultVersion)}/wishlist-draft`,
    {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({
        result_version: resultVersion,
        expected_version: expectedVersion,
        idempotency_key: idempotencyKey,
        groups: groups.map((group) => ({
          university_code: group.universityCode,
          major_group: group.majorGroup,
          major_codes: group.majorCodes,
        })),
      }),
    },
  );
  const payload = await parseResponse<{ draft?: WishlistDraft }>(response);
  const draft = normalizeWishlistDraft(payload.draft);
  if (!draft) throw new Error("DRAFT_SAVE_EMPTY");
  return draft;
}

function normalizeWishlistDraft(
  draft: WishlistDraft | undefined,
): WishlistDraft | null {
  if (!draft) return null;
  return {
    ...draft,
    draftVersion: Number(draft.draftVersion),
  };
}

export async function confirmProfile(
  sessionId: string,
  command: {
    runId: string;
    interruptId: string;
    expectedStateVersion: number;
    profileVersion: string;
    idempotencyKey: string;
  },
): Promise<{ runId: string; sessionId: string; status: string }> {
  const response = await fetch(
    `/v1/plans/${encodeURIComponent(sessionId)}/profile:confirm`,
    {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({
        profile_json: JSON.stringify({
          run_id: command.runId,
          interrupt_id: command.interruptId,
          expected_state_version: command.expectedStateVersion,
          profile_version: command.profileVersion,
          idempotency_key: command.idempotencyKey,
          answer: { type: "CONFIRM_PROFILE" },
        }),
      }),
    },
  );
  return parseResponse(response);
}

export async function cancelRun(
  runId: string,
  idempotencyKey: string,
): Promise<CancelReceipt> {
  const response = await fetch(
    `/v1/runs/${encodeURIComponent(runId)}:cancel`,
    {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ idempotency_key: idempotencyKey }),
    },
  );
  return parseResponse<CancelReceipt>(response);
}

export interface EventStreamOptions {
  runId: string;
  afterEventSeq: number;
  signal: AbortSignal;
  onEvent: (event: RunEvent) => void;
  onConnectionChange?: (connected: boolean) => void;
  onError?: (error: Error) => void;
}

export async function subscribeRunEvents(
  options: EventStreamOptions,
): Promise<void> {
  let cursor = options.afterEventSeq;
  while (!options.signal.aborted) {
    try {
      const response = await fetch(
        `/v1/runs/${encodeURIComponent(options.runId)}/events?after_event_seq=${cursor}`,
        {
          headers: {
            ...headers(false),
            Accept: "text/event-stream",
          },
          signal: options.signal,
        },
      );
      if (!response.ok) {
        throw await toGatewayError(response);
      }
      options.onConnectionChange?.(true);
      cursor = await consumeEventStream(response, cursor, options.onEvent);
      options.onConnectionChange?.(false);
      if (options.signal.aborted) return;
      await delay(250, options.signal);
    } catch (error) {
      if (options.signal.aborted) return;
      options.onConnectionChange?.(false);
      options.onError?.(error instanceof Error ? error : new Error(String(error)));
      await delay(500, options.signal);
    }
  }
}

async function toGatewayError(response: Response): Promise<GatewayError> {
  let message = `gateway ${response.status}`;
  try {
    const body = await response.json();
    message = body.message || body.reason || message;
  } catch {
    // Keep HTTP status fallback.
  }
  return new GatewayError(message, response.status);
}

async function consumeEventStream(
  response: Response,
  initialCursor: number,
  onEvent: (event: RunEvent) => void,
): Promise<number> {
  if (!response.body) throw new Error("SSE response body unavailable");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let cursor = initialCursor;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const parsed = parseEventFrame(frame);
      if (parsed) {
        cursor = Math.max(cursor, Number(parsed.eventSeq));
        onEvent(parsed);
      }
      boundary = buffer.indexOf("\n\n");
    }
  }
  return cursor;
}

function parseEventFrame(frame: string): RunEvent | null {
  const dataLines = frame
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart());
  if (dataLines.length === 0) return null;
  return JSON.parse(dataLines.join("\n")) as RunEvent;
}

function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = window.setTimeout(resolve, ms);
    signal.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });
}
