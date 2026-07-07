import { useEffect } from "react";
import type { LLMProviderName } from "../../api/client";

/**
 * Inline setup guide opened from the "Setup help" button on each
 * provider row. Guides the user on how to obtain API keys.
 */

interface ProviderSetupModalProps {
  provider: LLMProviderName;
  open: boolean;
  onClose(): void;
}

export function ProviderSetupModal({ provider, open, onClose }: ProviderSetupModalProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="setup-modal-backdrop"
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="setup-modal" role="dialog" aria-modal="true">
        <div className="setup-modal__header">
          <span className="setup-modal__title">{titleFor(provider)}</span>
          <button
            type="button"
            className="setup-modal__close"
            onClick={onClose}
            aria-label="Close setup guide"
          >
            ×
          </button>
        </div>

        {provider === "claude" && <ClaudeContent />}
        {provider === "gemini" && <GeminiContent />}
        {provider === "openai" && <OpenAiContent />}
        {provider === "nine_router" && <NineRouterContent />}

        <div className="setup-modal__footer">
          <a
            className="setup-modal__docs-link"
            href={docsLinkFor(provider)}
            target="_blank"
            rel="noopener noreferrer"
          >
            Open {labelFor(provider)} website ↗
          </a>
          <button
            type="button"
            className="setup-modal__close-btn"
            onClick={onClose}
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

function ClaudeContent() {
  return (
    <div className="setup-modal__body">
      <p>Flowboard calls the Anthropic Messages API directly using your API Key.</p>
      <ol className="setup-modal__steps">
        <li>
          <span className="setup-modal__step-label">Get an API key</span>
          <p className="setup-modal__step-hint">
            Go to the Anthropic Console at{" "}
            <a href="https://console.anthropic.com/" target="_blank" rel="noopener noreferrer">
              console.anthropic.com ↗
            </a>
            , sign in, and navigate to the <b>API Keys</b> section to create a new key.
          </p>
        </li>
        <li>
          <span className="setup-modal__step-label">Save it in Settings</span>
          <p className="setup-modal__step-hint">
            Paste the key in the Claude API key field in Flowboard Settings and click <b>Save Key</b>.
          </p>
        </li>
      </ol>
      <p className="setup-modal__note">
        Your API key is saved locally in <code>~/.flowboard/secrets.json</code> (mode 0600) and is only sent directly to Anthropic's API.
      </p>
    </div>
  );
}

function GeminiContent() {
  return (
    <div className="setup-modal__body">
      <p>Flowboard calls the Google Gemini API directly using your API Key.</p>
      <ol className="setup-modal__steps">
        <li>
          <span className="setup-modal__step-label">Get a Gemini API key</span>
          <p className="setup-modal__step-hint">
            Go to Google AI Studio at{" "}
            <a href="https://aistudio.google.com/" target="_blank" rel="noopener noreferrer">
              aistudio.google.com ↗
            </a>
            , sign in, and click <b>Get API key</b> to generate a new key.
          </p>
        </li>
        <li>
          <span className="setup-modal__step-label">Save it in Settings</span>
          <p className="setup-modal__step-hint">
            Paste the key in the Gemini API key field in Flowboard Settings and click <b>Save Key</b>.
          </p>
        </li>
      </ol>
      <p className="setup-modal__note">
        Your key is stored locally in <code>~/.flowboard/secrets.json</code> and is only used to connect to Google's API endpoints.
      </p>
    </div>
  );
}

function OpenAiContent() {
  return (
    <div className="setup-modal__body">
      <p>Flowboard calls the OpenAI API directly using your API Key.</p>
      <ol className="setup-modal__steps">
        <li>
          <span className="setup-modal__step-label">Get an OpenAI API key</span>
          <p className="setup-modal__step-hint">
            Go to the OpenAI API Keys dashboard at{" "}
            <a href="https://platform.openai.com/api-keys" target="_blank" rel="noopener noreferrer">
              platform.openai.com/api-keys ↗
            </a>
            , sign in, and click <b>Create new secret key</b>.
          </p>
        </li>
        <li>
          <span className="setup-modal__step-label">Save it in Settings</span>
          <p className="setup-modal__step-hint">
            Paste the key in the OpenAI API key field in Flowboard Settings and click <b>Save Key</b>.
          </p>
        </li>
      </ol>
      <p className="setup-modal__note">
        The key stays local and is never shared. You can test your connection once saved.
      </p>
    </div>
  );
}

function NineRouterContent() {
  return (
    <div className="setup-modal__body">
      <p>Flowboard connects to your local 9Router Proxy (https://github.com/decolua/9router) to handle load balancing and auto-fallback between models.</p>
      <ol className="setup-modal__steps">
        <li>
          <span className="setup-modal__step-label">Start 9Router Proxy</span>
          <p className="setup-modal__step-hint">
            Ensure your local 9Router daemon is running. The default local dashboard is located at{" "}
            <a href="http://localhost:20128" target="_blank" rel="noopener noreferrer">
              http://localhost:20128 ↗
            </a>.
          </p>
        </li>
        <li>
          <span className="setup-modal__step-label">Copy 9Router API Key</span>
          <p className="setup-modal__step-hint">
            Go to the 9Router Dashboard &rarr; <b>Endpoint & Key</b>, copy the generated local API Key.
          </p>
        </li>
        <li>
          <span className="setup-modal__step-label">Save and Select Model Group</span>
          <p className="setup-modal__step-hint">
            Paste the API key in Flowboard Settings, click <b>Save Key</b>, and then select the desired Model Combo (e.g. <code>GEMINI</code> or <code>Codex-GPT</code>) from the dropdown list.
          </p>
        </li>
      </ol>
      <p className="setup-modal__note">
        Ensure 9Router is active so Flowboard can fetch the active Model Combos automatically.
      </p>
    </div>
  );
}

function titleFor(p: LLMProviderName): string {
  switch (p) {
    case "claude":
      return "🔑 Claude API Key Setup";
    case "gemini":
      return "🔑 Gemini API Key Setup";
    case "openai":
      return "🔑 OpenAI API Key Setup";
    case "nine_router":
      return "🔑 9Router Proxy Setup";
  }
}

function labelFor(p: LLMProviderName): string {
  switch (p) {
    case "claude":
      return "Anthropic";
    case "gemini":
      return "Google Gemini";
    case "openai":
      return "OpenAI";
    case "nine_router":
      return "9Router";
  }
}

function docsLinkFor(p: LLMProviderName): string {
  switch (p) {
    case "claude":
      return "https://console.anthropic.com/";
    case "gemini":
      return "https://aistudio.google.com/";
    case "openai":
      return "https://platform.openai.com/";
    case "nine_router":
      return "http://localhost:20128/";
  }
}
