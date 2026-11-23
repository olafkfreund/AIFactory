/** @vitest-environment jsdom */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { archiveTasks, persistTaskStatus, startTask, stopTask, useTaskStore } from './task-store';
import type { Task } from '../shared/types';

const task = (projectId: string, status: Task['status'] = 'backlog'): Task => ({
  id: `${projectId}:1`, specId: '1', projectId, title: projectId, description: '', status,
  subtasks: [], logs: [], createdAt: new Date(), updatedAt: new Date(),
});

beforeEach(() => {
  useTaskStore.setState({ tasks: [], error: null });
});

describe('shell task actions', () => {
  it('does not replace the current project after an archive from another project settles', async () => {
    let finish: (value: { success: boolean }) => void = () => undefined;
    const getTasks = vi.fn();
    window.API = { archiveTasks: () => new Promise((resolve) => { finish = resolve; }), getTasks } as unknown as typeof window.API;
    useTaskStore.setState({ tasks: [task('alpha')] });
    const pending = archiveTasks('alpha', ['alpha:1']);
    useTaskStore.setState({ tasks: [task('beta')] });
    finish({ success: true });
    await pending;
    expect(useTaskStore.getState().tasks.map((item) => item.projectId)).toEqual(['beta']);
    expect(getTasks).not.toHaveBeenCalled();
  });

  it('restores a rejected drop and an unsuccessful start status patch', async () => {
    window.API = { updateTaskStatus: vi.fn().mockResolvedValue({ success: false, error: 'Blocked' }) } as unknown as typeof window.API;
    useTaskStore.setState({ tasks: [task('alpha')] });
    expect(await persistTaskStatus('alpha:1', 'done')).toBe(false);
    expect(useTaskStore.getState().tasks[0].status).toBe('backlog');
    await expect(startTask('alpha:1')).rejects.toThrow('Blocked');
    expect(useTaskStore.getState().tasks[0].status).toBe('backlog');
  });

  it('waits for start and stop and restores a rejected stop', async () => {
    let finishStart: (value: { success: boolean }) => void = () => undefined;
    let finishStop: (value: { success: boolean; error?: string }) => void = () => undefined;
    window.API = {
      updateTaskStatus: vi.fn().mockResolvedValue({ success: true }),
      startTask: () => new Promise((resolve) => { finishStart = resolve; }),
      stopTask: () => new Promise((resolve) => { finishStop = resolve; }),
    } as unknown as typeof window.API;
    useTaskStore.setState({ tasks: [task('alpha')] });
    let started = false;
    const starting = startTask('alpha:1').then(() => { started = true; });
    await Promise.resolve();
    expect(started).toBe(false);
    finishStart({ success: true });
    await starting;
    const stopping = stopTask('alpha:1');
    expect(useTaskStore.getState().tasks[0].status).toBe('backlog');
    finishStop({ success: false, error: 'Cannot stop' });
    await expect(stopping).rejects.toThrow('Cannot stop');
    expect(useTaskStore.getState().tasks[0].status).toBe('in_progress');
  });
});
