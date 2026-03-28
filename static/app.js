const SOURCE_TEXT_KEY = "addref-workspace-source-v2";
const JOB_POLL_INTERVAL_MS = 1200;
const LAST_JOB_KEY = "addref-last-job-v1";

const els = {
  sourceText: document.getElementById("source-text"),
  runButton: document.getElementById("run-button"),
  runButtonBottom: document.getElementById("run-button-bottom"),
  continueButton: document.getElementById("continue-button"),
  continueButtonBottom: document.getElementById("continue-button-bottom"),
  clearText: document.getElementById("clear-text"),
  fillSample: document.getElementById("fill-sample"),
  statusBadge: document.getElementById("status-badge"),
  messageStrip: document.getElementById("message-strip"),
  annotatedText: document.getElementById("annotated-text"),
  referenceList: document.getElementById("reference-list"),
  traceList: document.getElementById("trace-list"),
  copyResult: document.getElementById("copy-result"),
  exportSelected: document.getElementById("export-selected"),
  exportAll: document.getElementById("export-all"),
  sessionEmail: document.getElementById("session-email"),
  authLink: document.getElementById("auth-link"),
  logoutButton: document.getElementById("logout-button"),
  accountEmail: document.getElementById("account-email"),
  accountUsage: document.getElementById("account-usage"),
  progressPercent: document.getElementById("progress-percent"),
  progressStage: document.getElementById("progress-stage"),
  progressDetail: document.getElementById("progress-detail"),
  progressFill: document.getElementById("progress-fill"),
  progressHistory: document.getElementById("progress-history"),
  progressToggle: document.getElementById("progress-toggle"),
};

const state = {
  result: null,
  running: false,
  session: { authenticated: false },
  currentJobId: "",
  progressExpanded: false,
  progressJob: null,
  progressJobId: "",
};

initialize();

async function initialize() {
  restoreDraft();
  wireEvents();
  renderResult(null);
  renderProgress(null);
  toggleActionState(false);
  await Promise.all([fetchHealth(), refreshSession()]);
  await restoreLastJob();
}

function wireEvents() {
  els.sourceText.addEventListener("input", persistDraft);
  els.runButton.addEventListener("click", () => runCitationFlow({ continueExisting: false }));
  els.runButtonBottom.addEventListener("click", () => runCitationFlow({ continueExisting: false }));
  els.continueButton.addEventListener("click", () => runCitationFlow({ continueExisting: true }));
  els.continueButtonBottom.addEventListener("click", () => runCitationFlow({ continueExisting: true }));
  els.clearText.addEventListener("click", clearDraft);
  els.fillSample.addEventListener("click", fillSampleText);
  els.copyResult.addEventListener("click", copyAnnotatedText);
  els.exportSelected.addEventListener("click", () => exportRis(true));
  els.exportAll.addEventListener("click", () => exportRis(false));
  els.logoutButton.addEventListener("click", handleLogout);
  els.progressToggle.addEventListener("click", toggleProgressHistory);
}

async function fetchHealth() {
  try {
    const response = await fetch("/api/health");
    if (!response.ok) {
      throw new Error("health");
    }
    setStatus("服务已就绪", "done");
  } catch (error) {
    setStatus("服务未连通", "error");
  }
}

async function refreshSession() {
  try {
    state.session = await window.AddRefSessionClient.fetchSession();
  } catch (error) {
    state.session = { authenticated: false };
  }
  window.AddRefSessionClient.applySessionChrome(state.session, els);
}

async function handleLogout() {
  await window.AddRefSessionClient.logout();
  state.session = { authenticated: false };
  state.running = false;
  state.currentJobId = "";
  window.AddRefSessionClient.applySessionChrome(state.session, els);
  setMessage("已退出登录。", "success");
}

function restoreDraft() {
  const draft = localStorage.getItem(SOURCE_TEXT_KEY);
  els.sourceText.value = draft || "";
}

function persistDraft() {
  localStorage.setItem(SOURCE_TEXT_KEY, els.sourceText.value);
}

function clearDraft() {
  els.sourceText.value = "";
  persistDraft();
}

function fillSampleText() {
  els.sourceText.value =
    "肠道菌群失衡与炎症性肠病的发生和进展密切相关。越来越多的研究提示，特定短链脂肪酸产生菌的减少会削弱肠道屏障功能，并放大黏膜免疫反应。粪菌移植在部分复发性艰难梭菌感染患者中显示出较高的临床缓解率，但其在炎症性肠病中的疗效和安全性仍存在差异。未来需要更多随机对照试验来明确菌群干预在不同疾病亚型中的最佳应用策略。";
  persistDraft();
}

async function runCitationFlow({ continueExisting = false } = {}) {
  if (state.running) {
    return;
  }

  if (!state.session.authenticated) {
    setMessage("请先登录。", "error");
    window.location.href = "/auth";
    return;
  }

  if (continueExisting) {
    ensureContinueAvailable();
  }

  persistDraft();
  state.running = true;
  state.currentJobId = "";
  if (!continueExisting) {
    state.result = null;
    clearStoredJobId();
    renderResult(null);
  }
  renderProgress(null);
  setStatus("准备中", "running");
  setMessage(continueExisting ? "继续添加任务提交中。" : "任务提交中。", "success");
  toggleActionState(true);

  try {
    const config = window.AddRefConfigStore.loadConfig();
    const budgetError = validateRunBudget(config);
    if (budgetError) {
      throw new Error(budgetError);
    }
    const payload = buildCitationPayload(config, { continueExisting });

    const response = await fetch("/api/cite-jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      credentials: "same-origin",
    });

    const data = await response.json();
    if (!response.ok) {
      if (response.status === 401) {
        await refreshSession();
      }
      throw new Error(data.error || "处理失败。");
    }

    state.currentJobId = data.job_id || "";
    storeJobId(state.currentJobId);
    renderProgress(data);
    setStatus("处理中 0%", "running");
    setMessage(data.message || (continueExisting ? "继续添加任务已创建。" : "任务已创建。"), "success");
    await pollCitationJob(state.currentJobId);
  } catch (error) {
    setStatus("出错", "error");
    setMessage(error.message || "处理失败。", "error");
  } finally {
    state.running = false;
    toggleActionState(false);
  }
}

async function pollCitationJob(jobId) {
  if (!jobId) {
    throw new Error("任务创建失败。");
  }

  while (state.running && state.currentJobId === jobId) {
    const response = await fetch(`/api/cite-jobs/${encodeURIComponent(jobId)}`, {
      credentials: "same-origin",
    });
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 401) {
        await refreshSession();
      }
      throw new Error(data.error || "无法获取处理进度。");
    }

    renderProgress(data);

    if (data.status === "completed") {
      const result = data.result || null;
      state.result = result;
      renderResult(result);
      state.running = false;
      setStatus("完成", "done");
      setMessage(buildCompletionMessage(result), "success");
      if (result && result.usage) {
        state.session.usage = result.usage;
        window.AddRefSessionClient.applySessionChrome(state.session, els);
      }
      return;
    }

    if (data.status === "failed") {
      state.running = false;
      throw new Error(data.error || data.detail || "处理失败。");
    }

    const progressPercent = Math.max(0, Number.parseInt(data.progress_percent || 0, 10) || 0);
    if (data.status === "queued") {
      setStatus("排队中", "running");
      setMessage(data.detail || data.message || "排队中。", "success");
    } else {
      setStatus(`处理中 ${progressPercent}%`, "running");
      setMessage(data.message || "处理中。", "success");
    }
    await wait(JOB_POLL_INTERVAL_MS);
  }
}

async function restoreLastJob() {
  if (!state.session.authenticated) {
    return;
  }

  try {
    let jobId = loadStoredJobId();
    let job = null;

    if (jobId) {
      try {
        job = await fetchCitationJob(jobId);
      } catch (error) {
        if (!isNotFoundError(error)) {
          throw error;
        }
        clearStoredJobId();
        jobId = "";
      }
    }

    if (!job) {
      try {
        job = await fetchLatestCitationJob();
        jobId = job.job_id || "";
        if (!jobId) {
          return;
        }
        storeJobId(jobId);
      } catch (error) {
        if (isNotFoundError(error)) {
          return;
        }
        throw error;
      }
    }

    state.currentJobId = jobId;
    renderProgress(job);

    if (job.status === "completed") {
      const result = job.result || null;
      state.result = result;
      renderResult(result);
      setStatus("完成", "done");
      setMessage("已恢复上次任务结果。", "success");
      if (result && result.usage) {
        state.session.usage = result.usage;
        window.AddRefSessionClient.applySessionChrome(state.session, els);
      }
      return;
    }

    if (job.status === "failed") {
      setStatus("出错", "error");
      setMessage(job.error || job.detail || "上次任务处理失败。", "error");
      return;
    }

    state.running = true;
    toggleActionState(true);
    if (job.status === "queued") {
      setStatus("排队中", "running");
      setMessage(job.detail || "已恢复排队中的任务。", "success");
    } else {
      setStatus(`处理中 ${Number.parseInt(job.progress_percent || 0, 10) || 0}%`, "running");
      setMessage("已恢复上次任务。", "success");
    }
    await pollCitationJob(jobId);
  } catch (error) {
    clearStoredJobId();
    state.currentJobId = "";
    renderProgress(null);
    if (String(error.message || "").includes("任务不存在")) {
      setMessage("上次任务已失效。", "error");
      return;
    }
    setMessage(error.message || "恢复任务失败。", "error");
  } finally {
    state.running = false;
    toggleActionState(false);
  }
}

async function fetchCitationJob(jobId) {
  const response = await fetch(`/api/cite-jobs/${encodeURIComponent(jobId)}`, {
    credentials: "same-origin",
  });
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401) {
      await refreshSession();
    }
    const error = new Error(data.error || "无法获取处理进度。");
    error.status = response.status;
    throw error;
  }
  return data;
}

async function fetchLatestCitationJob() {
  const response = await fetch("/api/cite-jobs/latest", {
    credentials: "same-origin",
  });
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401) {
      await refreshSession();
    }
    const error = new Error(data.error || "无法恢复最近任务。");
    error.status = response.status;
    throw error;
  }
  return data;
}

function toggleActionState(running) {
  els.runButton.disabled = running;
  els.runButtonBottom.disabled = running;
  const canContinue = !running && hasContinueCandidate();
  els.continueButton.disabled = !canContinue;
  els.continueButtonBottom.disabled = !canContinue;
  const hasResult = Boolean(state.result && state.result.references && state.result.references.length);
  els.copyResult.disabled = running || !hasResult;
  els.exportSelected.disabled = running || !hasResult;
  els.exportAll.disabled = running || !hasResult;
}

function setStatus(text, kind) {
  els.statusBadge.textContent = text;
  els.statusBadge.className = `status-badge ${kind || ""}`.trim();
}

function setMessage(text, kind) {
  if (!text) {
    els.messageStrip.textContent = "";
    els.messageStrip.className = "message-strip";
    return;
  }
  els.messageStrip.textContent = text;
  els.messageStrip.className = `message-strip visible ${kind || ""}`.trim();
}

function renderResult(result) {
  renderAnnotatedText(result);
  renderReferences(result);
  renderTrace(result);
  toggleActionState(state.running);
}

function renderAnnotatedText(result) {
  if (!result) {
    els.annotatedText.innerHTML = "运行后显示结果";
    els.annotatedText.className = "output-box empty";
    return;
  }

  const source = result.annotated_text || "";
  els.annotatedText.innerHTML = highlightMarkers(source);
  els.annotatedText.className = "output-box";
}

function renderReferences(result) {
  if (!result || !result.references || result.references.length === 0) {
    els.referenceList.innerHTML = "暂无参考文献";
    els.referenceList.className = "reference-list empty-state";
    return;
  }

  els.referenceList.className = "reference-list";
  els.referenceList.innerHTML = result.references
    .map((reference) => {
      const article = reference.article || {};
      const marker = reference.marker;
      const authors = Array.isArray(article.authors) ? article.authors.join(", ") : "";
      const impactFactor =
        typeof article.impact_factor === "number" ? `IF ${article.impact_factor.toFixed(3)}` : "";
      const journalLine = [article.journal, article.year, impactFactor].filter(Boolean).join(" · ");
      const doi = article.doi ? `<span>DOI ${escapeHtml(article.doi)}</span>` : "";
      const url = article.pubmed_url
        ? `<a href="${escapeHtml(article.pubmed_url)}" target="_blank" rel="noreferrer">PubMed</a>`
        : "";
      return `
        <article class="reference-card">
          <header>
            <div>
              <h3>[${marker}] ${escapeHtml(article.title || "Untitled")}</h3>
              <p>${escapeHtml(authors || "作者信息缺失")}</p>
            </div>
            <input class="reference-check" type="checkbox" checked data-marker="${marker}">
          </header>
          <div class="reference-meta">
            <span>${escapeHtml(journalLine || "期刊信息缺失")}</span>
            <span>PMID ${escapeHtml(article.pmid || "-")}</span>
            ${doi}
            ${url}
          </div>
        </article>
      `;
    })
    .join("");
}

function renderTrace(result) {
  if (!result) {
    els.traceList.innerHTML = "运行后显示检索轨迹";
    els.traceList.className = "trace-list empty-state";
    return;
  }

  const placementCards = (result.placements || []).map((placement) => {
    const articles = Array.isArray(placement.articles) && placement.articles.length
      ? placement.articles
      : (placement.article ? [placement.article] : []);
    const markerLabel = formatMarkerLabel(placement.markers || placement.marker || []);
    const articleSummary = articles.length
      ? articles
          .map((article) => `文献：${escapeHtml(article.title || "未命中标题")}（PMID ${escapeHtml(article.pmid || "-")}）`)
          .join("<br>")
      : "文献：未命中文献";
    const attempts = renderAttempts(placement.attempts || []);
    return `
      <article class="trace-card">
        <header>
          <div>
            <h3>命中句子 ${escapeHtml(markerLabel)}</h3>
            <p>${escapeHtml(placement.sentence_text || "")}</p>
          </div>
          <span class="attempt-pill">${escapeHtml(placement.final_query || "")}</span>
        </header>
        <p>${articleSummary}</p>
        <details>
          <summary>查看检索迭代</summary>
          <div class="attempt-list">${attempts}</div>
        </details>
      </article>
    `;
  });

  const unresolvedCards = (result.unresolved_targets || []).map((item) => {
    const attempts = renderAttempts(item.attempts || []);
    return `
      <article class="trace-card">
        <header>
          <div>
            <h3>未解析句子</h3>
            <p>${escapeHtml(item.sentence_text || "")}</p>
          </div>
          <span class="attempt-pill">未找到合适文献</span>
        </header>
        <details>
          <summary>查看失败检索</summary>
          <div class="attempt-list">${attempts}</div>
        </details>
      </article>
    `;
  });

  const combined = placementCards.concat(unresolvedCards);
  els.traceList.className = "trace-list";
  els.traceList.innerHTML = combined.join("") || "没有可展示的检索轨迹。";
}

function renderProgress(job) {
  state.progressJob = job;
  if (!job) {
    state.progressExpanded = false;
    state.progressJobId = "";
    els.progressPercent.textContent = "0%";
    els.progressStage.textContent = "等待开始";
    els.progressDetail.textContent = "提交任务后显示实时进度";
    els.progressFill.style.width = "0%";
    els.progressHistory.innerHTML = "提交任务后显示步骤记录";
    els.progressHistory.className = "progress-history empty-state";
    els.progressToggle.classList.add("hidden");
    return;
  }

  const jobId = job.job_id || "";
  if (jobId && state.progressJobId !== jobId) {
    state.progressExpanded = false;
    state.progressJobId = jobId;
  }

  const progressPercent = Math.max(0, Math.min(100, Number.parseInt(job.progress_percent || 0, 10) || 0));
  els.progressPercent.textContent = `${progressPercent}%`;
  els.progressStage.textContent = job.message || "处理中";
  els.progressDetail.textContent = job.detail || "正在处理";
  els.progressFill.style.width = `${progressPercent}%`;

  const history = Array.isArray(job.history) ? job.history : [];
  if (!history.length) {
    els.progressHistory.innerHTML = "暂无步骤记录";
    els.progressHistory.className = "progress-history empty-state";
    els.progressToggle.classList.add("hidden");
    return;
  }

  const orderedHistory = history.slice().reverse();
  const visibleHistory = state.progressExpanded ? orderedHistory : orderedHistory.slice(0, 5);
  els.progressHistory.className = "progress-history";
  els.progressHistory.innerHTML = visibleHistory
    .map((item) => {
      const time = formatEventTime(item.time);
      return `
        <div class="progress-entry">
          <span>${escapeHtml(time)}</span>
          <strong>${escapeHtml(item.message || "")}</strong>
        </div>
      `;
    })
    .join("");

  if (orderedHistory.length > 5) {
    els.progressToggle.classList.remove("hidden");
    els.progressToggle.textContent = state.progressExpanded
      ? "收起进度"
      : `展开全部 ${orderedHistory.length} 条`;
  } else {
    els.progressToggle.classList.add("hidden");
  }
}

function renderAttempts(attempts) {
  if (!attempts.length) {
    return '<div class="attempt-item retry"><p>没有可用的检索记录。</p></div>';
  }
  return attempts
    .map((attempt) => {
      const topResults = Array.isArray(attempt.top_results)
        ? attempt.top_results
            .map((item) => `${escapeHtml(item.pmid || "")} · ${escapeHtml(item.title || "")}`)
            .join("<br>")
        : "";
      return `
        <div class="attempt-item ${attempt.decision === "accept" ? "" : "retry"}">
          <h4>第 ${attempt.attempt} 轮 · ${escapeHtml(attempt.decision || "retry")}</h4>
          <p><strong>Query:</strong> ${escapeHtml(attempt.query || "")}</p>
          <p><strong>结果数:</strong> ${escapeHtml(String(attempt.result_count || 0))}</p>
          <p><strong>原始命中:</strong> ${escapeHtml(String(attempt.raw_result_count || attempt.result_count || 0))}</p>
          <p><strong>过滤剔除:</strong> ${escapeHtml(String(attempt.filtered_out_count || 0))}</p>
          <p><strong>命中文献:</strong> ${escapeHtml(formatChosenPmids(attempt))}</p>
          <p><strong>判断:</strong> ${escapeHtml(attempt.reason || "")}</p>
          <p><strong>Top Results:</strong><br>${topResults || "无"}</p>
        </div>
      `;
    })
    .join("");
}

async function copyAnnotatedText() {
  if (!state.result || !state.result.annotated_text) {
    return;
  }
  await navigator.clipboard.writeText(state.result.annotated_text);
  setMessage("已复制。", "success");
}

async function exportRis(selectedOnly) {
  if (!state.result || !Array.isArray(state.result.references)) {
    return;
  }

  const references = selectedOnly ? getSelectedReferences() : state.result.references;
  if (!references.length) {
    setMessage("请至少勾选一条参考文献。", "error");
    return;
  }

  try {
    const response = await fetch("/api/export-ris", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ references }),
      credentials: "same-origin",
    });
    if (!response.ok) {
      const data = await response.json();
      if (response.status === 401) {
        await refreshSession();
      }
      throw new Error(data.error || "RIS 导出失败。");
    }

    const blob = await response.blob();
    const url = window.URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "references.ris";
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.URL.revokeObjectURL(url);
    setMessage("RIS 已生成。", "success");
  } catch (error) {
    setMessage(error.message || "RIS 导出失败。", "error");
  }
}

function getSelectedReferences() {
  const selectedMarkers = new Set(
    Array.from(document.querySelectorAll(".reference-check:checked")).map((item) =>
      Number.parseInt(item.dataset.marker || "0", 10)
    )
  );
  return (state.result.references || []).filter((reference) => selectedMarkers.has(reference.marker));
}

function highlightMarkers(text) {
  const escaped = escapeHtml(text);
  return escaped.replace(/\[(\d+(?:\s*,\s*\d+)*)\]/g, '<span class="ref-marker">[$1]</span>');
}

function formatMarkerLabel(markers) {
  const normalized = Array.isArray(markers)
    ? markers
    : [markers];
  const values = normalized
    .map((item) => Number.parseInt(String(item || "0"), 10))
    .filter((item) => Number.isFinite(item) && item > 0);
  if (!values.length) {
    return "[?]";
  }
  return `[${values.join(", ")}]`;
}

function formatChosenPmids(attempt) {
  const chosen = Array.isArray(attempt.chosen_pmids) ? attempt.chosen_pmids : [];
  if (chosen.length) {
    return chosen.join(", ");
  }
  return attempt.chosen_pmid || "-";
}

function buildCitationPayload(config, { continueExisting }) {
  const text = continueExisting ? getContinueSourceText() : els.sourceText.value;
  return {
    text,
    openai: {
      base_url: config.baseUrl,
      api_key: config.openaiKey,
      model: config.model,
      api_mode: config.apiMode,
    },
    ncbi: {
      api_key: config.ncbiKey,
      email: config.ncbiEmail,
      disable_defaults: Boolean(config.disableDefaultNcbi),
    },
    max_targets: Number.parseInt(config.maxTargets, 10),
    results_per_query: Number.parseInt(config.resultsPerQuery, 10),
    max_attempts: Number.parseInt(config.maxAttempts, 10),
    recent_years: config.recentYears ? Number.parseInt(config.recentYears, 10) : "",
    impact_factor_min: config.impactFactorMin ? Number.parseFloat(config.impactFactorMin) : "",
    impact_factor_max: config.impactFactorMax ? Number.parseFloat(config.impactFactorMax) : "",
    existing_references: continueExisting ? (state.result?.references || []) : [],
    existing_placements: continueExisting ? (state.result?.placements || []) : [],
  };
}

function hasContinueCandidate() {
  if (!state.result) {
    return false;
  }
  const sentenceCount = Number.parseInt(state.result.sentence_count || 0, 10) || 0;
  const placementCount = Array.isArray(state.result.placements) ? state.result.placements.length : 0;
  return sentenceCount > placementCount;
}

function ensureContinueAvailable() {
  if (!state.result) {
    throw new Error("请先完成一次处理。");
  }
  if (!hasContinueCandidate()) {
    throw new Error("当前正文里没有可继续添加文献的句子。");
  }
  const currentText = (els.sourceText.value || "").trim();
  const sourceText = getContinueSourceText().trim();
  if (currentText && sourceText && currentText !== sourceText) {
    throw new Error("正文已修改。继续添加只能基于上一次处理的同一份正文，请重新开始处理。");
  }
}

function getContinueSourceText() {
  return state.result?.source_text || els.sourceText.value || "";
}

function buildCompletionMessage(result) {
  const totalPlacements = (result?.placements || []).length;
  const totalReferences = (result?.references || []).length;
  const newPlacements = Number.parseInt(result?.new_placement_count || 0, 10) || 0;
  const newReferences = Number.parseInt(result?.new_reference_count || 0, 10) || 0;
  if (result?.continued_from_existing) {
    if (newPlacements <= 0) {
      return `已完成。本轮未新增文献，当前共 ${totalPlacements} 处、${totalReferences} 条。`;
    }
    return `已完成。本轮新增 ${newPlacements} 处、${newReferences} 条；当前共 ${totalPlacements} 处、${totalReferences} 条。`;
  }
  return `已完成。插入 ${totalPlacements} 处，生成 ${totalReferences} 条。`;
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function validateRunBudget(config) {
  const maxTargets = Number.parseInt(config.maxTargets, 10) || 4;
  const resultsPerQuery = Number.parseInt(config.resultsPerQuery, 10) || 6;
  const maxAttempts = Number.parseInt(config.maxAttempts, 10) || 10;
  if (maxTargets * resultsPerQuery * maxAttempts > 8000) {
    return "插入条数、每轮结果数、最大轮次的乘积不能超过 8000。";
  }
  if (config.recentYears) {
    const parsedRecentYears = Number.parseInt(config.recentYears, 10);
    if (!Number.isFinite(parsedRecentYears) || parsedRecentYears < 1 || parsedRecentYears > 50) {
      return "近 n 年需填写 1 到 50 之间的整数。";
    }
  }
  if (config.impactFactorMin) {
    const parsedMin = Number.parseFloat(config.impactFactorMin);
    if (!Number.isFinite(parsedMin) || parsedMin < 0 || parsedMin > 500) {
      return "IF 最小值需填写 0 到 500 之间的数字。";
    }
  }
  if (config.impactFactorMax) {
    const parsedMax = Number.parseFloat(config.impactFactorMax);
    if (!Number.isFinite(parsedMax) || parsedMax < 0 || parsedMax > 500) {
      return "IF 最大值需填写 0 到 500 之间的数字。";
    }
  }
  if (config.impactFactorMin && config.impactFactorMax) {
    const parsedMin = Number.parseFloat(config.impactFactorMin);
    const parsedMax = Number.parseFloat(config.impactFactorMax);
    if (parsedMin > parsedMax) {
      return "IF 最小值不能大于最大值。";
    }
  }
  return "";
}

function storeJobId(jobId) {
  if (!jobId) {
    clearStoredJobId();
    return;
  }
  const key = getJobStorageKey();
  if (!key) {
    return;
  }
  localStorage.setItem(key, jobId);
}

function loadStoredJobId() {
  const key = getJobStorageKey();
  return key ? localStorage.getItem(key) || "" : "";
}

function clearStoredJobId() {
  const key = getJobStorageKey();
  if (!key) {
    return;
  }
  localStorage.removeItem(key);
}

function getJobStorageKey() {
  const email = state.session && state.session.user ? state.session.user.email : "";
  return email ? `${LAST_JOB_KEY}:${email.toLowerCase()}` : "";
}

function isNotFoundError(error) {
  return Boolean(error) && Number(error.status) === 404;
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

function wait(ms) {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

function toggleProgressHistory() {
  if (!state.progressJob) {
    return;
  }
  state.progressExpanded = !state.progressExpanded;
  renderProgress(state.progressJob);
}
