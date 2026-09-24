# A01: real Electron and Python logout

Baseline: `d60e99f5c0d87f651239ca9840881cc8be1eddbc`.
Run on branch `acceptance/windows-real-boundary-d60e99f`, on Windows x64.
The harness refuses tracked product differences from that baseline.

## Run

Prepare the project's real Python dependencies in the repository `.venv`.
From `desktop`, run `npm ci` and `npm run build`. Ensure the locked Electron
binary exists at `desktop/node_modules/electron/dist/electron.exe`; if the
environment skipped binary installation, run its `install.js` normally.
From this directory run `npm ci`, then `npm test`.
Do not run a Vite server on ports 5173–5176 during this test.

Playwright launches the unchanged built main entrypoint and its real preload,
renderer, PythonBackend, and Python WebChannel. It uses a unique temporary
profile, HOME, COW_HOME, COW_DATA_DIR and workspace. The child environment is
allowlisted and contains no inherited provider credentials. All login and
logout actions use actual UI controls. Existing developer data is not used.

The old conversation is a deterministic fixture written through the existing
ConversationStore into real isolated SQLite. Its owner is the device that
actually authenticated with the temporary password. Only the non-secret owner
identifier is read by external main-process instrumentation; no credential is
exposed to the renderer or evidence. The fixture is loaded through the real
sessions/history paths and selected in the UI. It is not model-generated.

Normal logout must restore LoginGate, invalidate the active ID, hide previous
messages, report unauthenticated via real IPC `/auth/check`, and return 401 for
`/api/sessions`. Relogin must retain the fresh active draft without restoring
old messages. The same authenticated device may still access its own history.

The failure case rotates only the isolated password through the existing
protected `/config` API, then clicks UI Sign out. The real backend rejects
logout with HTTP 401. The harness checks the resulting page error, LoginGate,
session rotation, denied protected requests and a successful fresh login.
This covers backend rejection, not transport timeout or backend process loss.

There are no mock backends, IPC, auth, renderer stores or localStorage, and no
product test bypasses. Main bearer clearing is inferred only to the behavioral
extent that subsequent real requests have no old authenticated privilege;
the bearer itself is never exposed or logged. Multi-principal isolation, real
providers, NSIS, signing and customer acceptance remain outside this test.

Evidence is written to `acceptance/evidence/<run-id>` with manifest, results,
redacted logs, screenshots and SHA-256 sums. These generated directories are
ignored by Git. The manifest binds the baseline, current harness commit, its
script hash, actual build hashes and Electron executable hash. The source-mode
entrypoint may display the Electron runtime version; this is not NSIS evidence.
Success requires observed main/Python processes to exit and removes only this
run's runtime directory. Failure preserves runtime data for diagnosis; it must
never be committed. Evidence from failed attempts must not be called PASS.

Driver reference: <https://playwright.dev/docs/api/class-electron>.
