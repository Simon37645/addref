const els = {
  baseUrl: document.getElementById("base-url"),
  model: document.getElementById("model"),
  openaiKey: document.getElementById("openai-key"),
  apiMode: document.getElementById("api-mode"),
  ncbiKey: document.getElementById("ncbi-key"),
  ncbiEmail: document.getElementById("ncbi-email"),
  disableDefaultNcbi: document.getElementById("disable-default-ncbi"),
  maxTargets: document.getElementById("max-targets"),
  resultsPerQuery: document.getElementById("results-per-query"),
  maxAttempts: document.getElementById("max-attempts"),
  recentYears: document.getElementById("recent-years"),
  impactFactorMin: document.getElementById("impact-factor-min"),
  impactFactorMax: document.getElementById("impact-factor-max"),
  saveSettings: document.getElementById("save-settings"),
  testModel: document.getElementById("test-model"),
  clearSettings: document.getElementById("clear-settings"),
  settingsMessage: document.getElementById("settings-message"),
  testStatus: document.getElementById("test-status"),
  currentPassword: document.getElementById("current-password"),
  newPassword: document.getElementById("new-password"),
  changePassword: document.getElementById("change-password"),
  sessionEmail: document.getElementById("session-email"),
  authLink: document.getElementById("auth-link"),
  logoutButton: document.getElementById("logout-button"),
};

const state = {
  testing: false,
  session: { authenticated: false },
};

initialize();

async function initialize() {
  hydrateForm();
  wireEvents();
  await refreshSession();
}

function wireEvents() {
  els.saveSettings.addEventListener("click", saveSettings);
  els.testModel.addEventListener("click", testModelConnection);
  els.clearSettings.addEventListener("click", clearSettings);
  els.changePassword.addEventListener("click", handleChangePassword);
  els.logoutButton.addEventListener("click", handleLogout);
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
  window.AddRefSessionClient.applySessionChrome(state.session, els);
  setMessage("已退出登录。", "success");
}

function hydrateForm() {
  const config = window.AddRefConfigStore.loadConfig();
  els.baseUrl.value = config.baseUrl || "";
  els.model.value = config.model || "";
  els.openaiKey.value = config.openaiKey || "";
  els.apiMode.value = config.apiMode || "auto";
  els.ncbiKey.value = config.ncbiKey || "";
  els.ncbiEmail.value = config.ncbiEmail || "";
  els.disableDefaultNcbi.checked = Boolean(config.disableDefaultNcbi);
  els.maxTargets.value = config.maxTargets || "4";
  els.resultsPerQuery.value = config.resultsPerQuery || "6";
  els.maxAttempts.value = config.maxAttempts || "10";
  els.recentYears.value = config.recentYears || "";
  els.impactFactorMin.value = config.impactFactorMin || "";
  els.impactFactorMax.value = config.impactFactorMax || "";
}

function collectFormConfig() {
  return {
    baseUrl: els.baseUrl.value.trim(),
    model: els.model.value.trim(),
    openaiKey: els.openaiKey.value.trim(),
    apiMode: els.apiMode.value,
    ncbiKey: els.ncbiKey.value.trim(),
    ncbiEmail: els.ncbiEmail.value.trim(),
    disableDefaultNcbi: els.disableDefaultNcbi.checked,
    maxTargets: els.maxTargets.value.trim() || "4",
    resultsPerQuery: els.resultsPerQuery.value.trim() || "6",
    maxAttempts: els.maxAttempts.value.trim() || "10",
    recentYears: els.recentYears.value.trim(),
    impactFactorMin: els.impactFactorMin.value.trim(),
    impactFactorMax: els.impactFactorMax.value.trim(),
  };
}

function saveSettings() {
  const config = collectFormConfig();
  const validationError = validateRunBudget(config);
  if (validationError) {
    setMessage(validationError, "error");
    return;
  }
  window.AddRefConfigStore.saveConfig(config);
  setMessage("设置已保存。", "success");
}

function clearSettings() {
  window.AddRefConfigStore.clearConfig();
  hydrateForm();
  setMessage("设置已清空。", "success");
  setTestStatus("待检测", "");
}

async function testModelConnection() {
  if (state.testing) {
    return;
  }

  if (!state.session.authenticated) {
    setMessage("请先登录。", "error");
    window.location.href = "/auth";
    return;
  }

  state.testing = true;
  setTestStatus("检测中", "running");
  setMessage("检测中，请稍候。", "success");
  toggleButtons(true);

  try {
    const config = collectFormConfig();
    const response = await fetch("/api/test-openai", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        openai: {
          base_url: config.baseUrl,
          api_key: config.openaiKey,
          model: config.model,
          api_mode: config.apiMode,
        },
        ncbi: {
          disable_defaults: Boolean(config.disableDefaultNcbi),
        },
      }),
      credentials: "same-origin",
    });
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 401) {
        await refreshSession();
      }
      throw new Error(data.error || "模型连接检测失败。");
    }

    if (data.usage) {
      state.session.usage = data.usage;
      window.AddRefSessionClient.applySessionChrome(state.session, els);
    }
    setTestStatus("模型可用", "done");
    setMessage(`模型连接成功，模式 ${data.mode_used}，返回：${data.response_preview}`, "success");
  } catch (error) {
    setTestStatus("模型异常", "error");
    setMessage(error.message || "模型连接检测失败。", "error");
  } finally {
    state.testing = false;
    toggleButtons(false);
  }
}

async function handleChangePassword() {
  if (!state.session.authenticated) {
    setMessage("请先登录。", "error");
    window.location.href = "/auth";
    return;
  }

  try {
    const response = await fetch("/api/change-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        current_password: els.currentPassword.value,
        new_password: els.newPassword.value,
      }),
      credentials: "same-origin",
    });
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 401) {
        await refreshSession();
      }
      throw new Error(data.error || "修改密码失败。");
    }
    els.currentPassword.value = "";
    els.newPassword.value = "";
    setMessage(data.message || "密码已更新。", "success");
  } catch (error) {
    setMessage(error.message || "修改密码失败。", "error");
  }
}

function toggleButtons(disabled) {
  els.saveSettings.disabled = disabled;
  els.testModel.disabled = disabled;
  els.clearSettings.disabled = disabled;
}

function setMessage(text, kind) {
  els.settingsMessage.textContent = text || "";
  els.settingsMessage.className = text
    ? `message-strip visible ${kind || ""}`.trim()
    : "message-strip";
}

function setTestStatus(text, kind) {
  els.testStatus.textContent = text;
  els.testStatus.className = `status-badge ${kind || ""}`.trim();
}

function validateRunBudget(config) {
  const maxTargets = Number.parseInt(config.maxTargets, 10) || 4;
  const resultsPerQuery = Number.parseInt(config.resultsPerQuery, 10) || 6;
  const maxAttempts = Number.parseInt(config.maxAttempts, 10) || 10;
  if (maxTargets * resultsPerQuery * maxAttempts > 8000) {
    return "插入条数、每轮结果数、最大轮次的乘积不能超过 8000。";
  }
  const recentYears = config.recentYears.trim();
  if (recentYears) {
    const parsedRecentYears = Number.parseInt(recentYears, 10);
    if (!Number.isFinite(parsedRecentYears) || parsedRecentYears < 1 || parsedRecentYears > 50) {
      return "近 n 年需填写 1 到 50 之间的整数。";
    }
  }
  const impactFactorMin = config.impactFactorMin.trim();
  const impactFactorMax = config.impactFactorMax.trim();
  if (impactFactorMin) {
    const parsedMin = Number.parseFloat(impactFactorMin);
    if (!Number.isFinite(parsedMin) || parsedMin < 0 || parsedMin > 500) {
      return "IF 最小值需填写 0 到 500 之间的数字。";
    }
  }
  if (impactFactorMax) {
    const parsedMax = Number.parseFloat(impactFactorMax);
    if (!Number.isFinite(parsedMax) || parsedMax < 0 || parsedMax > 500) {
      return "IF 最大值需填写 0 到 500 之间的数字。";
    }
  }
  if (impactFactorMin && impactFactorMax) {
    const parsedMin = Number.parseFloat(impactFactorMin);
    const parsedMax = Number.parseFloat(impactFactorMax);
    if (parsedMin > parsedMax) {
      return "IF 最小值不能大于最大值。";
    }
  }
  return "";
}
