/**
 * @vitest-environment jsdom
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { render, screen, waitFor } from '@testing-library/react';
import * as apiClient from '../../lib/api-client';
import { DEFAULT_APP_SETTINGS } from '../../shared/constants';
import { useSettingsStore } from '../../stores/settings-store';
import { AgentProfileSettings } from './AgentProfileSettings';
import { GeneralSettings } from './GeneralSettings';
import { AgentProfileSelector } from '../AgentProfileSelector';
import type { AppSettings } from '../../shared/types';

describe('B6 provider probe gating and B12 unset profile rendering', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    useSettingsStore.setState({
      settings: {
        ...DEFAULT_APP_SETTINGS,
        selectedAgentProfile: undefined,
        graphitiLlmProvider: undefined,
        memoryEmbeddingProvider: undefined,
        ollamaBaseUrl: undefined,
        llmOllamaBaseUrl: 'http://localhost:11434',
        llmProvider: 'anthropic',
        llmOpenaiBaseUrl: undefined,
      },
    });
  });

  it('does not probe Ollama or openai-compat when unconfigured, while still loading saved endpoints', async () => {
    const apiSpy = vi
      .spyOn(apiClient, 'apiRequest')
      .mockImplementation((endpoint: string) => {
        if (endpoint === '/llm-endpoints') {
          return Promise.resolve({
            success: true,
            data: [{ id: 'ep-1', label: 'vLLM Prod', default_model: 'Qwen2.5-Coder-32B' }],
          });
        }
        if (endpoint === '/github/models') {
          return Promise.resolve({ success: true, data: [] });
        }
        return Promise.resolve({ success: true, data: { models: [] } });
      });

    const settings = useSettingsStore.getState().settings;
    render(
      <GeneralSettings
        settings={settings}
        onSettingsChange={vi.fn()}
        section="agent"
      />
    );
    render(
      <AgentProfileSelector
        profileId="auto"
        model="opus"
        thinkingLevel="high"
        onProfileChange={vi.fn()}
        onModelChange={vi.fn()}
        onThinkingLevelChange={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(apiSpy).toHaveBeenCalledWith('/llm-endpoints');
    });

    const calledEndpoints = apiSpy.mock.calls.map(([ep]) => ep);
    expect(calledEndpoints.some((ep) => ep.startsWith('/settings/ollama/models'))).toBe(false);
    expect(calledEndpoints.some((ep) => ep.startsWith('/settings/openai-compat/models'))).toBe(false);

    // Saved endpoint model is used to resolve labels when configured on a phase
    useSettingsStore.setState({
      settings: {
        ...settings,
        customPhaseModels: {
          spec: 'openai-compatible:vLLM Prod:Qwen2.5-Coder-32B',
          planning: 'opus',
          coding: 'sonnet',
          qa: 'opus',
          qa_fixer: 'opus',
        },
      },
    });

    await waitFor(() => {
      expect(screen.getAllByText('vLLM Prod — Qwen2.5-Coder-32B').length).toBeGreaterThan(0);
    });
  });

  it('passes configured ollamaBaseUrl and llmOpenaiBaseUrl when probing', async () => {
    const apiSpy = vi
      .spyOn(apiClient, 'apiRequest')
      .mockImplementation((endpoint: string) => {
        if (endpoint.startsWith('/settings/ollama/models')) {
          return Promise.resolve({
            success: true,
            data: { models: [{ name: 'qwen2.5-coder:7b' }] },
          });
        }
        if (endpoint.startsWith('/settings/openai-compat/models')) {
          return Promise.resolve({
            success: true,
            data: { models: [{ name: 'local-deepseek' }] },
          });
        }
        return Promise.resolve({ success: true, data: [] });
      });

    const configuredSettings: AppSettings = {
      ...DEFAULT_APP_SETTINGS,
      ollamaBaseUrl: 'http://ollama.internal:11434',
      llmOpenaiBaseUrl: 'http://vllm.internal:8000',
    };
    useSettingsStore.setState({ settings: configuredSettings });

    render(
      <GeneralSettings
        settings={configuredSettings}
        onSettingsChange={vi.fn()}
        section="agent"
      />
    );

    await waitFor(() => {
      expect(apiSpy).toHaveBeenCalledWith('/settings/ollama/models', {
        method: 'POST',
        body: { ollamaBaseUrl: 'http://ollama.internal:11434' },
      });
      expect(apiSpy).toHaveBeenCalledWith(
        `/settings/openai-compat/models?baseUrl=${encodeURIComponent('http://vllm.internal:8000')}`
      );
    });
  });

  it('uses an explicit LLM URL when the other Ollama URL is the server default', async () => {
    const apiSpy = vi.spyOn(apiClient, 'apiRequest').mockResolvedValue({ success: true, data: { models: [] } });
    const settings: AppSettings = {
      ...DEFAULT_APP_SETTINGS,
      llmProvider: 'anthropic',
      ollamaBaseUrl: 'http://localhost:11434',
      llmOllamaBaseUrl: 'http://custom.internal:11434',
    };
    render(<GeneralSettings settings={settings} onSettingsChange={vi.fn()} section="agent" />);
    await waitFor(() => expect(apiSpy).toHaveBeenCalledWith('/settings/ollama/models', {
      method: 'POST',
      body: { ollamaBaseUrl: 'http://custom.internal:11434' },
    }));
  });

  it('shows "Auto (default)" without highlighting any profile card when selectedAgentProfile is unset, and keeps phase controls visible', async () => {
    const apiSpy = vi.spyOn(apiClient, 'apiRequest').mockResolvedValue({ success: true, data: [] });

    const { container } = render(<AgentProfileSettings />);

    await waitFor(() => {
      expect(apiSpy).toHaveBeenCalledWith('/llm-endpoints');
    });

    expect(screen.getByText('Auto (default)')).toBeInTheDocument();
    expect(screen.queryByText('Auto (Optimized)')).not.toBeInTheDocument();

    // No profile card is highlighted as selected
    expect(container.querySelectorAll('button.border-primary')).toHaveLength(0);

    // Phase configuration controls remain visible
    expect(screen.getByText('Phase Configuration')).toBeInTheDocument();
    expect(screen.getByText('Spec Creation')).toBeInTheDocument();
  });

  it('highlights the Auto profile card and shows "Auto (Optimized)" once explicitly selected', async () => {
    const apiSpy = vi.spyOn(apiClient, 'apiRequest').mockResolvedValue({ success: true, data: [] });
    useSettingsStore.setState({
      settings: {
        ...DEFAULT_APP_SETTINGS,
        selectedAgentProfile: 'auto',
      },
    });

    const { container } = render(<AgentProfileSettings />);

    await waitFor(() => {
      expect(apiSpy).toHaveBeenCalledWith('/llm-endpoints');
    });

    expect(screen.getByText('Auto (Optimized)')).toBeInTheDocument();
    expect(screen.queryByText('Auto (default)')).not.toBeInTheDocument();
    expect(container.querySelectorAll('button.border-primary')).toHaveLength(1);
  });
});
