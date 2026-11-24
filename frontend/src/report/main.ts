import "../style.css";

import {
  getRun,
  getWishlistDraft,
  GatewayError,
  saveWishlistDraft,
} from "../api/gateway";
import type {
  ReportMajor,
  RunView,
  WishlistDraft,
  WishlistDraftGroup,
} from "../api/types";
import {
  canExportPdf,
  candidateSections,
  candidateIntroduction,
  candidateTierGroups,
  escapeHtml,
  pdfExportLabel,
  pdfFilename,
  schoolDisplayTitle,
} from "./model";
import {
  canSaveDraft,
  defaultMajorCodes,
  groupKey,
  mergeDraftWithDefaults,
} from "./draft";

const rootElement = document.querySelector<HTMLDivElement>("#report");
if (!rootElement) throw new Error("REPORT_ROOT_MISSING");
const root: HTMLDivElement = rootElement;

let currentRun: RunView | null = null;
let currentDraft: WishlistDraft | null = null;
let currentGroups: WishlistDraftGroup[] = [];
let dirty = false;
let statusMessage = "";
const expandedGroups = new Set<string>();
const removedGroupSelections = new Map<string, string[]>();
let titleBeforePrint = document.title;
const historyCriticalMargin = 0.002;

void loadReport();
window.addEventListener("beforeprint", preparePrint);
window.addEventListener("afterprint", finishPrint);

async function loadReport(): Promise<void> {
  const runId = new URLSearchParams(location.search).get("run");
  if (!runId) {
    renderError("缺少 run 参数，无法读取不可变推荐报告。");
    return;
  }
  root.innerHTML = '<div class="loading-page">正在读取已核验推荐报告…</div>';
  try {
    const run = await getRun(runId);
    if (!run.formalResult) {
      renderError(
        run.state === "SUCCEEDED"
          ? "该结果不是 V1_FORMAL 正式报告。"
          : `任务尚未生成正式报告：${run.terminalReason || run.state}`,
      );
      return;
    }
    let draft: WishlistDraft | null = null;
    try {
      draft = await getWishlistDraft(run.formalResult.resultVersion);
    } catch (cause) {
      statusMessage =
        cause instanceof Error
          ? `草稿读取失败：${cause.message}`
          : "草稿读取失败。";
    }
    renderReport(run, draft);
  } catch (cause) {
    if (cause instanceof GatewayError && cause.status === 401) {
      location.replace(
        `/login.html?return=${encodeURIComponent(
          `${location.pathname}${location.search}`,
        )}`,
      );
      return;
    }
    if (
      cause instanceof GatewayError &&
      cause.status === 404 &&
      cause.message === "RUN_NOT_FOUND"
    ) {
      renderUnavailableReport();
      return;
    }
    renderError(
      cause instanceof GatewayError
        ? `读取报告失败（${cause.status}）：${cause.message}`
        : cause instanceof Error
          ? cause.message
          : String(cause),
    );
  }
}

function renderReport(run: RunView, draft: WishlistDraft | null): void {
  const formal = run.formalResult;
  if (!formal) return;
  const sections = formal.report.sections.filter((section) =>
    section.sectionKey.startsWith("candidate:"),
  );
  currentRun = run;
  currentDraft = draft;
  currentGroups = mergeDraftWithDefaults(sections, draft);
  removedGroupSelections.clear();
  dirty = false;
  paintReport();
}

function paintReport(): void {
  const run = currentRun;
  const formal = run?.formalResult;
  if (!run || !formal) return;
  const candidates = candidateSections(run);
  const selectedKeys = new Set(currentGroups.map((group) => groupKey(group)));
  const groups = candidateTierGroups(candidates).map(({ tier, schools }) => ({
    tier,
    schools: orderTierSchools(schools).filter((school) =>
      selectedKeys.has(`${school.universityCode}|${school.majorGroup}`),
    ),
  }));
  const excludedCandidates = candidates.filter(
    (school) =>
      !selectedKeys.has(`${school.universityCode}|${school.majorGroup}`),
  );
  const plan = formal.report.sections.find(
    (section) => section.sectionKey === "plan",
  );
  const dataGaps = formal.report.sections.find(
    (section) => section.sectionKey === "data_gaps",
  );
  const risk = formal.report.sections.find(
    (section) => section.sectionKey === "risk",
  );
  const rank = plan?.body.match(/位次\s*(\d+)\s*名/)?.[1] ?? "—";
  const percentileRatio = plan?.body.match(/百分位\s*([0-9.]+)/)?.[1];
  const userPercentileRatio = percentileRatio
    ? Number(percentileRatio)
    : undefined;
  const percentile = formatPercentile(percentileRatio);
  const category = plan?.body.match(/(物理类|历史类)考生/)?.[1];
  const rankPositionLabel = category ? `${category}排位` : "排位";
  const planBody =
    plan && percentileRatio
      ? plan.body.replace(/(百分位\s*)[0-9.]+/, `排位${percentile}`)
      : plan?.body || "正式报告已生成。";
  const hasHistory = groups.some(({ schools }) =>
    schools.some((school) => school.history.length > 0),
  );
  const draftRisk = renderDraftRiskSummary();
  const printWishlist = renderPrintWishlist(
    candidates,
    userPercentileRatio,
  );
  const exportAllowed = canExportPdf(formal);
  const hasSavedDraft = Boolean(currentDraft);
  const reportScope = category
    ? `湖南 2025 · ${category} · 本科批`
    : "湖南 2025 · 本科批";
  const reportStatusLabel =
    formal.resultState === "ACTIVE" ? "报告有效" : "报告已失效";
  const reportStatusClass =
    formal.resultState === "ACTIVE" ? "active" : "stale";
  const shortResultVersion = formal.resultVersion.slice(0, 8);
  const dataGapText = dataGaps?.body.trim() ?? "";
  const hasActionableDataGaps =
    dataGapText !== "" &&
    !["无", "无。", "无待核验数据", "无待核验数据。"].includes(dataGapText);

  root.innerHTML = `
    <header class="report-topbar report-summary-topbar">
      <div class="report-topbar-title">
        <span class="brand-mark">愿</span>
        <div>
          <strong>知愿 · 志愿推荐报告</strong>
          <small>
            ${escapeHtml(reportScope)} ·
            结果版本 ${escapeHtml(shortResultVersion)}
          </small>
        </div>
      </div>
      <nav class="report-topbar-actions" aria-label="报告操作">
        <span class="report-status ${reportStatusClass}">
          ${reportStatusLabel}
        </span>
        <a class="session-badge" href="/wishlists.html">我的志愿表</a>
        <a class="session-badge" href="/">返回对话</a>
      </nav>
    </header>
    <div class="report-layout">
      <nav class="report-toc">
        <span class="muted">目录</span>
        ${groups
          .map(
            ({ tier }) =>
              `<a href="#tier-${tier}">${tier === "冲" ? "冲刺" : tier === "稳" ? "稳妥" : "保底"}院校</a>`,
          )
          .join("")}
        ${
          excludedCandidates.length
            ? '<a href="#excluded-groups">未纳入志愿表</a>'
            : ""
        }
        <a href="#risk">风险提示</a>
      </nav>
      <article class="report-article">
        <span class="report-kicker">PERSONAL PLAN · 湖南 2025 本科批</span>
        <h1>你的冲稳保志愿报告</h1>
        <p class="report-lede">${escapeHtml(planBody)}</p>
        <div class="report-summary">
          <div><small>预估位次</small><strong>${escapeHtml(rank)}</strong></div>
          <div><small>${escapeHtml(rankPositionLabel)}</small><strong>${escapeHtml(percentile)}</strong></div>
          <div><small>拟填 / 推荐专业组</small><strong>${currentGroups.length} / ${candidates.length}</strong></div>
          <div><small>引用覆盖率</small><strong>${Math.round(formal.citationCoverage * 100)}%</strong></div>
        </div>
        <div class="draft-toolbar">
          <div class="draft-summary">
            <strong>拟填志愿表</strong>
            <span id="draft-status" class="${dirty ? "dirty" : "saved"}">${escapeHtml(statusText())}</span>
          </div>
          <div class="draft-actions">
            <span class="print-hint">导出时在打印面板选择“另存为 PDF”</span>
            <button class="secondary-button" id="save-draft" ${canSaveDraft(currentGroups) ? "" : "disabled"}>保存草稿</button>
            <button class="primary-button" id="export-pdf" ${canSaveDraft(currentGroups) && exportAllowed ? "" : "disabled"}>${exportAllowed ? pdfExportLabel(hasSavedDraft, dirty) : "结果已失效，无法导出"}</button>
          </div>
        </div>
        <div class="screen-draft-risk">${draftRisk}</div>
        ${printWishlist}
        ${
          hasHistory
            ? `
              <p class="report-history-note">
                表中“暂无数据”表示暂无可核验的历史记录，不代表该专业组当年未招生。
                跨年最低位次先按当年同科类总人数换算为百分位；你的当前百分位为${escapeHtml(percentile)}。
                历史百分位与当前百分位相差不超过 0.2 个百分点时标记为临界，并按一半命中权重计算。
              </p>
            `
            : ""
        }
        <div class="wishlist-tiers">
          ${groups
            .map(
              ({ tier, schools }) => `
              <section class="report-section" id="tier-${tier}">
                <h2>${tier === "冲" ? "冲刺院校" : tier === "稳" ? "稳妥院校" : "保底院校"}</h2>
                <p>
                  ${
                  tier === "冲"
                    ? "适合放置在志愿表前段，仍需重点核对热门专业的组内规则。"
                    : tier === "稳"
                      ? "与当前位次更接近，通常应作为方案主体。"
                      : "用于给方案留下可接受的落点，不代表保证录取。"
                  }
                </p>
                <div class="school-list">
                  ${
                    schools.length
                      ? schools
                          .map((school) =>
                            renderSchoolGroup(
                              run,
                              school,
                              tier,
                              userPercentileRatio,
                            ),
                          )
                          .join("")
                      : '<p class="muted">该梯度暂无拟填专业组。</p>'
                  }
                </div>
              </section>
            `,
            )
            .join("")}
        </div>
        ${
          excludedCandidates.length
            ? `
              <section class="report-section excluded-groups-section" id="excluded-groups">
                <h2>未纳入志愿表的推荐专业组</h2>
                <p>
                  这些专业组仍保留在正式推荐中，不参与当前草稿风险计算。
                  需要时可以重新加入；重新加入后使用系统默认拟填专业。
                </p>
                <div class="school-list">
                  ${excludedCandidates
                    .map((school) =>
                      renderSchoolGroup(
                        run,
                        school,
                        school.tier === "稳"
                          ? "稳"
                          : school.tier === "保"
                            ? "保"
                            : "冲",
                        userPercentileRatio,
                      ),
                    )
                    .join("")}
                </div>
              </section>
            `
            : ""
        }
        <section class="report-section" id="risk">
          <h2>风险提示</h2>
          <div class="risk-note">
            ${
              hasActionableDataGaps
                ? `<p><strong>需要核实：</strong>${escapeHtml(dataGapText)}</p>`
                : ""
            }
            <p>${escapeHtml(risk?.body || formal.riskNotice)}</p>
            <p>${escapeHtml(formal.disclaimer)}</p>
          </div>
        </section>
      </article>
    </div>
    <div class="print-block-message">
      <h1>无法导出 PDF 志愿报告</h1>
      <p>请关闭打印面板，在报告页保存当前志愿草稿，并确认正式结果仍为有效状态后重试。</p>
    </div>
    <div class="print-footer">
      知愿 · PDF 志愿报告 · 历史数据估算，不构成录取保证
    </div>
  `;
  bindDraftActions();
  const requestedTier =
    new URLSearchParams(location.search).get("tier") ||
    decodeFragment(location.hash);
  if (requestedTier && ["冲", "稳", "保"].includes(requestedTier)) {
    document
      .getElementById(`tier-${requestedTier}`)
      ?.scrollIntoView({ block: "start" });
  }
}

function orderTierSchools(
  schools: ReturnType<typeof candidateSections>,
): ReturnType<typeof candidateSections> {
  return [...schools].sort((left, right) => {
    const leftIndex = currentGroups.findIndex(
      (group) =>
        group.universityCode === left.universityCode &&
        group.majorGroup === left.majorGroup,
    );
    const rightIndex = currentGroups.findIndex(
      (group) =>
        group.universityCode === right.universityCode &&
        group.majorGroup === right.majorGroup,
    );
    if (leftIndex >= 0 && rightIndex >= 0) return leftIndex - rightIndex;
    if (leftIndex >= 0) return -1;
    if (rightIndex >= 0) return 1;
    return (
      (left.finalRank ?? Number.MAX_SAFE_INTEGER) -
        (right.finalRank ?? Number.MAX_SAFE_INTEGER) ||
      left.sectionKey.localeCompare(right.sectionKey)
    );
  });
}

function renderGroupActions(
  groupIndex: number,
  included: boolean,
  canMoveUp: boolean,
  canMoveDown: boolean,
  sectionKey: string,
): string {
  if (!included) {
    return `
      <div class="wishlist-group-actions group-actions-toolbar">
        <button data-action="add-group" data-section-key="${escapeHtml(sectionKey)}">重新加入</button>
      </div>
    `;
  }
  return `
    <div class="wishlist-group-actions group-actions-toolbar">
      <button data-action="group-move-up" data-group-index="${groupIndex}" ${canMoveUp ? "" : "disabled"}><span aria-hidden="true">↑</span>上移</button>
      <button data-action="group-move-down" data-group-index="${groupIndex}" ${canMoveDown ? "" : "disabled"}><span aria-hidden="true">↓</span>下移</button>
      <button class="danger" data-action="remove-group" data-group-index="${groupIndex}"><span aria-hidden="true">×</span>移除</button>
    </div>
  `;
}

function renderDraftRiskSummary(): string {
  const risk = currentDraft?.risk;
  if (!risk) {
    return `
      <div class="draft-risk-card">
        <strong>草稿风险摘要</strong>
        <span>保存草稿后由算法按当前专业组集合生成。</span>
      </div>
    `;
  }
  const stale = dirty
    ? '<span class="risk-stale">页面已有未保存修改，以下为已保存版本</span>'
    : "";
  const shortfall = risk.safeGuardOk
    ? ""
    : `<p class="risk-warning"><strong>保底不足：</strong>当前达到稳拿阈值的组为 ${risk.safeGuardCount} 个，配置目标为 ${risk.safeGuardTarget} 个。可以保存，但请谨慎评估滑档风险。</p>`;
  return `
    <div class="draft-risk-card ${risk.safeGuardOk ? "" : "risk-high"}">
      <div class="draft-risk-heading">
        <strong>草稿风险摘要</strong>
        ${stale}
      </div>
      <div class="draft-risk-metrics">
        <span>${risk.groupCount} 组</span>
        <span>冲 ${risk.rushCount}</span>
        <span>稳 ${risk.stableCount}</span>
        <span>保 ${risk.safeTierCount}</span>
        <span>稳拿保底 ${risk.safeGuardCount}/${risk.safeGuardTarget}</span>
        <span>估算滑档率 ${formatSlipRate(risk.slipRate)}</span>
        ${
          risk.expectedSatisfaction === undefined
            ? ""
            : `<span>期望满意度 ${Math.round(risk.expectedSatisfaction * 100)}%</span>`
        }
      </div>
      ${shortfall}
    </div>
  `;
}

function formatSlipRate(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "—";
  if (value < 0.0001) return "低于 0.01%";
  return `${Number((value * 100).toFixed(2))}%`;
}

function renderPrintWishlist(
  candidates: ReturnType<typeof candidateSections>,
  userPercentile: number | undefined,
): string {
  const byIdentity = new Map(
    candidates.map((school) => [
      `${school.universityCode}|${school.majorGroup}`,
      school,
    ]),
  );
  return `
    <section class="print-selected-wishlist">
      <h2>已保存拟填志愿</h2>
      ${renderDraftRiskSummary()}
      ${
        currentGroups.length
          ? currentGroups
              .map((group, index) => {
                const school = byIdentity.get(groupKey(group));
                if (!school) return "";
                const selected = group.majorCodes
                  .map((code) =>
                    school.majors?.find((major) => major.majorCode === code),
                  )
                  .filter((major): major is ReportMajor => Boolean(major));
                return `
                  <article class="school-row wishlist-group print-wishlist-item">
                    <span class="tier ${school.tier === "稳" ? "stable" : school.tier === "保" ? "safe" : ""}">${escapeHtml(school.tier)}</span>
                    <div class="school-group-content">
                      <div class="school-heading-row">
                        <h3>${index + 1}. ${escapeHtml(schoolDisplayTitle(school))}</h3>
                        <span class="print-school-probability">
                          专业组录取概率
                          <strong>${escapeHtml(school.probability)}%</strong>
                        </span>
                      </div>
                      ${
                        school.history.length
                          ? `
                            <table class="history-table">
                              <thead><tr><th>年份</th><th>专业组最低录取分</th><th>最低位次</th><th>同科类百分位</th><th>相对当前位次</th></tr></thead>
                              <tbody>
                                ${school.history
                                  .map(
                                    (history) => `
                                      <tr>
                                        <td>${history.year}</td>
                                        <td class="${history.minScore === null ? "missing" : ""}">${history.minScore ?? "暂无数据"}</td>
                                        <td class="${history.minRank === null ? "missing" : ""}">${history.minRank ?? "暂无数据"}</td>
                                        <td class="${history.percentile === null ? "missing" : ""}">${formatHistoryPercentile(history.percentile)}</td>
                                        <td>${renderHistoryComparison(history.percentile, userPercentile)}</td>
                                      </tr>
                                    `,
                                  )
                                  .join("")}
                              </tbody>
                            </table>
                          `
                          : ""
                      }
                      ${renderPrintMajorList(selected)}
                    </div>
                  </article>
                `;
              })
              .join("")
          : '<p class="muted">未保存任何拟填院校专业组。</p>'
      }
    </section>
  `;
}

function renderSchoolGroup(
  run: RunView,
  school: ReturnType<typeof candidateSections>[number],
  tier: "冲" | "稳" | "保",
  userPercentile: number | undefined,
): string {
  const groupIndex = currentGroups.findIndex(
    (group) =>
      group.universityCode === school.universityCode &&
      group.majorGroup === school.majorGroup,
  );
  const draftGroup = currentGroups[groupIndex];
  const selected = (draftGroup?.majorCodes ?? [])
    .map((code) => school.majors?.find((major) => major.majorCode === code))
    .filter((major): major is ReportMajor => Boolean(major));
  const remaining = (school.majors ?? []).filter(
    (major) => !selected.some((item) => item.majorCode === major.majorCode),
  );
  const key = `${school.universityCode}|${school.majorGroup}`;
  const expanded = expandedGroups.has(key);
  const included = groupIndex >= 0;
  const tierKeys = new Set(
    candidateSections(run)
      .filter((candidate) => candidate.tier === tier)
      .map((candidate) => `${candidate.universityCode}|${candidate.majorGroup}`),
  );
  const tierOrder = currentGroups
    .map((group) => groupKey(group))
    .filter((identity) => tierKeys.has(identity));
  const tierPosition = tierOrder.indexOf(key);
  const canMoveUp = included && tierPosition > 0;
  const canMoveDown =
    included && tierPosition >= 0 && tierPosition < tierOrder.length - 1;
  return `
    <article
      class="school-row wishlist-group ${included ? "" : "wishlist-group-excluded"}"
      data-group-index="${groupIndex}"
      data-group-key="${escapeHtml(key)}"
      data-tier="${escapeHtml(tier)}"
    >
      <span class="tier ${tier === "稳" ? "stable" : tier === "保" ? "safe" : ""}">${tier}</span>
      <div class="school-group-content">
        <div class="school-summary-block">
          <div class="school-heading-row">
            <a class="school-title-link" href="/school.html?run=${encodeURIComponent(run.runId)}&section=${encodeURIComponent(school.sectionKey)}">
              <h3>${escapeHtml(schoolDisplayTitle(school))}</h3>
            </a>
            ${renderGroupActions(
              groupIndex,
              included,
              canMoveUp,
              canMoveDown,
              school.sectionKey,
            )}
            <span class="print-school-probability">
              专业组录取概率
              <strong>${escapeHtml(school.probability)}%</strong>
            </span>
          </div>
          <p>${escapeHtml(candidateIntroduction(school))}</p>
          ${
            school.history.length
              ? `
                <table class="history-table">
                  <thead><tr><th>年份</th><th>专业组最低录取分</th><th>最低位次</th><th>同科类百分位</th><th>相对当前位次</th></tr></thead>
                  <tbody>
                    ${school.history
                      .map(
                        (history) => `
                          <tr>
                            <td>${history.year}</td>
                            <td class="${history.minScore === null ? "missing" : ""}">${history.minScore ?? "暂无数据"}</td>
                            <td class="${history.minRank === null ? "missing" : ""}">${history.minRank ?? "暂无数据"}</td>
                            <td class="${history.percentile === null ? "missing" : ""}">${formatHistoryPercentile(history.percentile)}</td>
                            <td>${renderHistoryComparison(history.percentile, userPercentile)}</td>
                          </tr>
                        `,
                      )
                      .join("")}
                  </tbody>
                </table>
                ${renderPlanAdjustmentNote(school)}
              `
              : '<p class="history-note">旧版报告未包含历年专业组录取参考。</p>'
          }
        </div>
        ${
          included
            ? `
              <div class="major-selection">
                <div class="major-selection-heading">
                  <strong>拟填专业 ${selected.length}/6</strong>
                  <span>专业顺序会作为填报顺序</span>
                </div>
                ${
                  school.majors?.length
                    ? `
                      ${renderMajorTable(groupIndex, selected, "selected")}
                      ${renderPrintMajorList(selected)}
                    `
                    : '<p class="muted">旧版报告未包含逐专业结构，请重新生成报告后编辑。</p>'
                }
                ${
                  remaining.length
                    ? `
                      <button class="text-button" data-action="toggle-remaining" data-group-index="${groupIndex}">
                        ${expanded ? "收起其余专业" : `查看其余 ${remaining.length} 个专业`}
                      </button>
                      <div class="remaining-majors" data-group-index="${groupIndex}" ${expanded ? "" : "hidden"}>
                        ${renderMajorTable(groupIndex, remaining, "remaining")}
                      </div>
                    `
                    : ""
                }
              </div>
            `
            : `
              <div class="removed-group-note">
                <strong>已移出志愿表</strong>
                <span>该组仍保留在正式报告中；重新加入后，组内专业从系统默认推荐开始。</span>
                <button class="text-button" data-action="add-group" data-section-key="${escapeHtml(school.sectionKey)}">重新加入</button>
              </div>
            `
        }
      </div>
      <div class="school-probability">
        <span class="muted">专业组录取概率</span>
        <div class="probability">${escapeHtml(school.probability)}%</div>
      </div>
    </article>
  `;
}

function renderMajorTable(
  groupIndex: number,
  majors: ReportMajor[],
  mode: "selected" | "remaining",
): string {
  if (!majors.length) {
    return '<p class="muted">暂无专业。</p>';
  }
  const selectedCount = currentGroups[groupIndex]?.majorCodes.length ?? 0;
  return `
    <div class="major-table-wrap">
      <table class="major-table">
        <thead>
          <tr>
            <th>${mode === "selected" ? "顺序" : "—"}</th>
            <th>专业代码</th>
            <th>专业名称</th>
            <th>选科要求</th>
            <th>最低分</th>
            <th>最低位次</th>
            <th>专业录取概率</th>
            <th>学费</th>
            <th>学制</th>
            <th>招生人数</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          ${majors
            .map((major, index) => {
              const selected = currentGroups[groupIndex]?.majorCodes.includes(
                major.majorCode,
              );
              return `
                <tr class="${major.selectable ? "" : "major-disabled"}">
                  <td data-label="顺序">${mode === "selected" ? index + 1 : "—"}</td>
                  <td data-label="专业代码">${escapeHtml(major.majorCode)}</td>
                  <td data-label="专业名称">
                    ${escapeHtml(major.majorName)}
                    ${major.recommended ? '<span class="major-badge">默认</span>' : ""}
                    ${!major.selectable ? `<small class="major-reason">${escapeHtml(major.unavailableReason || "不可填报")}</small>` : ""}
                  </td>
                  <td data-label="选科要求">${escapeHtml(major.subjectReq || "暂无数据")}</td>
                  <td data-label="最低分">${major.minScore ?? "暂无数据"}${major.referenceYear ? `<small>${major.referenceYear}年</small>` : ""}</td>
                  <td data-label="最低位次">${major.minRank ?? "暂无数据"}${major.referenceYear ? `<small>${major.referenceYear}年</small>` : ""}</td>
                  <td data-label="专业录取概率">${formatMajorProbability(major)}</td>
                  <td data-label="学费">${formatTuition(major)}</td>
                  <td data-label="学制">${escapeHtml(major.duration || "暂无数据")}</td>
                  <td data-label="招生人数">${major.planCount ?? "暂无数据"}</td>
                  <td class="major-actions" data-label="操作">
                    <div class="major-action-buttons">
                      ${
                        selected
                          ? `
                            <button data-action="move-up" data-group-index="${groupIndex}" data-code="${escapeHtml(major.majorCode)}" ${index === 0 ? "disabled" : ""}>上移</button>
                            <button data-action="move-down" data-group-index="${groupIndex}" data-code="${escapeHtml(major.majorCode)}" ${index === majors.length - 1 ? "disabled" : ""}>下移</button>
                            <button data-action="remove" data-group-index="${groupIndex}" data-code="${escapeHtml(major.majorCode)}" ${selectedCount <= 1 ? "disabled" : ""}>移除</button>
                          `
                          : `
                            <button data-action="add" data-group-index="${groupIndex}" data-code="${escapeHtml(major.majorCode)}" ${!major.selectable || selectedCount >= 6 ? "disabled" : ""}>加入</button>
                          `
                      }
                    </div>
                  </td>
                </tr>
              `;
            })
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderPrintMajorList(majors: ReportMajor[]): string {
  if (!majors.length) return "";
  return `
    <div class="print-major-list">
      ${majors
        .map(
          (major, index) => `
            <article class="print-major">
              <div class="print-major-head">
                <span class="print-major-order">${index + 1}</span>
                <div class="print-major-identity">
                  <div class="print-major-name-line">
                    <strong>${escapeHtml(major.majorName)}</strong>
                    <span class="print-major-code">专业代码 ${escapeHtml(major.majorCode)}</span>
                  </div>
                  ${major.recommended ? '<span class="print-major-default">系统默认推荐</span>' : ""}
                </div>
                <div class="print-major-probability">
                  <span>专业录取概率</span>
                  <strong>${escapeHtml(printMajorProbability(major))}</strong>
                </div>
              </div>
              <div class="print-major-meta">
                <div class="print-major-subject">
                  <span>选科要求</span>
                  <strong>${escapeHtml(major.subjectReq || "暂无数据")}</strong>
                </div>
                <div>
                  <span>最低分</span>
                  <strong>${major.minScore ?? "暂无数据"}${major.referenceYear ? `（${major.referenceYear}年）` : ""}</strong>
                </div>
                <div>
                  <span>最低位次</span>
                  <strong>${major.minRank ?? "暂无数据"}${major.referenceYear ? `（${major.referenceYear}年）` : ""}</strong>
                </div>
                <div>
                  <span>学费</span>
                  <strong>${escapeHtml(formatTuition(major))}</strong>
                </div>
                <div>
                  <span>学制</span>
                  <strong>${escapeHtml(major.duration || "暂无数据")}</strong>
                </div>
                <div>
                  <span>招生人数</span>
                  <strong>${major.planCount ?? "暂无数据"}</strong>
                </div>
              </div>
            </article>
          `,
        )
        .join("")}
    </div>
  `;
}

function printMajorProbability(major: ReportMajor): string {
  if (major.admissionProbability === undefined) {
    return "暂无可可靠估计";
  }
  return `${Math.round(major.admissionProbability * 100)}%`;
}

function bindDraftActions(): void {
  document.querySelectorAll<HTMLButtonElement>("[data-action]").forEach(
    (button) => {
      button.addEventListener("click", () => {
        const action = button.dataset.action;
        const groupIndex = Number(button.dataset.groupIndex);
        const code = button.dataset.code ?? "";
        const sectionKey = button.dataset.sectionKey ?? "";
        if (action === "add-group") {
          addDraftGroup(sectionKey);
          return;
        }
        if (action === "remove-group" && currentGroups[groupIndex]) {
          removeDraftGroup(groupIndex);
          return;
        }
        if (
          (action === "group-move-up" || action === "group-move-down") &&
          currentGroups[groupIndex]
        ) {
          moveDraftGroupWithinTier(
            groupIndex,
            action === "group-move-up" ? -1 : 1,
          );
          return;
        }
        const group = currentGroups[groupIndex];
        if (!group) return;
        if (action === "toggle-remaining") {
          const key = groupKey(group);
          if (expandedGroups.has(key)) expandedGroups.delete(key);
          else expandedGroups.add(key);
          paintReport();
          return;
        }
        const currentIndex = group.majorCodes.indexOf(code);
        if (action === "add") {
          if (group.majorCodes.length >= 6) {
            statusMessage = "每组最多选择 6 个专业，请先移除一个专业。";
          } else {
            group.majorCodes.push(code);
            dirty = true;
            statusMessage = "";
          }
        } else if (action === "remove" && currentIndex >= 0) {
          if (group.majorCodes.length <= 1) {
            statusMessage = "每个志愿组至少保留 1 个拟填专业。";
          } else {
            group.majorCodes.splice(currentIndex, 1);
            dirty = true;
            statusMessage = "";
          }
        } else if (action === "move-up" && currentIndex > 0) {
          [group.majorCodes[currentIndex - 1], group.majorCodes[currentIndex]] =
            [group.majorCodes[currentIndex], group.majorCodes[currentIndex - 1]];
          dirty = true;
          statusMessage = "";
        } else if (
          action === "move-down" &&
          currentIndex >= 0 &&
          currentIndex < group.majorCodes.length - 1
        ) {
          [group.majorCodes[currentIndex + 1], group.majorCodes[currentIndex]] =
            [group.majorCodes[currentIndex], group.majorCodes[currentIndex + 1]];
          dirty = true;
          statusMessage = "";
        }
        paintReport();
      });
    },
  );
  document
    .getElementById("save-draft")
    ?.addEventListener("click", () => void persistDraft());
  document
    .getElementById("export-pdf")
    ?.addEventListener("click", () => void exportPdf());
}

function moveDraftGroupWithinTier(
  groupIndex: number,
  direction: -1 | 1,
): void {
  const run = currentRun;
  const group = currentGroups[groupIndex];
  if (!run || !group) return;
  const identity = groupKey(group);
  const school = candidateSections(run).find(
    (candidate) =>
      `${candidate.universityCode}|${candidate.majorGroup}` === identity,
  );
  if (!school) return;
  const tierKeys = new Set(
    candidateSections(run)
      .filter((candidate) => candidate.tier === school.tier)
      .map((candidate) => `${candidate.universityCode}|${candidate.majorGroup}`),
  );
  const tierOrder = currentGroups
    .map((item) => groupKey(item))
    .filter((key) => tierKeys.has(key));
  const position = tierOrder.indexOf(identity);
  const neighborIdentity = tierOrder[position + direction];
  if (!neighborIdentity) return;
  const [moved] = currentGroups.splice(groupIndex, 1);
  const neighborIndex = currentGroups.findIndex(
    (item) => groupKey(item) === neighborIdentity,
  );
  if (neighborIndex < 0) return;
  currentGroups.splice(
    neighborIndex + (direction > 0 ? 1 : 0),
    0,
    moved,
  );
  dirty = true;
  statusMessage = "已调整本档专业组顺序。";
  paintReport();
}

function addDraftGroup(sectionKey: string): void {
  const run = currentRun;
  if (!run || !sectionKey) return;
  const school = candidateSections(run).find(
    (section) => section.sectionKey === sectionKey,
  );
  if (!school || !school.universityCode || !school.majorGroup) return;
  const identity = `${school.universityCode}|${school.majorGroup}`;
  if (currentGroups.some((group) => groupKey(group) === identity)) return;
  if (currentGroups.length >= 45) {
    statusMessage = "志愿表最多保留 45 个院校专业组。";
    paintReport();
    return;
  }
  const cached = removedGroupSelections.get(identity);
  currentGroups.push({
    universityCode: school.universityCode,
    majorGroup: school.majorGroup,
    majorCodes: cached?.length
      ? [...cached]
      : defaultMajorCodes(school),
  });
  removedGroupSelections.delete(identity);
  dirty = true;
  statusMessage = "";
  paintReport();
}

function removeDraftGroup(groupIndex: number): void {
  const group = currentGroups[groupIndex];
  if (!group) return;
  removedGroupSelections.set(groupKey(group), [...group.majorCodes]);
  currentGroups.splice(groupIndex, 1);
  dirty = true;
  statusMessage = currentGroups.length
    ? ""
    : "当前尚未保留专业组，至少加入 1 组后才能保存。";
  paintReport();
}

async function persistDraft(): Promise<boolean> {
  const formal = currentRun?.formalResult;
  if (!formal || !canSaveDraft(currentGroups)) return false;
  const button = document.getElementById("save-draft") as
    | HTMLButtonElement
    | null;
  if (button) {
    button.disabled = true;
    button.textContent = "保存中…";
  }
  let saved = false;
  try {
    const response = await saveWishlistDraft(
      formal.resultVersion,
      currentDraft?.draftVersion ?? 0,
      `wishlist-${formal.resultVersion}-${crypto.randomUUID()}`,
      currentGroups,
    );
    currentDraft = response;
    currentGroups = mergeDraftWithDefaults(
      currentRun?.formalResult?.report.sections.filter((section) =>
        section.sectionKey.startsWith("candidate:"),
      ) ?? [],
      response,
    );
    removedGroupSelections.clear();
    dirty = false;
    statusMessage = `已保存草稿版本 ${response.draftVersion}`;
    saved = true;
  } catch (cause) {
    statusMessage =
      cause instanceof GatewayError && cause.status === 409
        ? "草稿已在其他页面更新，请刷新后重新编辑。"
        : `保存失败：${cause instanceof Error ? cause.message : String(cause)}`;
  }
  paintReport();
  return saved;
}

async function exportPdf(): Promise<void> {
  const formal = currentRun?.formalResult;
  if (!formal || !canExportPdf(formal)) {
    statusMessage = "当前正式结果已失效，不能生成新的 PDF。";
    paintReport();
    return;
  }
  if (!currentDraft || dirty) {
    const saved = await persistDraft();
    if (!saved) return;
  }
  if (!currentDraft || dirty || !canExportPdf(formal)) return;
  window.print();
}

function preparePrint(): void {
  const formal = currentRun?.formalResult;
  const blocked =
    !formal ||
    !canExportPdf(formal) ||
    !currentDraft ||
    dirty ||
    !canSaveDraft(currentGroups);
  titleBeforePrint = document.title;
  document.body.classList.toggle("print-blocked", blocked);
  document.title = blocked
    ? "知愿-志愿报告-请先保存草稿"
    : pdfFilename(currentDraft?.draftVersion ?? 0);
}

function finishPrint(): void {
  document.body.classList.remove("print-blocked");
  document.title = titleBeforePrint;
}

function statusText(): string {
  const formal = currentRun?.formalResult;
  if (formal && !canExportPdf(formal)) {
    return "正式结果已失效，不能生成新的 PDF";
  }
  if (statusMessage) return statusMessage;
  if (dirty) {
    return currentGroups.length
      ? `有未保存修改，当前保留 ${currentGroups.length} 组`
      : "尚未保留专业组，不能保存";
  }
  return currentDraft
    ? `已恢复草稿版本 ${currentDraft.draftVersion}`
    : "当前为系统默认志愿表";
}

function formatMajorProbability(major: ReportMajor): string {
  if (major.admissionProbability === undefined) {
    return "暂无可可靠估计";
  }
  return `${Math.round(major.admissionProbability * 100)}%`;
}

function formatTuition(major: ReportMajor): string {
  return major.tuition === undefined
    ? "暂无数据"
    : `${Math.round(major.tuition).toLocaleString("zh-CN")} 元/年`;
}

function decodeFragment(hash: string): string {
  if (!hash.startsWith("#tier-")) return "";
  try {
    return decodeURIComponent(hash.slice("#tier-".length));
  } catch {
    return "";
  }
}

function formatPercentile(ratio: string | undefined): string {
  if (!ratio) return "—";
  const value = Number(ratio);
  if (!Number.isFinite(value)) return "—";
  return `前 ${Number((value * 100).toFixed(2))}%`;
}

function formatHistoryPercentile(percentile: number | null): string {
  return percentile === null
    ? "—"
    : `${Number((percentile * 100).toFixed(3))}%`;
}

function renderPlanAdjustmentNote(
  school: ReturnType<typeof candidateSections>[number],
): string {
  if (school.previousPlanCount === undefined) {
    return "";
  }
  const currentPlan = Number(school.planCount);
  const previousPlan = school.previousPlanCount;
  const delta = currentPlan - previousPlan;
  const changeRate =
    previousPlan > 0 ? Math.abs(delta / previousPlan) * 100 : 0;
  const changeText =
    delta === 0
      ? "计划数与2024年持平"
      : delta < 0
        ? `计划数减少 ${Math.abs(delta)} 人（${changeRate.toFixed(1)}%），可能使实际录取难度上升`
        : `计划数增加 ${delta} 人（${changeRate.toFixed(1)}%），可能使实际录取难度下降`;
  return `
    <p class="history-adjustment-note">
      2025年计划 ${escapeHtml(school.planCount)} 人，2024年计划 ${previousPlan} 人；
      ${changeText}。为保持历史口径稳定，计划变化不修改历史百分位和概率数值。
    </p>
  `;
}

function renderHistoryComparison(
  historicalPercentile: number | null,
  userPercentile: number | undefined,
): string {
  if (
    historicalPercentile === null ||
    userPercentile === undefined ||
    !Number.isFinite(userPercentile)
  ) {
    return '<span class="history-comparison">—</span>';
  }
  const difference = historicalPercentile - userPercentile;
  if (difference >= historyCriticalMargin) {
    return '<span class="history-comparison hit">命中</span>';
  }
  if (difference > -historyCriticalMargin) {
    return '<span class="history-comparison critical">临界</span>';
  }
  return '<span class="history-comparison">未命中</span>';
}

function renderUnavailableReport(): void {
  root.innerHTML = `
    <div class="report-error">
      <h1>无法打开推荐报告</h1>
      <p>该报告不存在，或不属于当前登录账号。链接可能来自认证上线前的匿名会话。</p>
      <p class="muted">请从当前账号的“我的志愿表”或最近对话中打开已有方案。</p>
      <div class="button-row">
        <a class="primary-button" href="/wishlists.html">打开我的志愿表</a>
        <a class="secondary-button" href="/">返回对话</a>
      </div>
    </div>
  `;
}

function renderError(message: string): void {
  root.innerHTML = `
    <div class="report-error">
      <h1>无法打开推荐报告</h1>
      <p>${escapeHtml(message)}</p>
      <a class="primary-button" href="/">返回对话</a>
    </div>
  `;
}
