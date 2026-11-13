import packageJson from '../../package.json';

declare const __KNOWE_BUILD_COMMIT__: string;

/** The displayed revision belongs to the built frontend, never the host's current checkout. */
export function appVersionInfo(): { version: string; commit: string; shortCommit: string } {
  const metadata: { version: string; productVersion?: string } = packageJson;
  const version = window.knowe?.version || metadata.productVersion || metadata.version;
  const rawCommit = typeof __KNOWE_BUILD_COMMIT__ === 'string' ? __KNOWE_BUILD_COMMIT__.trim() : '';
  const commit = /^[a-f\d]{7,40}(?:-dirty)?$/i.test(rawCommit) ? rawCommit : '';
  return { version, commit, shortCommit: commit ? commit.slice(0, 7) + (commit.endsWith('-dirty') ? '*' : '') : '' };
}
