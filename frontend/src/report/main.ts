import "../style.css";

import { getRun, GatewayError } from "../api/gateway";
import type { RunView } from "../api/types";
import {
  candidateSections,
  escapeHtml,
  safeSourceUrl,
  schoolDisplayTitle,
} from "./model";

const rootElement = document.querySelector<HTMLDivElement>("#report");
if (!rootElement) throw new Error("REPORT_ROOT_MISSING");
const root: HTMLDivElement = rootElement;

void loadReport();

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
    renderReport(run);
  } catch (cause) {
    renderError(
      cause instanceof GatewayError
        ? `读取报告失败（${cause.status}）：${cause.message}`
        : cause instanceof Error
          ? cause.message
          : String(cause),
    );
  }
}

function renderReport(run: RunView): void {
  const formal = run.formalResult;
  if (!formal) return;
  const candidates = candidateSections(run);
  const groups = (["冲", "稳", "保"] as const).map((tier) => ({
    tier,
    schools: candidates.filter((candidate) => candidate.tier === tier),
  }));
  const plan = formal.report.sections.find(
    (section) => section.sectionKey === "plan",
  );
  const qualification = formal.report.sections.find(
    (section) => section.sectionKey === "qualification",
  );
  const risk = formal.report.sections.find(
    (section) => section.sectionKey === "risk",
  );
  const dataGaps = formal.report.sections.find(
    (section) => section.sectionKey === "data_gaps",
  );
  const rank = plan?.body.match(/位次\s*(\d+)\s*名/)?.[1] ?? "—";
  const percentile = plan?.body.match(/百分位\s*([0-9.]+)/)?.[1] ?? "—";

  root.innerHTML = `
    <header class="report-topbar">
      <a href="/">← 返回对话</a>
      <strong>知愿 · 志愿推荐报告</strong>
      <span class="muted">${escapeHtml(formal.resultVersion)}</span>
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
        <a href="#qualification">资格与数据</a>
        <a href="#risk">风险提示</a>
      </nav>
      <article class="report-article">
        <span class="report-kicker">PERSONAL PLAN · 湖南 2025 本科批</span>
        <h1>你的冲稳保志愿报告</h1>
        <p class="report-lede">${escapeHtml(plan?.body || "正式报告已生成。")}</p>
        <div class="report-summary">
          <div><small>预估位次</small><strong>${escapeHtml(rank)}</strong></div>
          <div><small>历史百分位</small><strong>${escapeHtml(percentile)}</strong></div>
          <div><small>推荐专业组</small><strong>${candidates.length}</strong></div>
          <div><small>引用覆盖率</small><strong>${Math.round(formal.citationCoverage * 100)}%</strong></div>
        </div>
        ${groups
          .map(
            ({ tier, schools }) => `
              <section class="report-section" id="tier-${tier}">
                <h2>${tier === "冲" ? "冲刺院校" : tier === "稳" ? "稳妥院校" : "保底院校"}</h2>
                <p>${
                  tier === "冲"
                    ? "适合放置在志愿表前段，仍需重点核对热门专业的组内规则。"
                    : tier === "稳"
                      ? "与当前位次更接近，通常应作为方案主体。"
                      : "用于给方案留下可接受的落点，不代表保证录取。"
                }</p>
                <div class="school-list">
                  ${
                    schools.length
                      ? schools
                          .map(
                            (school) => `
                              <a class="school-row" href="school.html?run=${encodeURIComponent(run.runId)}&section=${encodeURIComponent(school.sectionKey)}">
                                <span class="tier ${tier === "稳" ? "stable" : tier === "保" ? "safe" : ""}">${tier}</span>
                                <div>
                                  <h3>${escapeHtml(schoolDisplayTitle(school))}</h3>
                                  <p>${escapeHtml(school.body)}</p>
                                </div>
                                <div>
                                  <span class="muted">历史估算</span>
                                  <div class="probability">${escapeHtml(school.probability)}%</div>
                                </div>
                              </a>
                            `,
                          )
                          .join("")
                      : '<p class="muted">该梯度暂无正式候选。</p>'
                  }
                </div>
              </section>
            `,
          )
          .join("")}
        <section class="report-section" id="qualification">
          <h2>资格核验</h2>
          <p>${escapeHtml(qualification?.body || "资格状态未知。")}</p>
          <p class="muted">${escapeHtml(dataGaps?.body || "无待核验数据。")}</p>
        </section>
        <section class="report-section" id="risk">
          <h2>风险提示</h2>
          <div class="risk-note">${escapeHtml(risk?.body || formal.riskNotice)}</div>
        </section>
      </article>
      <aside class="report-aside">
        <section>
          <h3>报告身份</h3>
          <p>release: ${escapeHtml(formal.releaseId)}<br />Profile: ${escapeHtml(formal.confirmedProfileVersion)}</p>
        </section>
        <section>
          <h3>验证状态</h3>
          <p class="report-state">${escapeHtml(formal.verificationStatus)}</p>
          <p>引用覆盖率 ${Math.round(formal.citationCoverage * 100)}%</p>
        </section>
        <section>
          <h3>重要说明</h3>
          <p>${escapeHtml(formal.noAdmissionGuarantee)}</p>
        </section>
      </aside>
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
