import { isWebRuntime } from './deployment';
import type { RuntimeEndpoints } from './bridge';

/** Browser-only fallback lives in one place; Electron supplies the authoritative values. */
export const DEFAULT_RUNTIME_ENDPOINTS: RuntimeEndpoints = Object.freeze({
  httpBase: 'http://127.0.0.1:8081',
  wsUrl: 'ws://127.0.0.1:8080',
});

export function runtimeEndpoints(): RuntimeEndpoints {
  if (isWebRuntime()) return {
    httpBase: `${window.location.origin}/api/runtime`,
    wsUrl: `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/api/runtime/ws`,
  };
  return window.knowe?.runtimeEndpoints ?? DEFAULT_RUNTIME_ENDPOINTS;
}

export function runtimeHttpBase(): string {
  return runtimeEndpoints().httpBase.replace(/\/$/, '');
}

export function runtimeWsUrl(): string {
  return runtimeEndpoints().wsUrl;
}
