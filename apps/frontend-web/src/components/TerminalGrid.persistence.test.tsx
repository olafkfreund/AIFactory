/** @vitest-environment jsdom */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { render, screen, act } from '@testing-library/react';
import { useEffect, useState } from 'react';
import { TerminalGrid } from './TerminalGrid';
import { useTerminalStore } from '../stores/terminal-store';

const output = vi.hoisted(() => new EventTarget());
vi.mock('./Terminal', () => ({
  Terminal: ({ id }: { id: string }) => {
    const [text, setText] = useState('');
    useEffect(() => {
      const listener = (event: Event) => { setText((value) => value + (event as CustomEvent<{ id: string; text: string }>).detail.text); };
      output.addEventListener(id, listener);
      return () => output.removeEventListener(id, listener);
    }, [id]);
    return <output data-testid={`terminal-${id}`}>{text}</output>;
  },
}));
vi.mock('./FileExplorerPanel', () => ({ FileExplorerPanel: () => null }));

beforeEach(() => {
  useTerminalStore.setState({ terminals: [
    { id: 'alpha', title: 'Alpha', cwd: '/alpha', projectPath: '/alpha', status: 'running', createdAt: new Date(), isClaudeMode: false },
    { id: 'beta', title: 'Beta', cwd: '/beta', projectPath: '/beta', status: 'running', createdAt: new Date(), isClaudeMode: false },
  ], activeTerminalId: 'alpha' });
  window.API = new Proxy({ getTerminalSessionDates: () => Promise.resolve({ success: true, data: [] }) }, {
    get: (target, key: string) => (target as Record<string, unknown>)[key] ?? (() => Promise.resolve({ success: true })),
  }) as unknown as typeof window.API;
});

describe('TerminalGrid project switches', () => {
  it('keeps each child mounted and receiving output while another project is visible', () => {
    const { rerender } = render(<TerminalGrid projectPath="/alpha" isActive />);
    act(() => output.dispatchEvent(new CustomEvent('alpha', { detail: { id: 'alpha', text: 'before ' } })));
    rerender(<TerminalGrid projectPath="/beta" isActive />);
    act(() => output.dispatchEvent(new CustomEvent('alpha', { detail: { id: 'alpha', text: 'during ' } })));
    rerender(<TerminalGrid projectPath="/alpha" isActive />);
    expect(screen.getByTestId('terminal-alpha')).toHaveTextContent('before during');
  });
});
