import { useCallback, useEffect, useRef, useState } from "react";
import {
  getLlmConfig,
  getLlmProviders,
  setLlmConfig,
  testLlmProvider,
  setLlmApiKey,
  setLlmModel,
  getLlmProviderModels,
  type LLMConfig,
  type LLMProviderInfo,
  type LLMProviderName,
} from "../../api/client";
import { ProviderCard } from "./ProviderCard";
import { ProviderSetupModal } from "./ProviderSetupModal";

/**
 * Single-provider model — one AI provider serves all 3 features
 * (Auto-Prompt / Vision / Planner). User picks one card, configures key/model,
 * runs ONE connection test, then Apply commits the change to all 3 features.
 */

const REFRESH_INTERVAL_MS = 30_000;
const SHOWN_PROVIDERS: LLMProviderName[] = ["gemini", "claude", "openai", "nine_router"];
const FIRST_RUN_DEFAULT: LLMProviderName = "gemini";

type TestState = "untested" | "testing" | "ok" | "fail";
interface ConnectionTestResult {
  state: TestState;
  error?: string;
  latencyMs?: number;
}

const INITIAL_TEST: ConnectionTestResult = { state: "untested" };

function deriveCurrent(config: LLMConfig | null): LLMProviderName | null {
  if (!config) return null;
  const a = config.auto_prompt;
  if (a === null) return null;
  if (a === config.vision && config.vision === config.planner) {
    return a;
  }
  return null;
}

export function AiProvidersSection() {
  const [providers, setProviders] = useState<LLMProviderInfo[] | null>(null);
  const [config, setConfig] = useState<LLMConfig | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [pending, setPending] = useState<LLMProviderName | null>(null);
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [loadingModels, setLoadingModels] = useState(false);
  const [selectedModel, setSelectedModel] = useState("");
  const [customModelInput, setCustomModelInput] = useState("");
  const [showCustomInput, setShowCustomInput] = useState(false);

  const [test, setTest] = useState<ConnectionTestResult>(INITIAL_TEST);
  const [applying, setApplying] = useState(false);
  const [helpFor, setHelpFor] = useState<LLMProviderName | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [p, c] = await Promise.all([getLlmProviders(), getLlmConfig()]);
      if (!aliveRef.current) return;
      setProviders(p);
      setConfig(c);
      setLoadError(null);
    } catch (err) {
      if (!aliveRef.current) return;
      setLoadError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  // Initial load + 30s polling
  useEffect(() => {
    void refresh();
    const interval = setInterval(() => {
      if (document.visibilityState === "visible") void refresh();
    }, REFRESH_INTERVAL_MS);
    const onVis = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      clearInterval(interval);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [refresh]);

  const current = deriveCurrent(config);
  useEffect(() => {
    if (pending !== null || config === null) return;
    if (current !== null && SHOWN_PROVIDERS.includes(current)) {
      setPending(current);
    } else {
      setPending(FIRST_RUN_DEFAULT);
    }
  }, [current, pending, config]);

  const pendingProvider = pending && providers ? providers.find((p) => p.name === pending) : null;

  // Load models for pending provider when configured status changes
  useEffect(() => {
    if (!pending || !pendingProvider?.configured) {
      setModels([]);
      setSelectedModel("");
      setShowCustomInput(false);
      return;
    }

    setLoadingModels(true);
    getLlmProviderModels(pending)
      .then((list) => {
        if (!aliveRef.current) return;
        setModels(list);

        const activeModel = pendingProvider.selectedModel;
        if (activeModel) {
          setSelectedModel(activeModel);
          if (!list.includes(activeModel)) {
            setShowCustomInput(true);
            setCustomModelInput(activeModel);
          } else {
            setShowCustomInput(false);
          }
        } else if (list.length > 0) {
          setSelectedModel(list[0]);
          void handleSaveModel(list[0]);
        }
      })
      .catch((err) => {
        console.error("Failed to load models:", err);
      })
      .finally(() => {
        if (aliveRef.current) setLoadingModels(false);
      });
  }, [pending, pendingProvider?.configured, pendingProvider?.selectedModel]);

  function showToast(msg: string) {
    setToast(msg);
    setTimeout(() => setToast(null), 4000);
  }

  function handleSelect(name: LLMProviderName) {
    if (name === pending) return;
    setPending(name);
    setApiKeyInput("");
    setCustomModelInput("");
    setShowCustomInput(false);
    setTest(INITIAL_TEST);
  }

  async function handleSaveKey() {
    if (!pending || !apiKeyInput) return;
    try {
      await setLlmApiKey(pending, apiKeyInput);
      showToast(`API Key saved for ${labelOf(pending)}.`);
      setApiKeyInput("");
      await refresh();
    } catch (err) {
      showToast(`Failed to save key: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  async function handleClearKey() {
    if (!pending) return;
    try {
      await setLlmApiKey(pending, null);
      await setLlmModel(pending, null);
      showToast(`API Key and model cleared for ${labelOf(pending)}.`);
      setApiKeyInput("");
      setSelectedModel("");
      setCustomModelInput("");
      setShowCustomInput(false);
      setModels([]);
      setTest(INITIAL_TEST);
      await refresh();
    } catch (err) {
      showToast(`Failed to clear key: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  async function handleSaveModel(modelName: string) {
    if (!pending) return;
    try {
      setSelectedModel(modelName);
      await setLlmModel(pending, modelName);
      showToast(`Model configured to ${modelName} for ${labelOf(pending)}.`);
      await refresh();
    } catch (err) {
      showToast(`Failed to save model: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  async function runTest() {
    if (!pending) return;
    setTest({ state: "testing" });
    const result = await testLlmProvider(pending);
    setTest(
      result.ok
        ? { state: "ok", latencyMs: result.latencyMs }
        : { state: "fail", error: result.error || "test failed" },
    );
  }

  async function handleApply() {
    if (!pending || applying) return;
    setApplying(true);
    try {
      await setLlmConfig({
        auto_prompt: pending,
        vision: pending,
        planner: pending,
      });
      showToast(`AI provider switched to ${labelOf(pending)}.`);
      await refresh();
      window.dispatchEvent(new CustomEvent("flowboard:llm-config-changed"));
    } catch (err) {
      showToast(
        `Couldn't apply: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      if (aliveRef.current) setApplying(false);
    }
  }

  if (!providers && !config && !loadError) {
    return (
      <div className="ai-providers-section">
        <div className="ai-providers-section__skeleton">
          <div className="ai-providers-section__skeleton-row" />
          <div className="ai-providers-section__skeleton-row" />
          <div className="ai-providers-section__skeleton-row ai-providers-section__skeleton-row--tall" />
        </div>
      </div>
    );
  }

  if (loadError && (!providers || !config)) {
    return (
      <div className="ai-providers-section">
        <div className="ai-providers-section__error" role="alert">
          ⚠ Couldn't load AI provider state.
          <button
            type="button"
            className="ai-providers-section__retry"
            onClick={() => void refresh()}
          >
            Retry
          </button>
          <div className="ai-providers-section__error-detail">{loadError}</div>
        </div>
      </div>
    );
  }

  const byName: Record<LLMProviderName, LLMProviderInfo | undefined> = {
    claude: providers!.find((p) => p.name === "claude"),
    gemini: providers!.find((p) => p.name === "gemini"),
    openai: providers!.find((p) => p.name === "openai"),
    nine_router: providers!.find((p) => p.name === "nine_router"),
  };

  const ready = !!pendingProvider && pendingProvider.available && pendingProvider.configured;
  const testPassed = test.state === "ok";
  const testRunning = test.state === "testing";
  const selectionUnchanged = pending !== null && pending === current;
  const canApply =
    ready
    && testPassed
    && !applying
    && !testRunning
    && !selectionUnchanged;

  return (
    <div className="ai-providers-section">
      <div className="ai-providers-section__intro">
        Pick which AI powers Flowboard. One provider serves all three
        features — switching is one decision, not three.
      </div>

      {current === null && config !== null && !config.configured
        && (config.auto_prompt || config.vision || config.planner) && (
        <div className="ai-providers-section__mixed-notice" role="alert">
          ⓘ Your providers don't match across features
          ({config.auto_prompt ?? "—"} / {config.vision ?? "—"} / {config.planner ?? "—"}).
          Pick one below and Apply to consolidate.
        </div>
      )}

      <div className="provider-group">
        <div className="provider-group__title">API Providers</div>
        <div className="provider-group__cards">
          {SHOWN_PROVIDERS.map((name) => {
            const p = byName[name];
            if (!p) return null;
            return (
              <ProviderCard
                key={name}
                provider={p}
                selected={pending === name}
                current={current === name}
                onSelect={handleSelect}
              />
            );
          })}
        </div>
      </div>

      {pending && pendingProvider && (
        <div className="selection-panel">
          <div className="selection-panel__heading">
            Configure {labelOf(pending)} API Setup
            <button
              type="button"
              className="selection-panel__help-link"
              onClick={() => setHelpFor(pending)}
              style={{ float: "right", background: "none", border: "none", color: "var(--accent)", cursor: "pointer", fontSize: "11px" }}
            >
              Setup help →
            </button>
          </div>

          <div className="api-key-setup-block">
            <span className="api-key-setup-label" style={{ fontSize: "11px", color: "var(--muted)", display: "block", marginBottom: "4px" }}>
              API Key
            </span>
            <div className="api-key-input-row">
              <input
                type="password"
                className="api-key-input"
                placeholder={pendingProvider.configured ? "••••••••••••••••" : `Enter ${labelOf(pending)} API Key`}
                value={apiKeyInput}
                onChange={(e) => setApiKeyInput(e.target.value)}
                disabled={pendingProvider.configured}
              />
              {!pendingProvider.configured ? (
                <button
                  type="button"
                  className="api-key-save-btn"
                  onClick={handleSaveKey}
                  disabled={!apiKeyInput}
                >
                  Save Key
                </button>
              ) : (
                <button
                  type="button"
                  className="api-key-clear-btn"
                  onClick={handleClearKey}
                >
                  Clear Key
                </button>
              )}
            </div>
          </div>

          {pendingProvider.configured && (
            <div className="model-setup-block" style={{ marginTop: "12px", borderTop: "1px solid var(--border)", paddingTop: "12px" }}>
              <span className="model-setup-label" style={{ fontSize: "11px", color: "var(--muted)", display: "block", marginBottom: "4px" }}>
                Active Model
              </span>

              {loadingModels ? (
                <div style={{ fontSize: "11px", color: "var(--muted)" }}>Loading models...</div>
              ) : (
                <div className="model-select-container" style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
                  <select
                    className="api-key-input"
                    value={showCustomInput ? "custom" : selectedModel}
                    onChange={(e) => {
                      const val = e.target.value;
                      if (val === "custom") {
                        setShowCustomInput(true);
                      } else {
                        setShowCustomInput(false);
                        void handleSaveModel(val);
                      }
                    }}
                  >
                    {models.map((m) => (
                      <option key={m} value={m}>{m}</option>
                    ))}
                    <option value="custom">Custom model name...</option>
                  </select>

                  {showCustomInput && (
                    <div className="api-key-input-row">
                      <input
                        type="text"
                        className="api-key-input"
                        placeholder="Enter custom model ID"
                        value={customModelInput}
                        onChange={(e) => setCustomModelInput(e.target.value)}
                      />
                      <button
                        type="button"
                        className="api-key-save-btn"
                        onClick={() => void handleSaveModel(customModelInput)}
                        disabled={!customModelInput}
                      >
                        Set Model
                      </button>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {pendingProvider.configured && (
            <div className="test-apply-block" style={{ marginTop: "12px", borderTop: "1px solid var(--border)", paddingTop: "12px" }}>
              <div className="selection-panel__heading" style={{ marginBottom: "8px" }}>
                Test connection, then Apply
              </div>
              <ConnectionTestRow
                providerLabel={labelOf(pending)}
                result={test}
                onTest={runTest}
              />
              <div className="selection-panel__actions" style={{ marginTop: "12px" }}>
                <button
                  type="button"
                  className="selection-panel__apply-btn"
                  onClick={handleApply}
                  disabled={!canApply}
                  title={
                    selectionUnchanged
                      ? `${labelOf(pending)} is already active.`
                      : !testPassed
                        ? "Run the connection test successfully to enable Apply."
                        : `Apply ${labelOf(pending)} to all features.`
                  }
                >
                  {applying
                    ? "Applying…"
                    : selectionUnchanged
                      ? "Already active"
                      : "Apply changes"}
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {toast && (
        <div className="ai-providers-section__toast" role="alert">
          {toast}
        </div>
      )}

      <ProviderSetupModal
        provider={helpFor ?? "claude"}
        open={helpFor !== null}
        onClose={() => setHelpFor(null)}
      />
    </div>
  );
}

interface ConnectionTestRowProps {
  providerLabel: string;
  result: ConnectionTestResult;
  onTest(): void;
}

function ConnectionTestRow({ providerLabel, result, onTest }: ConnectionTestRowProps) {
  const icon =
    result.state === "ok"
      ? "✓"
      : result.state === "fail"
        ? "✗"
        : result.state === "testing"
          ? "⏳"
          : "○";
  const subtitle =
    result.state === "ok" && result.latencyMs != null
      ? `Connected · ${result.latencyMs}ms · powers Auto-Prompt, Vision, Planner`
      : result.state === "fail" && result.error
        ? result.error
        : result.state === "testing"
          ? "Pinging API..."
          : "Sends one tiny prompt to verify the API key.";
  return (
    <div className={`feature-test-row feature-test-row--${result.state}`}>
      <span
        className={`feature-test-row__icon feature-test-row__icon--${result.state}`}
        aria-hidden="true"
      >
        {icon}
      </span>
      <div className="feature-test-row__body">
        <span className="feature-test-row__name">
          {providerLabel} connection
        </span>
        <span
          className={
            result.state === "fail"
              ? "feature-test-row__error"
              : result.state === "ok"
                ? "feature-test-row__latency"
                : "feature-test-row__hint"
          }
        >
          {subtitle}
        </span>
      </div>
      <button
        type="button"
        className="feature-test-row__btn"
        onClick={onTest}
        disabled={result.state === "testing"}
      >
        {result.state === "testing"
          ? "Testing…"
          : result.state === "ok"
            ? "Re-test"
            : result.state === "fail"
              ? "Retry"
              : "Test"}
      </button>
    </div>
  );
}

function labelOf(name: LLMProviderName): string {
  switch (name) {
    case "claude":
      return "Claude";
    case "gemini":
      return "Gemini";
    case "openai":
      return "OpenAI";
    case "nine_router":
      return "9Router Proxy";
  }
}
