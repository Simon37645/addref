const els = {
  registerEmail: document.getElementById("register-email"),
  registerCode: document.getElementById("register-code"),
  registerPassword: document.getElementById("register-password"),
  sendRegisterCode: document.getElementById("send-register-code"),
  registerButton: document.getElementById("register-button"),
  loginEmail: document.getElementById("login-email"),
  loginPassword: document.getElementById("login-password"),
  loginButton: document.getElementById("login-button"),
  resetEmail: document.getElementById("reset-email"),
  resetCode: document.getElementById("reset-code"),
  resetPassword: document.getElementById("reset-password"),
  sendResetCode: document.getElementById("send-reset-code"),
  resetButton: document.getElementById("reset-button"),
  authMessage: document.getElementById("auth-message"),
};

const state = {
  registerCode: { sending: false, cooldownSeconds: 0, cooldownTimer: null },
  resetCode: { sending: false, cooldownSeconds: 0, cooldownTimer: null },
};

initialize();

async function initialize() {
  els.sendRegisterCode.addEventListener("click", handleSendRegisterCode);
  els.registerButton.addEventListener("click", handleRegister);
  els.loginButton.addEventListener("click", handleLogin);
  els.sendResetCode.addEventListener("click", handleSendResetCode);
  els.resetButton.addEventListener("click", handleResetPassword);
  try {
    const session = await fetch("/api/session", { credentials: "same-origin" }).then((response) =>
      response.json()
    );
    if (session.authenticated) {
      window.location.href = "/";
    }
  } catch (error) {
    return;
  }
}

async function handleSendRegisterCode() {
  await sendCode({
    stateKey: "registerCode",
    email: els.registerEmail.value.trim(),
    endpoint: "/api/send-register-code",
    button: els.sendRegisterCode,
    emptyMessage: "请先输入注册邮箱。",
  });
}

async function handleRegister() {
  await submitAuth("/api/register", {
    email: els.registerEmail.value.trim(),
    verification_code: els.registerCode.value.trim(),
    password: els.registerPassword.value,
  });
}

async function handleLogin() {
  await submitAuth("/api/login", {
    email: els.loginEmail.value.trim(),
    password: els.loginPassword.value,
  });
}

async function handleSendResetCode() {
  await sendCode({
    stateKey: "resetCode",
    email: els.resetEmail.value.trim(),
    endpoint: "/api/send-reset-code",
    button: els.sendResetCode,
    emptyMessage: "请先输入找回密码邮箱。",
  });
}

async function handleResetPassword() {
  try {
    setMessage("提交中，请稍候。", "success");
    const response = await fetch("/api/reset-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: els.resetEmail.value.trim(),
        verification_code: els.resetCode.value.trim(),
        new_password: els.resetPassword.value,
      }),
      credentials: "same-origin",
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "重置密码失败。");
    }
    els.loginEmail.value = els.resetEmail.value.trim();
    els.loginPassword.value = "";
    setMessage(data.message || "密码已重置，请使用新密码登录。", "success");
  } catch (error) {
    setMessage(error.message || "重置密码失败。", "error");
  }
}

async function submitAuth(url, payload, options = {}) {
  const { redirectOnSuccess = true } = options;
  try {
    setMessage("提交中，请稍候。", "success");
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      credentials: "same-origin",
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "提交失败。");
    }
    if (redirectOnSuccess) {
      setMessage("成功，正在跳转。", "success");
      window.location.href = "/";
      return;
    }
    setMessage(data.message || "提交成功。", "success");
  } catch (error) {
    setMessage(error.message || "提交失败。", "error");
  }
}

function setMessage(text, kind) {
  els.authMessage.textContent = text || "";
  els.authMessage.className = text
    ? `message-strip visible ${kind || ""}`.trim()
    : "message-strip";
}

function startCooldown(seconds) {
  startChannelCooldown("registerCode", seconds, els.sendRegisterCode);
}

async function sendCode({ stateKey, email, endpoint, button, emptyMessage }) {
  const channel = state[stateKey];
  if (channel.sending || channel.cooldownSeconds > 0) {
    return;
  }
  if (!email) {
    setMessage(emptyMessage, "error");
    return;
  }

  channel.sending = true;
  syncCodeButton(button, channel);
  try {
    setMessage("验证码发送中，请稍候。", "success");
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
      credentials: "same-origin",
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "验证码发送失败。");
    }
    setMessage(data.message || "验证码已发送，请检查邮箱，注意垃圾箱。", "success");
    startChannelCooldown(stateKey, Number.parseInt(data.cooldown_seconds, 10) || 60, button);
  } catch (error) {
    setMessage(error.message || "验证码发送失败。", "error");
  } finally {
    channel.sending = false;
    syncCodeButton(button, channel);
  }
}

function startChannelCooldown(stateKey, seconds, button) {
  const channel = state[stateKey];
  clearChannelCooldown(stateKey);
  channel.cooldownSeconds = Math.max(0, seconds);
  syncCodeButton(button, channel);
  channel.cooldownTimer = window.setInterval(() => {
    channel.cooldownSeconds = Math.max(0, channel.cooldownSeconds - 1);
    syncCodeButton(button, channel);
    if (channel.cooldownSeconds === 0) {
      clearChannelCooldown(stateKey);
      syncCodeButton(button, channel);
    }
  }, 1000);
}

function clearChannelCooldown(stateKey) {
  const channel = state[stateKey];
  if (channel.cooldownTimer) {
    window.clearInterval(channel.cooldownTimer);
    channel.cooldownTimer = null;
  }
}

function syncRegisterCodeButton() {
  syncCodeButton(els.sendRegisterCode, state.registerCode);
}

function syncCodeButton(button, channel) {
  if (channel.sending) {
    button.disabled = true;
    button.textContent = "发送中";
    return;
  }
  if (channel.cooldownSeconds > 0) {
    button.disabled = true;
    button.textContent = `${channel.cooldownSeconds}s`;
    return;
  }
  button.disabled = false;
  button.textContent = "发送验证码";
}
