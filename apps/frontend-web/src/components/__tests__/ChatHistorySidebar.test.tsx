/**
 * @vitest-environment jsdom
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { render, screen } from '@testing-library/react';
import { ChatHistorySidebar } from '../ChatHistorySidebar';
import { newSession, sendMessage, switchSession, useInsightsStore } from '../../stores/insights-store';
import type { API, InsightsSession, InsightsSessionSummary } from '../../shared/types';

function makeSummary(
  id: string,
  title: string,
  messageCount: number
): InsightsSessionSummary {
  const now = new Date();
  return {
    id,
    projectId: 'proj-1',
    title,
    messageCount,
    createdAt: now,
    updatedAt: now,
  };
}

function makeSession(id: string, title: string): InsightsSession {
  const now = new Date();
  return {
    id,
    projectId: 'proj-1',
    title,
    messages: [],
    createdAt: now,
    updatedAt: now,
  };
}

describe('ChatHistorySidebar and insights-store session visibility (B17)', () => {
  const sendInsightsMessage = vi.fn();
  const switchInsightsSession = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    (window as unknown as { API: Partial<API> }).API = {
      sendInsightsMessage,
      switchInsightsSession,
    };
    useInsightsStore.setState({
      session: null,
      sessions: [],
      localSummaries: {},
    });
  });

  it('hides empty non-active sessions and shows empty state when only non-active empty sessions exist', () => {
    const emptyInactive = makeSummary('s-empty', 'Empty Session', 0);

    const { rerender } = render(
      <ChatHistorySidebar
        sessions={[emptyInactive]}
        currentSessionId="s-other"
        isLoading={false}
        onNewSession={vi.fn()}
        onSelectSession={vi.fn()}
        onDeleteSession={vi.fn()}
        onRenameSession={vi.fn()}
      />
    );

    expect(screen.queryByText('Empty Session')).not.toBeInTheDocument();
    expect(screen.getByText('No conversations yet')).toBeInTheDocument();

    // When the empty session IS the active session, it is shown
    rerender(
      <ChatHistorySidebar
        sessions={[emptyInactive]}
        currentSessionId="s-empty"
        isLoading={false}
        onNewSession={vi.fn()}
        onSelectSession={vi.fn()}
        onDeleteSession={vi.fn()}
        onRenameSession={vi.fn()}
      />
    );

    expect(screen.getByText('Empty Session')).toBeInTheDocument();
  });

  it('bumps session messageCount on sendMessage so a session with a sent message survives switching away', async () => {
    const sessionA = makeSession('s-a', 'First Chat');
    const sessionB = makeSession('s-b', 'Second Chat');

    useInsightsStore.setState({
      session: sessionA,
      sessions: [
        makeSummary('s-a', 'First Chat', 0),
        makeSummary('s-b', 'Second Chat', 2),
      ],
    });

    // Send a message in session A (which started with messageCount: 0)
    sendMessage('proj-1', 'Hello from session A');

    expect(
      useInsightsStore.getState().sessions.find((s) => s.id === 's-a')?.messageCount
    ).toBe(1);

    // Switch to session B
    switchInsightsSession.mockResolvedValue({
      success: true,
      data: sessionB,
    });
    await switchSession('proj-1', 's-b');

    const state = useInsightsStore.getState();
    render(
      <ChatHistorySidebar
        sessions={state.sessions}
        currentSessionId={state.session?.id ?? null}
        isLoading={false}
        onNewSession={vi.fn()}
        onSelectSession={vi.fn()}
        onDeleteSession={vi.fn()}
        onRenameSession={vi.fn()}
      />
    );

    // Both session A (now with 1 message) and session B are visible
    expect(screen.getByText('First Chat')).toBeInTheDocument();
    expect(screen.getByText('Second Chat')).toBeInTheDocument();
  });

  it('keeps a sent chat when an older summaries response arrives after the send', async () => {
    let resolveList!: (result: { success: true; data: InsightsSessionSummary[] }) => void;
    (window as unknown as { API: Partial<API> }).API = {
      sendInsightsMessage,
      switchInsightsSession,
      newInsightsSession: vi.fn().mockResolvedValue({ success: true, data: makeSession('s-a', 'First Chat') }),
      listInsightsSessions: vi.fn().mockReturnValue(new Promise((resolve) => { resolveList = resolve; })),
    };

    const pending = newSession('proj-1');
    await vi.waitFor(() => expect(useInsightsStore.getState().session?.id).toBe('s-a'));
    sendMessage('proj-1', 'Hello');
    resolveList({ success: true, data: [makeSummary('s-a', 'First Chat', 0)] });
    await pending;

    switchInsightsSession.mockResolvedValue({ success: true, data: makeSession('s-b', 'Second Chat') });
    await switchSession('proj-1', 's-b');
    const state = useInsightsStore.getState();
    render(<ChatHistorySidebar
      sessions={state.sessions}
      currentSessionId={state.session?.id ?? null}
      isLoading={false}
      onNewSession={vi.fn()}
      onSelectSession={vi.fn()}
      onDeleteSession={vi.fn()}
      onRenameSession={vi.fn()}
    />);
    expect(screen.getByText('First Chat')).toBeInTheDocument();
    expect(useInsightsStore.getState().sessions.find((s) => s.id === 's-a')?.messageCount).toBe(1);
  });
});
