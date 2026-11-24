import "../style.css";

import {
  getRun,
  getWishlistDraft,
  GatewayError,
} from "../api/gateway";
import type { ReportMajor, WishlistDraftGroup } from "../api/types";
import {
  candidateSections,
  candidateIntroduction,
  escapeHtml,
  findReportSection,
  schoolDisplayTitle,
} from "../report/model";
import { mergeDraftWithDefaults } from "../report/draft";

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
    if (!run.formalResult) {
      renderError("该结果不是 V1_FORMAL 正式报告。");
      return;
    }
    const section = findReportSection(run, sectionKey);
    if (!section || !sectionKey.startsWith("candidate:")) {
      renderError("该学校详情不属于这份正式报告。");
      return;
    }
    const candidate = candidateSections(run).find(
      (item) => item.sectionKey === sectionKey,
    );
    const draft = await getWishlistDraft(run.formalResult.resultVersion).catch(
      () => null,
    );
    const draftGroup = mergeDraftWithDefaults([section], draft)[0];
    renderSchool(runId, section, candidate, draftGroup);
  } catch (cause) {
    if (cause instanceof GatewayError && cause.status === 401) {
      location.replace(
        `/login.html?return=${encodeURIComponent(
          `${location.pathname}${location.search}`,
        )}`,
      );
      return;
    }
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
  draftGroup: WishlistDraftGroup | undefined,
): void {
  root.innerHTML = `
    <header class="report-topbar">
      <a href="/reports/${encodeURIComponent(runId)}">← 返回汇总报告</a>
      <strong>${escapeHtml(schoolDisplayTitle(section))}</strong>
      <span class="muted">学校推荐详情</span>
    </header>
    <main class="school-detail">
      <section class="school-detail-hero">
        <div>
          <span class="tier ${candidate?.tier === "稳" ? "stable" : candidate?.tier === "保" ? "safe" : ""}">${escapeHtml(candidate?.tier || "推荐")}</span>
          <h1>${escapeHtml(schoolDisplayTitle(section))}</h1>
          <p>当前画像下按历史录取数据估算为${escapeHtml(candidate?.tier || "推荐")}梯度，具体组内专业与计划如下。</p>
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
            <h2>组内专业与计划</h2>
            <p>${escapeHtml(candidateIntroduction(section))}</p>
            ${renderSelectedMajors(section, draftGroup)}
            <p class="muted">录取概率只是历史数据估算，不构成录取保证。</p>
          </section>
          <section class="detail-block">
            <h2>2022–2024 专业组历史录取参考</h2>
            ${
              candidate?.history.length
                ? `
                  <table class="history-table">
                    <thead><tr><th>年份</th><th>专业组最低录取分</th><th>最低位次</th></tr></thead>
                    <tbody>
                      ${candidate.history
                        .map(
                          (history) => `
                            <tr>
                              <td>${history.year}</td>
                              <td class="${history.minScore === null ? "missing" : ""}">${history.minScore ?? "暂无数据"}</td>
                              <td class="${history.minRank === null ? "missing" : ""}">${history.minRank ?? "暂无数据"}</td>
                            </tr>
                          `,
                        )
                        .join("")}
                    </tbody>
                  </table>
                  <p class="history-note">最低录取分与最低位次取自同一条专业组历史记录。“暂无数据”表示暂无可核验的历史记录，不代表该组当年未招生。</p>
                `
                : '<p class="muted">该旧版报告未包含历年专业组录取参考。</p>'
            }
          </section>
          <section class="detail-block">
            <h2>已发布数据身份</h2>
            <p>本详情只读取同一 result_version、同一 release 的已通过门禁报告。学校身份、专业组、概率和各引用不会在页面内改写。</p>
          </section>
        </div>
      </div>
      <div class="risk-note">
        本报告为个人面试项目演示结果。正式填报前必须以湖南省教育考试院和学校当年招生章程为准。
      </div>
    </main>
  `;
}

function renderSelectedMajors(
  section: NonNullable<ReturnType<typeof findReportSection>>,
  draftGroup: WishlistDraftGroup | undefined,
): string {
  const majors = (draftGroup?.majorCodes ?? [])
    .map((code) =>
      section.majors?.find((major) => major.majorCode === code),
    )
    .filter((major): major is ReportMajor => Boolean(major));
  if (!majors.length) {
    return '<p class="muted">该报告没有可展示的结构化拟填专业。</p>';
  }
  return `
    <div class="major-table-wrap">
      <table class="major-table">
        <thead>
          <tr>
            <th>顺序</th><th>专业代码</th><th>专业名称</th>
            <th>选科要求</th><th>最低分</th><th>最低位次</th>
            <th>专业录取概率</th><th>学费</th><th>学制</th><th>招生人数</th>
          </tr>
        </thead>
        <tbody>
          ${majors
            .map(
              (major, index) => `
                <tr>
                  <td>${index + 1}</td>
                  <td>${escapeHtml(major.majorCode)}</td>
                  <td>${escapeHtml(major.majorName)}</td>
                  <td>${escapeHtml(major.subjectReq || "暂无数据")}</td>
                  <td>${major.minScore ?? "暂无数据"}${major.referenceYear ? `<small>${major.referenceYear}年</small>` : ""}</td>
                  <td>${major.minRank ?? "暂无数据"}${major.referenceYear ? `<small>${major.referenceYear}年</small>` : ""}</td>
                  <td>${major.admissionProbability === undefined ? "暂无可可靠估计" : `${Math.round(major.admissionProbability * 100)}%`}</td>
                  <td>${major.tuition === undefined ? "暂无数据" : `${Math.round(major.tuition).toLocaleString("zh-CN")} 元/年`}</td>
                  <td>${escapeHtml(major.duration || "暂无数据")}</td>
                  <td>${major.planCount ?? "暂无数据"}</td>
                </tr>
              `,
            )
            .join("")}
        </tbody>
      </table>
    </div>
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
