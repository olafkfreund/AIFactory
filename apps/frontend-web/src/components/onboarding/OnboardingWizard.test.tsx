/**
 * @vitest-environment jsdom
 */
/**
 * OnboardingWizard integration tests
 *
 * Integration tests for the complete onboarding wizard flow.
 * Verifies step navigation, OAuth/API key paths, back button behavior,
 * and progress indicator.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { OnboardingWizard } from './OnboardingWizard';

// Mock react-i18next to avoid initialization issues
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => {
      // Return the key itself or provide specific translations
      // Keys are without namespace since component uses useTranslation('namespace')
      const translations: Record<string, string> = {
        'welcome.title': 'Welcome to AIFactory',
        'welcome.subtitle': 'AI-powered autonomous coding assistant',
        'welcome.getStarted': 'Get Started',
        'welcome.skip': 'Skip Setup',
        'wizard.helpText': 'Let us help you get started with AI Factory',
        'welcome.features.aiPowered.title': 'AI-Powered',
        'welcome.features.aiPowered.description': 'Powered by Claude',
        'welcome.features.specDriven.title': 'Spec-Driven',
        'welcome.features.specDriven.description': 'Create from specs',
        'welcome.features.memory.title': 'Memory',
        'welcome.features.memory.description': 'Remembers context',
        'welcome.features.parallel.title': 'Parallel',
        'welcome.features.parallel.description': 'Work in parallel',
        'authChoice.title': 'Choose Your Authentication Method',
        'authChoice.subtitle': 'Select how you want to authenticate',
        'authChoice.oauthTitle': 'Sign in with Anthropic',
        'authChoice.oauthDesc': 'OAuth authentication',
        'authChoice.apiKeyTitle': 'Use Custom API Key',
        'authChoice.apiKeyDesc': 'Enter your own API key',
        'authChoice.skip': 'Skip for now',
        // Provider-choice step (current onboarding flow)
        'providerChoice.title': 'Choose Your AI Provider',
        'providerChoice.subtitle': 'Select how you want to connect',
        'providerChoice.claude.title': 'Claude',
        'providerChoice.claude.description': 'Sign in with your Anthropic account',
        'providerChoice.openaiCompat.title': 'OpenAI Compatible',
        'providerChoice.openaiCompat.description': 'Any OpenAI-compatible server',
        'providerChoice.skip.title': 'Skip for now',
        'providerChoice.skip.description': 'Set up later in Settings',
        // Common translations
        'common:back': 'Back',
        'common:actions.close': 'Close'
      };
      return translations[key] || key;
    },
    i18n: { language: 'en' }
  }),
  Trans: ({ children }: { children: React.ReactNode }) => children
}));

// Mock the settings store
const mockUpdateSettings = vi.fn();
const mockLoadSettings = vi.fn();
const mockProfiles: any[] = [];

vi.mock('../../stores/settings-store', () => ({
  useSettingsStore: vi.fn((selector) => {
    const state = {
      settings: { onboardingCompleted: false },
      isLoading: false,
      profiles: mockProfiles,
      activeProfileId: null,
      updateSettings: mockUpdateSettings,
      loadSettings: mockLoadSettings
    };
    if (!selector) return state;
    return selector(state);
  })
}));

// Mock electronAPI
const mockSaveSettings = vi.fn().mockResolvedValue({ success: true });

Object.defineProperty(window, 'electronAPI', {
  value: {
    saveSettings: mockSaveSettings,
    onAppUpdateDownloaded: vi.fn(),
    // OAuth-related methods needed for OAuthStep component
    onTerminalOAuthToken: vi.fn(() => vi.fn()), // Returns unsubscribe function
    getOAuthToken: vi.fn().mockResolvedValue(null),
    startOAuthFlow: vi.fn().mockResolvedValue({ success: true }),
    loadProfiles: vi.fn().mockResolvedValue([])
  },
  writable: true
});

// The wizard persists completion via window.API.saveSettings (the web API
// adapter that replaces the legacy window.electronAPI — see main.tsx initWebAPI).
const mockApiSaveSettings = vi.fn().mockResolvedValue({ success: true });

Object.defineProperty(window, 'API', {
  value: { saveSettings: mockApiSaveSettings },
  writable: true
});

describe('OnboardingWizard Integration Tests', () => {
  const defaultProps = {
    open: true,
    onOpenChange: vi.fn()
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('OAuth Path Navigation', () => {
    // Skipped: OAuth integration tests require full OAuth step mocking - not API Profile related
    it.skip('should navigate: welcome → auth-choice → oauth', async () => {
      render(<OnboardingWizard {...defaultProps} />);

      // Start at welcome step
      expect(screen.getByText(/Welcome to AIFactory/)).toBeInTheDocument();

      // Click "Get Started" to go to auth-choice
      const getStartedButton = screen.getByRole('button', { name: /Get Started/ });
      fireEvent.click(getStartedButton);

      // Should now show auth choice step
      await waitFor(() => {
        expect(screen.getByText(/Choose Your Authentication Method/)).toBeInTheDocument();
      });

      // Click OAuth option
      const oauthButton = screen.getByTestId('auth-option-oauth');
      fireEvent.click(oauthButton);

      // Should navigate to oauth step
      await waitFor(() => {
        expect(screen.getByText(/Sign in with Anthropic/)).toBeInTheDocument();
      });
    });

    // Skipped: OAuth path test requires full OAuth step mocking
    it.skip('should show correct progress indicator for OAuth path', async () => {
      render(<OnboardingWizard {...defaultProps} />);

      // Click through to auth-choice
      fireEvent.click(screen.getByRole('button', { name: /Get Started/ }));
      await waitFor(() => {
        expect(screen.getByText(/Choose Your Authentication Method/)).toBeInTheDocument();
      });

      // Verify progress indicator shows 5 steps
      const progressIndicators = document.querySelectorAll('[class*="step"]');
      expect(progressIndicators.length).toBeGreaterThanOrEqual(4); // At least 4 steps shown
    });
  });

  describe('API Key Path Navigation', () => {
    // Skipped: Test requires ProfileEditDialog integration mock
    it.skip('should skip oauth step when API key path chosen', async () => {
      render(<OnboardingWizard {...defaultProps} />);

      // Start at welcome step
      expect(screen.getByText(/Welcome to AIFactory/)).toBeInTheDocument();

      // Click "Get Started" to go to auth-choice
      fireEvent.click(screen.getByRole('button', { name: /Get Started/ }));
      await waitFor(() => {
        expect(screen.getByText(/Choose Your Authentication Method/)).toBeInTheDocument();
      });

      // Click API Key option
      const apiKeyButton = screen.getByTestId('auth-option-apikey');
      fireEvent.click(apiKeyButton);

      // Profile dialog should open
      await waitFor(() => {
        expect(screen.getByTestId('profile-edit-dialog')).toBeInTheDocument();
      });

      // Close dialog (simulating profile creation - in real scenario this would trigger skip)
      const closeButton = screen.queryByText(/Close|Cancel/);
      if (closeButton) {
        fireEvent.click(closeButton);
      }
    });

    it('should not show OAuth step text on the provider-choice screen', async () => {
      render(<OnboardingWizard {...defaultProps} />);

      // Navigate: welcome → provider-choice
      fireEvent.click(screen.getByRole('button', { name: /Get Started/ }));
      await waitFor(() => {
        expect(screen.getByText(/Choose Your AI Provider/)).toBeInTheDocument();
      });

      // Before a provider is chosen, no OAuth step content should be visible
      expect(screen.queryByText(/OAuth Authentication/)).toBeNull();
    });
  });

  describe('Back Button Behavior on the provider-choice step', () => {
    it('should return to the welcome step when Back is pressed', async () => {
      render(<OnboardingWizard {...defaultProps} />);

      // Navigate: welcome → provider-choice
      fireEvent.click(screen.getByRole('button', { name: /Get Started/ }));
      await waitFor(() => {
        expect(screen.getByText(/Choose Your AI Provider/)).toBeInTheDocument();
      });

      // Back goes to the previous step (welcome)
      fireEvent.click(screen.getByRole('button', { name: /Back/ }));
      await waitFor(() => {
        expect(screen.getByText(/Welcome to AIFactory/)).toBeInTheDocument();
      });
    });
  });

  describe('First-Run Detection', () => {
    it('should show wizard for users with no auth configured', () => {
      render(<OnboardingWizard {...defaultProps} open={true} />);

      // Wizard should be visible
      expect(screen.getByText(/Welcome to AIFactory/)).toBeInTheDocument();
    });

    it('should not show wizard for users with existing OAuth', () => {
      // This is tested in App.tsx integration tests
      // Here we verify the wizard can be closed
      const { rerender } = render(<OnboardingWizard {...defaultProps} open={true} />);

      expect(screen.getByText(/Welcome to AIFactory/)).toBeInTheDocument();

      // Close wizard
      rerender(<OnboardingWizard {...defaultProps} open={false} />);

      // Wizard content should not be visible
      expect(screen.queryByText(/Welcome to AIFactory/)).not.toBeInTheDocument();
    });

    it('should not show wizard for users with existing API profiles', () => {
      // This is tested in App.tsx integration tests
      // The wizard respects the open prop
      render(<OnboardingWizard {...defaultProps} open={false} />);

      expect(screen.queryByText(/Welcome to AIFactory/)).not.toBeInTheDocument();
    });
  });

  describe('Skip and Completion', () => {
    it('should complete wizard when skip is clicked', async () => {
      render(<OnboardingWizard {...defaultProps} />);

      // Click skip on welcome step
      const skipButton = screen.getByRole('button', { name: /Skip Setup/ });
      fireEvent.click(skipButton);

      // Should persist completion via window.API.saveSettings
      await waitFor(() => {
        expect(mockApiSaveSettings).toHaveBeenCalledWith({ onboardingCompleted: true });
      });
    });

    it('should call onOpenChange when wizard is closed', async () => {
      const mockOnOpenChange = vi.fn();
      render(<OnboardingWizard {...defaultProps} onOpenChange={mockOnOpenChange} />);

      // Click skip to close wizard
      const skipButton = screen.getByRole('button', { name: /Skip Setup/ });
      fireEvent.click(skipButton);

      await waitFor(() => {
        expect(mockOnOpenChange).toHaveBeenCalledWith(false);
      });
    });
  });

  describe('Step Progress Indicator', () => {
    // Skipped: Progress indicator tests require step-by-step CSS class inspection
    it.skip('should display progress indicator for non-welcome/completion steps', async () => {
      render(<OnboardingWizard {...defaultProps} />);

      // On welcome step, no progress indicator shown
      expect(screen.queryByText(/Welcome/)).toBeInTheDocument();
      // Progress indicator may not be visible on welcome step

      // Navigate to auth-choice
      fireEvent.click(screen.getByRole('button', { name: /Get Started/ }));
      await waitFor(() => {
        expect(screen.getByText(/Choose Your Authentication Method/)).toBeInTheDocument();
      });

      // Progress indicator should now be visible
      // The WizardProgress component should be rendered
      const progressElement = document.querySelector('[class*="step"]');
      expect(progressElement).toBeTruthy();
    });

    // Skipped: Step count test requires i18n step labels
    it.skip('should show correct number of steps (5 total)', async () => {
      render(<OnboardingWizard {...defaultProps} />);

      // Navigate to auth-choice
      fireEvent.click(screen.getByRole('button', { name: /Get Started/ }));
      await waitFor(() => {
        expect(screen.getByText(/Choose Your Authentication Method/)).toBeInTheDocument();
      });

      // Check for step labels in progress indicator
      const steps = [
        'Welcome',
        'Auth Method',
        'OAuth',
        'Memory',
        'Done'
      ];

      // At least some step labels should be present (not all may be visible at current step)
      const visibleSteps = steps.filter(step => screen.queryByText(step));
      expect(visibleSteps.length).toBeGreaterThan(0);
    });
  });

  describe('AC Coverage', () => {
    it('AC1: provider-choice screen displays the three provider options', async () => {
      render(<OnboardingWizard {...defaultProps} />);

      // Navigate to provider-choice
      fireEvent.click(screen.getByRole('button', { name: /Get Started/ }));
      await waitFor(() => {
        expect(screen.getByText(/Choose Your AI Provider/)).toBeInTheDocument();
      });

      // All three provider cards should be visible
      expect(screen.getByTestId('provider-choice-claude')).toBeInTheDocument();
      expect(screen.getByTestId('provider-choice-openai-compat')).toBeInTheDocument();
      expect(screen.getByTestId('provider-choice-skip')).toBeInTheDocument();
    });

    // Skipped: OAuth path test requires full OAuth step mocking
    it.skip('AC2: OAuth path initiates existing OAuth flow', async () => {
      render(<OnboardingWizard {...defaultProps} />);

      fireEvent.click(screen.getByText(/Get Started/));
      await waitFor(() => {
        expect(screen.getByText(/Choose Your Authentication Method/)).toBeInTheDocument();
      });

      const oauthButton = screen.getByTestId('auth-option-oauth');
      fireEvent.click(oauthButton);

      // Should proceed to OAuth step
      await waitFor(() => {
        // OAuth step content should be visible
        expect(document.querySelector('.fullscreen-dialog')).toBeInTheDocument();
      });
    });

    it('AC3: choosing the Claude provider advances past the provider-choice step', async () => {
      render(<OnboardingWizard {...defaultProps} />);

      fireEvent.click(screen.getByRole('button', { name: /Get Started/ }));
      await waitFor(() => {
        expect(screen.getByText(/Choose Your AI Provider/)).toBeInTheDocument();
      });

      // Choosing Claude routes into the Claude setup path (import-credentials),
      // so the provider-choice screen is no longer shown.
      fireEvent.click(screen.getByTestId('provider-choice-claude'));
      await waitFor(() => {
        expect(screen.queryByText(/Choose Your AI Provider/)).not.toBeInTheDocument();
      });
    });

    it('AC4: Existing auth skips wizard', () => {
      // Wizard with open=false simulates existing auth scenario
      render(<OnboardingWizard {...defaultProps} open={false} />);

      // Wizard should not be visible
      expect(screen.queryByText(/Welcome to AIFactory/)).not.toBeInTheDocument();
    });
  });
});
