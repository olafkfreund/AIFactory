import {
  RefreshCw,
  Download,
  CheckCircle2,
  AlertCircle,
  Loader2
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '../ui/button';
import { Label } from '../ui/label';
import { Switch } from '../ui/switch';
import { Separator } from '../ui/separator';
import type {
  Project,
  ProjectSettings as ProjectSettingsType,
  AutoBuildVersionInfo
} from '../../shared/types';

interface GeneralSettingsProps {
  project: Project;
  settings: ProjectSettingsType;
  setSettings: React.Dispatch<React.SetStateAction<ProjectSettingsType>>;
  versionInfo: AutoBuildVersionInfo | null;
  isCheckingVersion: boolean;
  isUpdating: boolean;
  handleInitialize: () => Promise<void>;
}

export function GeneralSettings({
  project,
  settings,
  setSettings,
  versionInfo,
  isCheckingVersion,
  isUpdating,
  handleInitialize
}: GeneralSettingsProps) {
  const { t } = useTranslation(['settings']);

  return (
    <>
      {/* Auto-Build Integration */}
      <section className="space-y-4">
        <h3 className="text-sm font-semibold text-foreground">Auto-Build Integration</h3>
        {!project.autoBuildPath ? (
          <div className="rounded-lg border border-border bg-muted/50 p-4">
            <div className="flex items-start gap-3">
              <AlertCircle className="h-5 w-5 text-warning mt-0.5 shrink-0" />
              <div className="flex-1">
                <p className="text-sm font-medium text-foreground">Not Initialized</p>
                <p className="text-xs text-muted-foreground mt-1">
                  Initialize Auto-Build to enable task creation and agent workflows.
                </p>
                <Button
                  size="sm"
                  className="mt-3"
                  onClick={handleInitialize}
                  disabled={isUpdating}
                >
                  {isUpdating ? (
                    <>
                      <RefreshCw className="mr-2 h-4 w-4 animate-spin" />
                      Initializing...
                    </>
                  ) : (
                    <>
                      <Download className="mr-2 h-4 w-4" />
                      Initialize Auto-Build
                    </>
                  )}
                </Button>
              </div>
            </div>
          </div>
        ) : (
          <div className="rounded-lg border border-border bg-muted/50 p-4 space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <CheckCircle2 className="h-4 w-4 text-success" />
                <span className="text-sm font-medium text-foreground">Initialized</span>
              </div>
              <code className="text-xs bg-background px-2 py-1 rounded">
                {project.autoBuildPath}
              </code>
            </div>
            {isCheckingVersion ? (
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <Loader2 className="h-3 w-3 animate-spin" />
                Checking status...
              </div>
            ) : versionInfo && (
              <div className="text-xs text-muted-foreground">
                {versionInfo.isInitialized ? 'Initialized' : 'Not initialized'}
              </div>
            )}
          </div>
        )}
      </section>

      {project.autoBuildPath && (
        <>
          <Separator />

          {/* CLAUDE.md Setting */}
          <section className="space-y-4">
            <div className="flex items-center justify-between">
              <div className="space-y-0.5">
                <Label className="font-normal text-foreground">
                  {t('projectSections.general.useClaudeMd')}
                </Label>
                <p className="text-xs text-muted-foreground">
                  {t('projectSections.general.useClaudeMdDescription')}
                </p>
              </div>
              <Switch
                checked={settings.useClaudeMd ?? true}
                onCheckedChange={(checked) =>
                  { setSettings({ ...settings, useClaudeMd: checked }); }
                }
              />
            </div>
          </section>

          <Separator />

          {/* Remote Control default — when on, new tasks start with
              enableRemoteControl: true unless the wizard overrides */}
          <section className="space-y-4">
            <div className="flex items-center justify-between">
              <div className="space-y-0.5">
                <Label className="font-normal text-foreground">
                  Enable Remote Control by default
                </Label>
                <p className="text-xs text-muted-foreground">
                  Every new task in this project gets the Claude Code{' '}
                  <code className="text-xs bg-muted px-1 rounded">--remote-control</code>{' '}
                  flag, so you can drive its session from{' '}
                  <a
                    href="https://claude.ai/code"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="underline hover:text-foreground"
                  >
                    claude.ai/code
                  </a>{' '}
                  or the Claude mobile app. The wizard's per-task toggle still
                  overrides on a case-by-case basis. Requires a paid Anthropic
                  subscription and{' '}
                  <code className="text-xs bg-muted px-1 rounded">claude auth login</code>{' '}
                  on the AIFactory host.
                </p>
              </div>
              <Switch
                checked={settings.remoteControlByDefault ?? false}
                onCheckedChange={(checked) =>
                  { setSettings({ ...settings, remoteControlByDefault: checked }); }
                }
              />
            </div>
          </section>

          <Separator />

          {/* Copilot Delegation default — when on, new tasks start with
              enableDelegation: true (only effective on GitHub projects) */}
          <section className="space-y-4">
            <div className="flex items-center justify-between">
              <div className="space-y-0.5">
                <Label className="font-normal text-foreground">
                  Delegate coding to Copilot for new tasks by default
                </Label>
                <p className="text-xs text-muted-foreground">
                  Every new task in this GitHub project starts with delegation
                  enabled — AIFactory plans the spec, then hands the issue to
                  GitHub Copilot Coding Agent for implementation. Uses your
                  Copilot seat instead of Claude tokens for the coder phase.
                  The wizard's per-task toggle still overrides. Only effective
                  on GitHub projects; GitLab Duo Workflow delegation lands in
                  a later release.
                </p>
              </div>
              <Switch
                checked={settings.delegateByDefault ?? false}
                onCheckedChange={(checked) =>
                  { setSettings({ ...settings, delegateByDefault: checked }); }
                }
              />
            </div>
          </section>

          <Separator />

          {/* PR endgame — auto-open a PR on a clean build, then (optionally)
              auto-merge once GitHub Copilot's code review APPROVES it. */}
          <section className="space-y-4">
            <div className="flex items-center justify-between">
              <div className="space-y-0.5">
                <Label className="font-normal text-foreground">
                  Auto-open a PR when a build finishes cleanly
                </Label>
                <p className="text-xs text-muted-foreground">
                  When a build completes its QA, AIFactory pushes the branch,
                  opens a pull request, and requests a GitHub Copilot code review —
                  then stops for a human. Requires a GitHub project. Default off.
                </p>
              </div>
              <Switch
                checked={settings.autoPr ?? false}
                onCheckedChange={(checked) =>
                  { setSettings({ ...settings, autoPr: checked }); }
                }
              />
            </div>
            <div className="flex items-center justify-between">
              <div className="space-y-0.5">
                <Label className="font-normal text-foreground">
                  Auto-merge after Copilot approves
                </Label>
                <p className="text-xs text-muted-foreground">
                  Once the auto-PR is open, AIFactory waits for GitHub Copilot's
                  code review and merges + re-tests <strong>only</strong> if Copilot
                  posts an <strong>APPROVED</strong> review. If Copilot requests
                  changes, finds problems, or never reviews, the PR is left open
                  for a human — nothing is merged around Copilot's findings. No
                  effect unless &ldquo;Auto-open a PR&rdquo; is on. Default off.
                </p>
              </div>
              <Switch
                checked={settings.autoMerge ?? false}
                disabled={!(settings.autoPr ?? false)}
                onCheckedChange={(checked) =>
                  { setSettings({ ...settings, autoMerge: checked }); }
                }
              />
            </div>
            <div className="flex items-center justify-between">
              <div className="space-y-0.5">
                <Label className="font-normal text-foreground">
                  Pre-merge reviewer
                </Label>
                <p className="text-xs text-muted-foreground">
                  Who must approve before auto-merge. <strong>AIFactory</strong> uses
                  its own code-review engine on the project's provider (Claude/Ollama —
                  no Copilot credits). <strong>Copilot</strong> uses GitHub Copilot's
                  review (requires Copilot code-review credits). <strong>Any</strong>{' '}
                  accepts any approving GitHub review.
                </p>
              </div>
              <select
                className="rounded-md border border-input bg-background px-2 py-1 text-sm"
                value={settings.prReviewer ?? 'aifactory'}
                disabled={!(settings.autoPr ?? false)}
                onChange={(e) =>
                  { setSettings({
                    ...settings,
                    prReviewer: e.target.value as 'aifactory' | 'copilot' | 'any',
                  }); }
                }
              >
                <option value="aifactory">AIFactory (Claude/Ollama)</option>
                <option value="copilot">GitHub Copilot</option>
                <option value="any">Any approval</option>
              </select>
            </div>
          </section>

          <Separator />

          {/* Notifications */}
          <section className="space-y-4">
            <h3 className="text-sm font-semibold text-foreground">Notifications</h3>
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <Label className="font-normal text-foreground">On Task Complete</Label>
                <Switch
                  checked={settings.notifications.onTaskComplete}
                  onCheckedChange={(checked) =>
                    { setSettings({
                      ...settings,
                      notifications: {
                        ...settings.notifications,
                        onTaskComplete: checked
                      }
                    }); }
                  }
                />
              </div>
              <div className="flex items-center justify-between">
                <Label className="font-normal text-foreground">On Task Failed</Label>
                <Switch
                  checked={settings.notifications.onTaskFailed}
                  onCheckedChange={(checked) =>
                    { setSettings({
                      ...settings,
                      notifications: {
                        ...settings.notifications,
                        onTaskFailed: checked
                      }
                    }); }
                  }
                />
              </div>
              <div className="flex items-center justify-between">
                <Label className="font-normal text-foreground">On Review Needed</Label>
                <Switch
                  checked={settings.notifications.onReviewNeeded}
                  onCheckedChange={(checked) =>
                    { setSettings({
                      ...settings,
                      notifications: {
                        ...settings.notifications,
                        onReviewNeeded: checked
                      }
                    }); }
                  }
                />
              </div>
              <div className="flex items-center justify-between">
                <Label className="font-normal text-foreground">Sound</Label>
                <Switch
                  checked={settings.notifications.sound}
                  onCheckedChange={(checked) =>
                    { setSettings({
                      ...settings,
                      notifications: {
                        ...settings.notifications,
                        sound: checked
                      }
                    }); }
                  }
                />
              </div>
            </div>
          </section>
        </>
      )}
    </>
  );
}
