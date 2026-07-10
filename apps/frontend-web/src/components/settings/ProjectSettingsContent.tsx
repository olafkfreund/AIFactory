import { useEffect, useRef } from 'react';
import { FolderOpen } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { SettingsSection } from './SettingsSection';
import { useProjectSettings, UseProjectSettingsReturn } from '../project-settings/hooks/useProjectSettings';
import { SectionRouter } from './sections/SectionRouter';
import { createHookProxy } from './utils/hookProxyFactory';
import type { Project } from '../../shared/types';

export type ProjectSettingsSection = 'general' | 'github' | 'memory' | 'mcp';

interface ProjectSettingsContentProps {
  project: Project | undefined;
  activeSection: ProjectSettingsSection;
  isOpen: boolean;
  onHookReady: (hook: UseProjectSettingsReturn | null) => void;
}

/**
 * Renders project settings content based on the active section.
 * Exposes hook state to parent for save coordination.
 */
export function ProjectSettingsContent({
  project,
  activeSection,
  isOpen,
  onHookReady
}: ProjectSettingsContentProps) {
  const { t } = useTranslation('settings');

  // Show empty state if no project selected
  if (!project) {
    return (
      <SettingsSection
        title={t('projectSettings.noProjectSelected.title')}
        description={t('projectSettings.noProjectSelected.description')}
      >
        <div className="flex flex-col items-center justify-center py-12 text-center">
          <FolderOpen className="h-12 w-12 text-muted-foreground/50 mb-4" />
          <p className="text-muted-foreground">
            Select a project to view and edit its settings
          </p>
        </div>
      </SettingsSection>
    );
  }

  return (
    <ProjectSettingsContentInner
      project={project}
      activeSection={activeSection}
      isOpen={isOpen}
      onHookReady={onHookReady}
    />
  );
}

/**
 * Inner component that uses the project settings hook.
 * Separated to ensure the hook is only called when a project is selected.
 */
function ProjectSettingsContentInner({
  project,
  activeSection,
  isOpen,
  onHookReady
}: {
  project: Project;
  activeSection: ProjectSettingsSection;
  isOpen: boolean;
  onHookReady: (hook: UseProjectSettingsReturn | null) => void;
}) {
  const hook = useProjectSettings(project, isOpen);

  // Keep a stable ref to the hook for the parent
  const hookRef = useRef(hook);
  hookRef.current = hook;

  const {
    settings,
    setSettings,
    versionInfo,
    isCheckingVersion,
    isUpdating,
    envConfig,
    isLoadingEnv,
    envError,
    updateEnvConfig,
    showOpenAIKey,
    setShowOpenAIKey,
    showGitHubToken,
    setShowGitHubToken,
    expandedSections: _expandedSections,
    toggleSection: _toggleSection,
    gitHubConnectionStatus,
    isCheckingGitHub,
    handleInitialize,
    error
  } = hook;

  // Expose hook to parent for save coordination - only once when dialog opens
  // We use hookRef to avoid infinite loops (hook object is recreated each render)
  useEffect(() => {
    if (isOpen) {
      const hookProxy = createHookProxy(hookRef);
      onHookReady(hookProxy);
    }
    return () => {
      onHookReady(null);
    };
  }, [isOpen, onHookReady]);

  return (
    <>
      <SectionRouter
        activeSection={activeSection}
        project={project}
        settings={settings}
        setSettings={setSettings}
        versionInfo={versionInfo}
        isCheckingVersion={isCheckingVersion}
        isUpdating={isUpdating}
        envConfig={envConfig}
        isLoadingEnv={isLoadingEnv}
        envError={envError}
        updateEnvConfig={updateEnvConfig}
        showOpenAIKey={showOpenAIKey}
        setShowOpenAIKey={setShowOpenAIKey}
        showGitHubToken={showGitHubToken}
        setShowGitHubToken={setShowGitHubToken}
        gitHubConnectionStatus={gitHubConnectionStatus}
        isCheckingGitHub={isCheckingGitHub}
        handleInitialize={handleInitialize}
      />

      {(error || envError) && (
        <div className="mt-4 rounded-lg bg-destructive/10 border border-destructive/30 p-3 text-sm text-destructive">
          {error || envError}
        </div>
      )}
    </>
  );
}
