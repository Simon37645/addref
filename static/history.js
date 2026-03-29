const HISTORY_POLL_INTERVAL_MS = 1200;

const els = {
  sessionEmail: document.getElementById("session-email"),
  authLink: document.getElementById("auth-link"),
  logoutButton: document.getElementById("logout-button"),
  accountEmail: document.getElementById("account-email"),
  accountUsage: document.getElementById("account-usage"),
  refreshHistory: document.getElementById("refresh-history"),
  historyRetention: document.getElementById("history-retention"),
  jobHistory: document.getElementById("job-history"),
  jobTitle: document.getElementById("job-title"),
  jobStatusBadge: document.getElementById("job-status-badge"),
  jobSummary: document.getElementById("job-summary"),
  progressFill: document.getElementById("progress-fill"),
  progressStage: document.getElementById("progress-stage"),
  progressDetail: document.getElementById("progress-detail"),
  progressHistory: document.getElementById("progress-history"),
  annotatedText: document.getElementById("annotated-text"),
  referenceList: document.getElementById("reference-list"),
  traceList: document.getElementById("trace-list"),
};

const state = {
  session: { authenticated: false },
  jobs: [],
  retentionHours: 24,
  selectedJobId: "",
  pollToken: 0,
};

initialize();

async function initialize() {
  wireEvents();
  renderEmptyDetail();
  renderJobHistory();
  await refreshSession();
  if (!state.session.authenticated) {
    return;
  }
  await refreshJobHistory();
  await loadInitialSelection();
}

function wireEvents() {
  els.refreshHistory.addEventListener("click", () => refreshJobHistory({ silent: false }));
  els.jobHistory.addEventListener("click", handleHistoryClick);
  els.logoutButton.addEventListener("click", handleLogout);
  window.addEventListener("popstate", loadInitialSelection);
}

async function refreshSession() {
  try {
    state.session = await window.AddRefSessionClient.fetchSession();
  } catch (error) {
    state.session = { authenticated: false };
  }
  window.AddRefSessionClient.applySessionChrome(state.session, els);
  renderJobHistory();
}

async function handleLogout() {
  await window.AddRefSessionClient.logout();
  state.session = { authenticated: false };
  state.jobs = [];
  state.selectedJobId = "";
  state.pollToken += 1;
  window.AddRefSessionClient.applySessionChrome(state.session, els);
  renderJobHistory();
  renderEmptyDetail();
}

async function refreshJobHistory({ silent = true } = {}) {
  if (!state.session.authenticated) {
    state.jobs = [];
    renderJobHistory();
    return;
  }

  try {
    const response = await fetch("/api/cite-jobs", { credentials: "same-origin" });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "无法获取历史任务。");
    }
    state.jobs = Array.isArray(data.jobs) ? data.jobs : [];
    state.retentionHours = Number.parseInt(data.retention_hours || 24, 10) || 24;
    renderJobHistory();
  } catch (error) {
    if (!silent) {
      renderSummaryMessage(error.message || "无法获取历史任务。", "error");
    }
  }
}

async function loadInitialSelection() {
  if (!state.session.authenticated) {
    return;
  }

  const queryJobId = new URLSearchParams(window.location.search).get("job") || "";
  const targetJobId = queryJobId || state.selectedJobId || (state.jobs[0] ? state.jobs[0].job_id : "");
  if (!targetJobId) {
    renderEmptyDetail();
    return;
  }
  await loadJob(targetJobId, { replaceUrl: !queryJobId });
}

async function handleHistoryClick(event) {
  const trigger = event.target.closest(".job-history-item");
  if (!trigger) {
    return;
  }
  const jobId = trigger.dataset.jobId || "";
  if (!jobId) {
    return;
  }
  await loadJob(jobId);
}

async function loadJob(jobId, { replaceUrl = false } = {}) {
  if (!jobId) {
    return;
  }
  state.pollToken += 1;
  const pollToken = state.pollToken;
  try {
    const job = await fetchJob(jobId);
    state.selectedJobId = jobId;
    syncUrl(jobId, { replace: replaceUrl });
    upsertJob(job);
    renderJobHistory();
    renderJobDetail(job);
    if (job.status === "queued" || job.status === "running") {
      startPolling(jobId, pollToken);
    }
  } catch (error) {
    renderSummaryMessage(error.message || "无法打开任务。", "error");
  }
}

async function fetchJob(jobId) {
  const response = await fetch(`/api/cite-jobs/${encodeURIComponent(jobId)}`, {
    credentials: "same-origin",
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || "无法获取任务。");
    error.status = response.status;
    throw error;
  }
  return data;
}

async function startPolling(jobId, pollToken) {
  while (pollToken === state.pollToken && state.selectedJobId === jobId) {
    await wait(HISTORY_POLL_INTERVAL_MS);
    if (pollToken !== state.pollToken || state.selectedJobId !== jobId) {
      return;
    }
    try {
      const job = await fetchJob(jobId);
      upsertJob(job);
      renderJobHistory();
      renderJobDetail(job);
      if (job.status !== "queued" && job.status !== "running") {
        return;
      }
    } catch (error) {
      renderSummaryMessage(error.message || "任务轮询失败。", "error");
      return;
    }
  }
}

function upsertJob(job) {
  const next = state.jobs.slice();
  const summary = summarizeJob(job);
  const index = next.findIndex((item) => item.job_id === summary.job_id);
  if (index >= 0) {
    next[index] = { ...next[index], ...summary };
  } else {
    next.unshift(summary);
  }
  state.jobs = next.sort((left, right) => timestamp(right.updated_at || right.created_at) - timestamp(left.updated_at || left.created_at));
}

function renderJobHistory() {
  els.historyRetention.textContent = `保留最近 ${state.retentionHours} 小时`;
  if (!state.session.authenticated) {
    els.jobHistory.innerHTML = "请先登录后查看历史任务";
    els.jobHistory.className = "job-history empty-state";
    return;
  }
  if (!state.jobs.length) {
    els.jobHistory.innerHTML = "最近 24 小时暂无任务";
    els.jobHistory.className = "job-history empty-state";
    return;
  }

  els.jobHistory.className = "job-history";
  els.jobHistory.innerHTML = state.jobs
    .map((job) => {
      const active = job.job_id === state.selectedJobId;
      const preview = escapeHtml(job.source_text_preview || job.detail || job.message || "无预览");
      const meta = buildJobMeta(job)
        .map((item) => `<span>${escapeHtml(item)}</span>`)
        .join("");
      return `
        <button type="button" class="job-history-item${active ? " active" : ""}" data-job-id="${escapeHtml(job.job_id)}">
          <div class="job-history-top">
            <span class="job-history-status ${escapeHtml(job.status || "")}">${escapeHtml(formatJobStatus(job))}</span>
            <span class="job-history-time">${escapeHtml(formatDateTime(job.updated_at || job.created_at || ""))}</span>
          </div>
          <p class="job-history-title">${escapeHtml(job.message || "任务")}</p>
          <p class="job-history-preview">${preview}</p>
          <div class="job-history-meta">${meta}</div>
        </button>
      `;
    })
    .join("");
}

function renderJobDetail(job) {
  els.jobTitle.textContent = job.message || "任务";
  els.jobStatusBadge.textContent = formatJobStatus(job);
  els.jobStatusBadge.className = `status-badge ${mapStatusKind(job.status)}`.trim();
  els.jobSummary.innerHTML = buildSummaryHtml(job);
  renderProgress(job);

  if (job.status === "completed" && job.result) {
    renderAnnotatedText(job.result);
    renderReferences(job.result);
    renderTrace(job.result);
    return;
  }

  renderAnnotatedText(null, job.status === "failed" ? "该任务没有可用结果。" : "任务尚未完成。");
  renderReferences(null, job.status === "failed" ? "该任务没有参考文献。" : "任务完成后显示参考文献。");
  renderTrace(null, job.status === "failed" ? "该任务没有可展示轨迹。" : "任务完成后显示检索轨迹。");
}

function renderProgress(job) {
  const progressPercent = Math.max(0, Math.min(100, Number.parseInt(job.progress_percent || 0, 10) || 0));
  els.progressFill.style.width = `${progressPercent}%`;
  els.progressStage.textContent = job.message || "处理中";
  els.progressDetail.textContent = job.detail || "无详细信息";
  const history = Array.isArray(job.history) ? job.history.slice().reverse() : [];
  if (!history.length) {
    els.progressHistory.innerHTML = "暂无步骤记录";
    els.progressHistory.className = "progress-history empty-state";
    return;
  }
  els.progressHistory.className = "progress-history";
  els.progressHistory.innerHTML = history
    .map((item) => `
      <div class="progress-entry">
        <span>${escapeHtml(formatEventTime(item.time))}</span>
        <strong>${escapeHtml(item.message || "")}</strong>
      </div>
    `)
    .join("");
}

function renderAnnotatedText(result, emptyText = "选择已完成任务后显示结果") {
  if (!result) {
    els.annotatedText.textContent = emptyText;
    els.annotatedText.className = "output-box empty";
    return;
  }
  const output = buildRenderedOutput(result);
  els.annotatedText.innerHTML = highlightMarkers(output);
  els.annotatedText.className = "output-box";
}

function renderReferences(result, emptyText = "选择已完成任务后显示参考文献") {
  if (!result || !Array.isArray(result.references) || !result.references.length) {
    els.referenceList.textContent = emptyText;
    els.referenceList.className = "reference-list empty-state";
    return;
  }
  els.referenceList.className = "reference-list";
  els.referenceList.innerHTML = result.references
    .map((reference) => {
      const article = reference.article || {};
      const journalLine = [article.journal, article.year].filter(Boolean).join(" · ");
      return `
        <article class="reference-card">
          <header>
            <div>
              <h3>[${reference.marker}] ${escapeHtml(article.title || "Untitled")}</h3>
              <p>${escapeHtml(Array.isArray(article.authors) ? article.authors.join(", ") : "作者信息缺失")}</p>
            </div>
          </header>
          <div class="reference-meta">
            <span>${escapeHtml(journalLine || "期刊信息缺失")}</span>
            <span>PMID ${escapeHtml(article.pmid || "-")}</span>
          </div>
        </article>
      `;
    })
    .join("");
}

function renderTrace(result, emptyText = "选择任务后显示检索轨迹") {
  if (!result) {
    els.traceList.textContent = emptyText;
    els.traceList.className = "trace-list empty-state";
    return;
  }
  const placementCards = (result.placements || []).map((placement) => {
    const articles = Array.isArray(placement.articles) && placement.articles.length
      ? placement.articles
      : (placement.article ? [placement.article] : []);
    const articleSummary = articles.length
      ? articles
          .map((article) => `文献：${escapeHtml(article.title || "未命中标题")}（PMID ${escapeHtml(article.pmid || "-")}）`)
          .join("<br>")
      : "文献：未命中文献";
    return `
      <article class="trace-card">
        <header>
          <div>
            <h3>命中句子 ${escapeHtml(formatMarkerLabel(placement.markers || placement.marker || []))}</h3>
            <p>${escapeHtml(placement.sentence_text || "")}</p>
          </div>
          <span class="attempt-pill">${escapeHtml(placement.final_query || "")}</span>
        </header>
        <p>${articleSummary}</p>
      </article>
    `;
  });
  const unresolvedCards = (result.unresolved_targets || []).map((item) => `
    <article class="trace-card">
      <header>
        <div>
          <h3>未解析句子</h3>
          <p>${escapeHtml(item.sentence_text || "")}</p>
        </div>
      </header>
    </article>
  `);
  const combined = placementCards.concat(unresolvedCards);
  els.traceList.className = "trace-list";
  els.traceList.innerHTML = combined.join("") || "没有可展示的检索轨迹。";
}

function renderEmptyDetail() {
  els.jobTitle.textContent = "未选择任务";
  els.jobStatusBadge.textContent = "等待选择";
  els.jobStatusBadge.className = "status-badge";
  els.jobSummary.textContent = "从左侧选择任务后显示详情。";
  els.progressFill.style.width = "0%";
  els.progressStage.textContent = "等待开始";
  els.progressDetail.textContent = "任务进度会显示在这里";
  els.progressHistory.textContent = "选择任务后显示步骤记录";
  els.progressHistory.className = "progress-history empty-state";
  renderAnnotatedText(null);
  renderReferences(null);
  renderTrace(null);
}

function buildSummaryHtml(job) {
  const parts = [
    `<span>${escapeHtml(`创建于 ${formatDateTime(job.created_at || "")}`)}</span>`,
    `<span>${escapeHtml(`更新于 ${formatDateTime(job.updated_at || "")}`)}</span>`,
  ];
  if (job.detail) {
    parts.push(`<span>${escapeHtml(job.detail)}</span>`);
  }
  return `<div class="selected-job-meta">${parts.join("")}</div>`;
}

function renderSummaryMessage(message, kind) {
  els.jobSummary.innerHTML = `<div class="selected-job-meta ${escapeHtml(kind || "")}"><span>${escapeHtml(message)}</span></div>`;
}

function buildJobMeta(job) {
  const parts = [];
  if (job.placement_count || job.reference_count) {
    parts.push(`${Number.parseInt(job.placement_count || 0, 10) || 0} 处插入`);
    parts.push(`${Number.parseInt(job.reference_count || 0, 10) || 0} 条文献`);
  }
  if (job.source_text_length) {
    parts.push(`${Number.parseInt(job.source_text_length || 0, 10) || 0} 字`);
  }
  if (!parts.length) {
    parts.push("点击查看");
  }
  return parts;
}

function formatJobStatus(job) {
  const status = String(job.status || "").toLowerCase();
  if (status === "completed") {
    return "已完成";
  }
  if (status === "failed") {
    return "失败";
  }
  if (status === "queued") {
    return "排队中";
  }
  if (status === "running") {
    return `处理中 ${Number.parseInt(job.progress_percent || 0, 10) || 0}%`;
  }
  return status || "任务";
}

function mapStatusKind(status) {
  const normalized = String(status || "").toLowerCase();
  if (normalized === "completed") {
    return "done";
  }
  if (normalized === "failed") {
    return "error";
  }
  if (normalized === "queued" || normalized === "running") {
    return "running";
  }
  return "";
}

function syncUrl(jobId, { replace = false } = {}) {
  const url = new URL(window.location.href);
  if (jobId) {
    url.searchParams.set("job", jobId);
  } else {
    url.searchParams.delete("job");
  }
  if (replace) {
    window.history.replaceState({}, "", url);
  } else {
    window.history.pushState({}, "", url);
  }
}

function summarizeJob(job) {
  const result = job && job.result && typeof job.result === "object" ? job.result : null;
  const sourceText = String((result && result.source_text) || job.source_text_preview || "");
  return {
    job_id: job.job_id || "",
    status: job.status || "",
    progress_percent: Number.parseInt(job.progress_percent || 0, 10) || 0,
    message: job.message || "",
    detail: job.detail || "",
    created_at: job.created_at || "",
    updated_at: job.updated_at || "",
    placement_count: result && Array.isArray(result.placements) ? result.placements.length : (Number.parseInt(job.placement_count || 0, 10) || 0),
    reference_count: result && Array.isArray(result.references) ? result.references.length : (Number.parseInt(job.reference_count || 0, 10) || 0),
    source_text_preview: sourceText.slice(0, 160).trim(),
    source_text_length: sourceText.length || Number.parseInt(job.source_text_length || 0, 10) || 0,
  };
}

function buildRenderedOutput(result) {
  const annotatedText = String(result.annotated_text || "").trim();
  const referenceBlock = String(result.reference_block || "").trim();
  if (annotatedText && referenceBlock) {
    return `${annotatedText}\n\n${referenceBlock}`;
  }
  return annotatedText || referenceBlock;
}

function highlightMarkers(text) {
  const escaped = escapeHtml(text);
  return escaped.replace(/\[(\d+(?:\s*,\s*\d+)*)\]/g, '<span class="ref-marker">[$1]</span>');
}

function formatMarkerLabel(markers) {
  const normalized = Array.isArray(markers) ? markers : [markers];
  const values = normalized
    .map((item) => Number.parseInt(String(item || "0"), 10))
    .filter((item) => Number.isFinite(item) && item > 0);
  return values.length ? `[${values.join(", ")}]` : "[?]";
}

function formatEventTime(value) {
  if (!value) {
    return "--:--:--";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return "--:--:--";
  }
  return parsed.toLocaleTimeString("zh-CN", { hour12: false });
}

function formatDateTime(value) {
  if (!value) {
    return "--";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return "--";
  }
  return parsed.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function timestamp(value) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? 0 : parsed.getTime();
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function wait(ms) {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}
