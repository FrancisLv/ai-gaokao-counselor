import type {
  CancelReceipt,
  CreatePlanReply,
  RunEvent,
  RunView,
  StructuredProfileForm,
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
  const principal = import.meta.env.VITE_PRINCIPAL_ID?.trim();
  if (principal) result["x-principal-id"] = principal;
  if (contentType) result["Content-Type"] = "application/json";
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

export async function createPlan(
  profile: StructuredProfileForm,
  idempotencyKey: string,
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
        },
        idempotency_key: idempotencyKey,
      }),
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
