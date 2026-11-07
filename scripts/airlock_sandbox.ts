/** Fixed trusted pytest exercise. Only the approved gateway may dispatch this tool. */
import { execFile } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const SANDBOX_SCOPE = 'airlock:sandbox-tests';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const python = resolve(root, 'backend/.venv/bin/python');
const worker = resolve(root, 'scripts/airlock_sandbox_worker.py');
export type SandboxArgs = { fixture: 'positive-sum-v1'; candidate: 'correct' | 'buggy' };

export function validateSandboxArgs(value: unknown): SandboxArgs {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Sandbox arguments must be an object');
  const args = value as Record<string, unknown>;
  if (Object.keys(args).length !== 2 || Object.keys(args).some(k => !['fixture', 'candidate'].includes(k)) ||
      args.fixture !== 'positive-sum-v1' || !['correct', 'buggy'].includes(args.candidate as string)) {
    throw new Error('Only fixture:positive-sum-v1 and candidate:correct|buggy are allowed; no code, paths or commands');
  }
  return { fixture: args.fixture, candidate: args.candidate as SandboxArgs['candidate'] };
}

export function runSandboxTests(value: unknown): Promise<Record<string, unknown>> {
  const args = validateSandboxArgs(value);
  return new Promise((resolveResult, reject) => {
    const child = execFile(python, ['-I', worker], {
      cwd: root, timeout: 15_000, killSignal: 'SIGKILL', maxBuffer: 16_384,
      env: { PATH: '/usr/bin:/bin', LANG: 'C.UTF-8' }, windowsHide: true,
    }, (error, stdout) => {
      if (error) { reject(new Error('Fixed sandbox test worker failed or timed out; no automatic retry')); return; }
      try {
        const result = JSON.parse(stdout);
        if (result.tool !== 'run_pytest_in_sandbox' || result.fixture !== args.fixture || result.candidate !== args.candidate ||
            !['passed', 'failed'].includes(result.test_outcome) || result.workspace_removed !== true ||
            !/^[a-f0-9]{64}$/.test(result.fixture_sha256)) throw new Error('Invalid sandbox test result');
        resolveResult(result);
      } catch { reject(new Error('Invalid bounded sandbox test worker response')); }
    });
    child.stdin?.end(JSON.stringify(args));
  });
}

export const sandboxTool = { validate: validateSandboxArgs, run: runSandboxTests };
