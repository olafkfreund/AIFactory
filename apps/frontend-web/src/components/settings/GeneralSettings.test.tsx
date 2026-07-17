/**
 * @vitest-environment jsdom
 */
/**
 * Tests for the parallel build execution controls in GeneralSettings (#376).
 *
 * Parallel execution runs multiple coding agents concurrently in isolated git
 * worktrees, so it ships OFF by default. These tests cover the toggle wiring,
 * the workers input only appearing when enabled, and the input clamping to the
 * same 1-8 range the backend validator enforces.
 */
import { describe, it, expect, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { GeneralSettings } from './GeneralSettings';
import type { AppSettings } from '../../shared/types';

// t() returns the key so assertions do not depend on copy.
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key })
}));

// AgentProfileSettings pulls in stores/network; not under test here.
vi.mock('./AgentProfileSettings', () => ({
  AgentProfileSettings: () => null
}));

vi.mock('../../shared/constants', async () => {
  const actual = await vi.importActual<typeof import('../../shared/constants')>(
    '../../shared/constants'
  );
  return {
    ...actual,
    fetchOllamaModels: vi.fn().mockResolvedValue([]),
    fetchOpenAICompatibleModels: vi.fn().mockResolvedValue([])
  };
});

function renderSettings(overrides: Partial<AppSettings> = {}) {
  const onSettingsChange = vi.fn();
  const settings = { ...overrides } as AppSettings;
  render(
    <GeneralSettings
      settings={settings}
      onSettingsChange={onSettingsChange}
      section="agent"
    />
  );
  return { onSettingsChange };
}

describe('GeneralSettings parallel execution', () => {
  it('renders the toggle off and hides the workers input by default', () => {
    renderSettings();

    expect(screen.getByRole('switch', { name: /parallelExecution/i })).not.toBeChecked();
    expect(screen.queryByLabelText(/parallelWorkers/i)).not.toBeInTheDocument();
  });

  it('enables parallel execution via the toggle', () => {
    const { onSettingsChange } = renderSettings();

    fireEvent.click(screen.getByRole('switch', { name: /parallelExecution/i }));

    expect(onSettingsChange).toHaveBeenCalledWith(
      expect.objectContaining({ parallelExecution: true })
    );
  });

  it('shows the workers input defaulting to 3 once enabled', () => {
    renderSettings({ parallelExecution: true });

    expect(screen.getByLabelText(/parallelWorkers/i)).toHaveValue(3);
  });

  it('propagates a valid worker count', () => {
    const { onSettingsChange } = renderSettings({ parallelExecution: true });

    fireEvent.change(screen.getByLabelText(/parallelWorkers/i), {
      target: { value: '5' }
    });

    expect(onSettingsChange).toHaveBeenCalledWith(
      expect.objectContaining({ parallelWorkers: 5 })
    );
  });

  it.each([
    ['99', 8],
    ['0', 1],
    ['', 3]
  ])('clamps a worker count of "%s" to %i', (input, expected) => {
    const { onSettingsChange } = renderSettings({ parallelExecution: true });

    fireEvent.change(screen.getByLabelText(/parallelWorkers/i), {
      target: { value: input }
    });

    expect(onSettingsChange).toHaveBeenCalledWith(
      expect.objectContaining({ parallelWorkers: expected })
    );
  });
});
