export type ProfileFieldState =
  | "PROVIDED"
  | "UNKNOWN"
  | "DECLINED"
  | "DERIVED"
  | "DEFAULT";

export interface StructuredProfileForm {
  province: string;
  applicationYear: number;
  category: "物理类" | "历史类";
  batch: string;
  score: number;
  subjects: string[];
  riskAppetite: "conservative" | "balanced" | "aggressive";
  priorityDims: string[];
  interests: string[];
  preferredCities: string[];
  preferredTiers: string[];
  requiredProvinces: string[];
  excludedProvinces: string[];
  requiredCities: string[];
  excludedCities: string[];
  requiredTiers: string[];
  requiredMajorNames: string[];
  requiredMajorKeywords: string[];
  excludedMajorKeywords: string[];
  excludedMajorNames: string[];
  excludedUniversities: string[];
}

export interface CreatePlanReply {
  runId: string;
  sessionId: string;
  status: string;
}

export interface ProfileFieldView {
  state: ProfileFieldState;
  source: string;
  stringValue?: string;
  intValue?: number;
  stringList?: { values: string[] };
}

export interface InterruptView {
  interruptId: string;
  kind: "INTAKE_ASK" | "PROFILE_CONFIRM" | "BACKFILL_ASK";
  state: string;
  profileVersion?: string;
  fields?: Record<string, ProfileFieldView>;
  regionClarification?: RegionClarification;
  findings?: Array<{
    code: string;
    fieldKeys: string[];
    blocking: boolean;
    messageKey: string;
  }>;
}

export interface RegionPreset {
  id: string;
  name: string;
  provinces: string[];
}

export interface RegionClarification {
  clarificationId: string;
  clarificationKind: "SELECT_PROVINCES";
  reasonCode: string;
  term: string;
  operation: "exclude" | "require" | "withdraw";
  catalogVersion: string;
  provinces: string[];
  presets: RegionPreset[];
}

export interface RegionAnswer {
  clarificationId: string;
  provinces: string[];
  presetIds: string[];
  catalogVersion: string;
  cancelRegionRestriction: boolean;
}

export interface UnverifiedCondition {
  code: string;
  message: string;
}

export interface AlgorithmPreview {
  resultVersion: string;
  runId: string;
  releaseId: string;
  confirmedProfileVersion: string;
  rank: number;
  percentile: number;
  rushCandidates: CandidateWithProb[];
  stableCandidates: CandidateWithProb[];
  safeCandidates: CandidateWithProb[];
  rushTotal?: number;
  stableTotal?: number;
  safeTotal?: number;
  auditBasis?: Record<string, unknown>;
  keptCandidates: CandidateWithProb[];
  rejectedCandidates: RejectedCandidate[];
  wishlist?: {
    candidates: Array<{
      cwp: CandidateWithProb;
      finalRank: number;
      utility?: { score: number };
    }>;
    report?: Record<string, unknown>;
  };
  verificationSummary: Array<{
    key: string;
    status: string;
    detail: string;
  }>;
  unverifiedConditions: UnverifiedCondition[];
  riskNotice: string;
  disclaimer: string;
  internalControlledOnly: boolean;
  formalRecommendation: boolean;
  resultKind: "ALGORITHM_PREVIEW";
}

export interface PolicyEvidenceItem {
  evidenceId: string;
  evidenceType: string;
  contentSha256: string;
  createdAt: string;
  producerVersion: string;
  docId: string;
  chunkId: string;
  title: string;
  documentType: string;
  issuer: string;
  sourceUri: string;
  fetchedAt?: string;
  sectionPath: string;
  excerpt: string;
  excerptSha256: string;
  charStart?: number;
  charEnd?: number;
  regions: string[];
  categories: string[];
  batches: string[];
  validFrom: string;
  validTo?: string;
  releaseId: string;
}

export interface PolicyEvidenceResult {
  resultVersion: string;
  resultKind: "POLICY_EVIDENCE";
  runId: string;
  releaseId: string;
  retrievalStatus: string;
  degradedReason?: string;
  vectorCandidates: number;
  keywordCandidates: number;
  rerankEnabled: boolean;
  rerankModel?: string;
  rerankConfigVersion?: string;
  rerankTimeoutMs?: number;
  relevanceFilteredCandidates: number;
  relevancePolicyVersion?: string;
  evidence: PolicyEvidenceItem[];
}

export interface CandidateWithProb {
  candidate?: {
    universityCode: string;
    universityName: string;
    majorGroup: string;
    schoolTier?: string;
    location?: string;
    majors?: AlgorithmMajor[];
  };
  prob?: {
    probability: number;
    method: string;
    dataYears: number;
    category: string;
  };
}

export interface AlgorithmMajor {
  majorCode: string;
  majorName: string;
  subjectReq?: string;
  duration?: string;
  tuition?: number;
  hasTuition?: boolean;
  minScore?: number;
  minRank?: number;
  referenceYear?: number;
  admissionProbability?: number;
  probabilityDataYears?: number;
  planCount?: number;
  recommended?: boolean;
  recommendationRank?: number;
  selectable?: boolean;
  unavailableReason?: string;
}

export interface ReportMajor {
  majorCode: string;
  majorName: string;
  subjectReq: string;
  duration: string;
  minScore?: number;
  minRank?: number;
  referenceYear?: number;
  admissionProbability?: number;
  probabilityDataYears: number;
  probabilitySampleYears?: number[];
  planCount?: number;
  tuition?: number;
  recommended: boolean;
  recommendationRank: number;
  selectable: boolean;
  unavailableReason: string;
}

export interface FormalEvidenceCitation {
  evidenceId: string;
  evidenceType: string;
  contentSha256: string;
  label: string;
  sourceUri?: string;
  sectionPath?: string;
  releaseId: string;
}

export interface FormalReportSection {
  sectionKey: string;
  title: string;
  body: string;
  citations: FormalEvidenceCitation[];
  history?: HistoricalAdmissionReference[];
  majors?: ReportMajor[];
  planCount?: number;
  previousPlanCount?: number;
  planAdjustFactor?: number;
  probability?: number;
  strategyTier?: string;
  utilityScore?: number;
  finalRank?: number;
  universityCode?: string;
  universityName?: string;
  majorGroup?: string;
}

export interface HistoricalAdmissionReference {
  year: number;
  minScore?: number;
  minRank?: number;
  totalCount?: number;
  mappingMethod?: string;
  mappingConfidence?: number;
}

export interface FormalReport {
  reportVersion: string;
  generationMode: string;
  sections: FormalReportSection[];
  claimIds: string[];
  unverifiedFacts: string[];
  degradationReason?: string;
}

export interface FormalResult {
  resultVersion: string;
  resultKind: "V1_FORMAL";
  releaseId: string;
  confirmedProfileVersion: string;
  verificationStatus: string;
  citationCoverage: number;
  resultState: string;
  staleReason?: string;
  disclaimer: string;
  riskNotice: string;
  noAdmissionGuarantee: string;
  report: FormalReport;
}

export interface WishlistDraftGroup {
  universityCode: string;
  majorGroup: string;
  majorCodes: string[];
}

export interface WishlistDraftRisk {
  groupCount: number;
  rushCount: number;
  stableCount: number;
  safeTierCount: number;
  safeGuardCount: number;
  safeGuardTarget: number;
  safeGuardOk: boolean;
  slipRate: number;
  expectedSatisfaction?: number;
}

export interface WishlistDraft {
  draftVersionId: string;
  resultVersion: string;
  runId: string;
  releaseId: string;
  draftVersion: number;
  groups: WishlistDraftGroup[];
  createdAt: string;
  risk?: WishlistDraftRisk;
}

export interface RejectedCandidate {
  candidate?: CandidateWithProb;
  rule: string;
  reason: string;
}

export interface RunView {
  runId: string;
  sessionId: string;
  taskKind: string;
  state:
    | "QUEUED"
    | "RUNNING"
    | "WAITING_HITL"
    | "EXPIRED"
    | "SUCCEEDED"
    | "FAILED"
    | "CANCELLED";
  stage: string;
  terminalReason?: string;
  stateVersion: number | string;
  releaseId: string;
  confirmedProfileVersion?: string;
  currentInterrupt?: InterruptView;
  resultPreview?: AlgorithmPreview;
  policyEvidence?: PolicyEvidenceResult;
  formalResult?: FormalResult;
  expiresAt?: string;
}

export interface PolicyQueryForm {
  queryText: string;
  province: string;
  applicationYear: number;
  category: "物理类" | "历史类" | "不限";
  batch: "本科批" | "不限";
  schoolIds: string;
  documentTypes: string;
  mode: "hybrid" | "vector" | "keyword";
}

export interface RunEvent {
  runId: string;
  eventSeq: number | string;
  type: "STATE_CHANGED" | "PROGRESS" | "INTERRUPT" | "RESULT" | "ERROR";
  stage: string;
  stateVersion: number | string;
  occurredAt?: string;
  payloadVersion?: number | string;
  payload?: Record<string, unknown>;
}

export interface CancelReceipt {
  accepted: boolean;
  reason?: string;
  runId: string;
  state: string;
  stage: string;
}

export interface ChatMessageView {
  messageId: string;
  runId?: string;
  role: "USER" | "ASSISTANT" | "SYSTEM";
  kind: "TEXT" | "PROGRESS" | "RESULT" | "ERROR";
  content: string;
  createdAt?: string;
  metadata?: Record<string, string>;
}

export interface SessionSummary {
  sessionId: string;
  title: string;
  latestRunId?: string;
  latestState: string;
  latestResultVersion?: string;
  latestResultKind?: string;
  runCount: number;
  createdAt?: string;
  updatedAt?: string;
}

export interface SessionRun {
  runId: string;
  taskKind: string;
  state: RunView["state"];
  stage: string;
  terminalReason?: string;
  parentResultVersion?: string;
  resultVersion?: string;
  resultKind?: string;
  createdAt?: string;
  updatedAt?: string;
}

export interface ChatSessionDetail {
  summary: SessionSummary;
  messages: ChatMessageView[];
  runs: SessionRun[];
  profileFields: Record<string, ProfileFieldView>;
}

export interface ConversationReply {
  sessionId: string;
  runId?: string;
  taskKind?: string;
  status: string;
  assistantMessage: string;
  regionClarification?: RegionClarification;
}
