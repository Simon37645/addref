(function () {
  async function fetchSession() {
    const response = await fetch("/api/session", { credentials: "same-origin" });
    if (!response.ok) {
      return { authenticated: false };
    }
    return response.json();
  }

  async function logout() {
    await fetch("/api/logout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
      credentials: "same-origin",
    });
  }

  function applySessionChrome(session, elements) {
    const authenticated = Boolean(session && session.authenticated);
    if (elements.sessionEmail) {
      renderUserIdentity(elements.sessionEmail, authenticated ? session.user : null);
      elements.sessionEmail.classList.toggle("hidden", !authenticated);
    }
    if (elements.authLink) {
      elements.authLink.classList.toggle("hidden", authenticated);
    }
    if (elements.logoutButton) {
      elements.logoutButton.classList.toggle("hidden", !authenticated);
    }
    if (elements.accountEmail) {
      renderUserIdentity(elements.accountEmail, authenticated ? session.user : null, "未登录");
    }
    if (elements.accountUsage) {
      if (!authenticated) {
        elements.accountUsage.textContent = "请先登录";
      } else if (session.user.is_owner) {
        elements.accountUsage.textContent = "不限额";
      } else {
        const remaining = session.usage.default_remaining;
        const limit = session.usage.default_limit;
        const maxTextLength = session.usage.default_max_text_length;
        elements.accountUsage.textContent = `默认服务剩余 ${remaining} / ${limit} 次，单次不超过 ${maxTextLength} 字`;
      }
    }
  }

  function renderUserIdentity(element, user, fallbackText = "") {
    if (!element) {
      return;
    }
    element.textContent = "";
    if (!user) {
      element.textContent = fallbackText;
      return;
    }

    element.append(document.createTextNode(user.email || ""));
  }

  window.AddRefSessionClient = {
    fetchSession,
    logout,
    applySessionChrome,
  };
})();
