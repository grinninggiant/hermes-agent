// Test-only esbuild replacement. Never imported by the production entry.
function blocked(_operation: () => unknown): void {}

export const startupEffects = {
  startBackend: <T>(_operation: () => Promise<T>): Promise<T> =>
    Promise.reject(new Error('Backend unavailable in isolated startup smoke')),
  checkForUpdates: <T>(_operation: () => Promise<T>): Promise<T> =>
    Promise.reject(new Error('Updates unavailable in isolated startup smoke')),
  reapBackends: async (_operation: () => Promise<void>): Promise<void> => {},
  stopBackend: blocked,
  registerProtocol: blocked,
  warmShell: blocked,
  recoverStartup: blocked,
  integrateDesktop: blocked
}
