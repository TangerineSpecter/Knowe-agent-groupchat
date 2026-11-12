/** Build mode, not user-agent sniffing: desktop and web share the same business UI. */
export function isWebRuntime(): boolean {
  const environment = import.meta.env;
  return environment.MODE === 'web' && !window.knowe;
}

export const WEB_DIRECTORY_PENDING = 'web:choose-directory';
