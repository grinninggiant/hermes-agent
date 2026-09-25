/** External startup authority. Production always delegates; no runtime test switch.
 * An isolated smoke bundle replaces this module at build time only.
 */
function delegate<T>(operation: () => T): T {
  return operation()
}

export const startupEffects = {
  startBackend: <T>(operation: () => Promise<T>): Promise<T> => operation(),
  checkForUpdates: <T>(operation: () => Promise<T>): Promise<T> => operation(),
  reapBackends: (operation: () => Promise<void>): Promise<void> => operation(),
  stopBackend: delegate,
  registerProtocol: delegate,
  warmShell: delegate,
  recoverStartup: delegate,
  integrateDesktop: delegate
}
