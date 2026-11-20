import "../style.css";

import { getRun, GatewayError } from "../api/gateway";
import {
  candidateSections,
  escapeHtml,
  findReportSection,
  safeSourceUrl,
  schoolDisplayTitle,
} from "../report/model";

const rootElement = document.querySelector<HTMLDivElement>("#school");
if (!rootElement) throw new Error("SCHOOL_ROOT_MISSING");
const root: HTMLDivElement = rootElement;

void loadSchool();

async function loadSchool(): Promise<void> {
  const query = new URLSearchParams(location.search);
  const runId = query.get("run");
  const sectionKey = query.get("section");
  if (!runId || !sectionKey) {
    renderError("缺少 run 或 section 参数。");
    return;
  }
  root.innerHTML = '<div class="loading-page">正在读取学校推荐详情…</div>';
  try {
    const run = await getRun(runId);
    const section = findReportSection(run, sectionKey);
    if (!section || !sectionKey.startsWith("candidate:")) {
      renderError("该学校详情不属于这份正式报告。");
      return;
    }
    const candidate = candidateSections(run).find(
      (item) => item.sectionKey === sectionKey,
    );
    renderSchool(runId, section, candidate);
  } catch (cause) {
    renderError(
      cause instanceof GatewayError
        ? `读取失败（${cause.status}）：${cause.message}`
        : cause instanceof Error
          ? cause.message
          : String(cause),
    );
  }
}

function renderSchool(
  runId: string,
  section: NonNullable<ReturnType<typeof findReportSection>>,
  candidate:
    | ReturnType<typeof candidateSections>[number]
    | undefined,
): void {
  const citations = section.citations
    .map((citation) => {
      const source = safeSourceUrl(citation);
      return `
        <li>
          ${
            source
              ? `<a href="${escapeHtml(source)}" rel="noopener noreferrer" target="_blank">${escapeHtml(citation.label)}</a>`
              : `<strong>${escapeHtml(citation.label)}</strong>`
          }
          <p>${escapeHtml(citation.sectionPath || citation.evidenceType)} · ${escapeHtml(citation.releaseId)}</p>
        </li>
      `;
    })
    .join("");
  root.innerHTML = `
    <header class="report-topbar">
      <a href="report.html?run=${encodeURIComponent(runId)}">← 返回汇总报告</a>
      <strong>${escapeHtml(schoolDisplayTitle(section))}</strong>
      <span class="muted">学校推荐详情</span>
    </header>
    <main class="school-detail">
      <section class="school-detail-hero">
        <div>
          <span class="tier ${candidate?.tier === "稳" ? "stable" : candidate?.tier === "保" ? "safe" : ""}">${escapeHtml(candidate?.tier || "推荐")}</span>
          <h1>${escapeHtml(schoolDisplayTitle(section))}</h1>
          <p>${escapeHtml(section.body)}</p>
        </div>
        <div class="detail-score">
          <div>
            <strong>${escapeHtml(candidate?.probability || "—")}%</strong>
            <span>历史数据估算概率</span>
          </div>
        </div>
      </section>
      <div class="detail-grid">
        <div>
          <section class="detail-block">
            <h2>推荐说明</h2>
            <p>${escapeHtml(section.body)}</p>
            <p class="muted">招生计划数：${escapeHtml(candidate?.planCount || "待核验")}。录取概率只是历史数据估算，不构成录取保证。</p>
          </section>
          <section class="detail-block">
            <h2>已发布数据身份</h2>
            <p>本详情只读取同一 result_version、同一 release 的已通过门禁报告。学校身份、专业组、概率和各引用不会在页面内改写。</p>
          </section>
        </div>
        <aside>
          <section class="detail-block">
            <h2>数据出处</h2>
            <ul class="citation-list">${citations || "<li>未提供引用</li>"}</ul>
          </section>
        </aside>
      </div>
      <div class="risk-note">
        本报告为个人面试项目演示结果。正式填报前必须以湖南省教育考试院和学校当年招生章程为准。
      </div>
    </main>
  `;
}

function renderError(message: string): void {
  root.innerHTML = `
    <div class="report-error">
      <h1>无法打开学校详情</h1>
      <p>${escapeHtml(message)}</p>
      <a class="primary-button" href="/">返回对话</a>
    </div>
  `;
}
