(function () {
  const PUBLIC_KEY = "addref-config-public-v3";
  const SECRET_KEY = "addref-config-secret-v3";

  function defaultConfig() {
    return {
      baseUrl: "",
      model: "",
      apiMode: "auto",
      ncbiEmail: "",
      disableDefaultNcbi: true,
      maxTargets: "4",
      resultsPerQuery: "6",
      maxAttempts: "10",
      openaiKey: "",
      ncbiKey: "",
    };
  }

  function readJson(storage, key) {
    try {
      const value = storage.getItem(key);
      return value ? JSON.parse(value) : {};
    } catch (error) {
      return {};
    }
  }

  function loadConfig() {
    return {
      ...defaultConfig(),
      ...readJson(localStorage, PUBLIC_KEY),
      ...readJson(sessionStorage, SECRET_KEY),
    };
  }

  function saveConfig(config) {
    const next = {
      ...defaultConfig(),
      ...(config || {}),
    };
    const publicConfig = {
      baseUrl: next.baseUrl || "",
      model: next.model || "",
      apiMode: next.apiMode || "auto",
      ncbiEmail: next.ncbiEmail || "",
      disableDefaultNcbi: Boolean(next.disableDefaultNcbi),
      maxTargets: String(next.maxTargets || "4"),
      resultsPerQuery: String(next.resultsPerQuery || "6"),
      maxAttempts: String(next.maxAttempts || "10"),
    };
    const secretConfig = {
      openaiKey: next.openaiKey || "",
      ncbiKey: next.ncbiKey || "",
    };

    localStorage.setItem(PUBLIC_KEY, JSON.stringify(publicConfig));
    if (secretConfig.openaiKey || secretConfig.ncbiKey) {
      sessionStorage.setItem(SECRET_KEY, JSON.stringify(secretConfig));
    } else {
      sessionStorage.removeItem(SECRET_KEY);
    }
  }

  function clearConfig() {
    localStorage.removeItem(PUBLIC_KEY);
    sessionStorage.removeItem(SECRET_KEY);
  }

  window.AddRefConfigStore = {
    defaultConfig,
    loadConfig,
    saveConfig,
    clearConfig,
  };
})();
