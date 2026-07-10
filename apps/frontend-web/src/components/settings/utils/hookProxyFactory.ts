import type { UseProjectSettingsReturn } from '../../project-settings/hooks/useProjectSettings';
import type { MutableRefObject } from 'react';

/**
 * Creates a proxy that always accesses the latest hook values via ref.
 * This prevents infinite loops caused by hook object recreation on each render.
 *
 * @param hookRef - Stable reference to the hook return value
 * @returns Proxy that provides access to the latest hook state
 */
export function createHookProxy(
  hookRef: MutableRefObject<UseProjectSettingsReturn>
): UseProjectSettingsReturn {
  return new Proxy(hookRef, {
    get: (target, prop) => (target.current as never)[prop as keyof UseProjectSettingsReturn],
  }) as unknown as UseProjectSettingsReturn;
}
