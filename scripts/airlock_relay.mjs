/** Genuine NVIDIA NeMo Relay 0.11.0 native managed-call boundary.
 * No synthetic ATOF writers. All scope/tool/LLM records come from the SDK.
 */
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { mkdir, writeFile, readFile, chmod } from 'node:fs/promises';
const require = createRequire(import.meta.url);

export async function withNativeRelay(outputDir, operation) {
  const modulePath = process.env.AIRLOCK_RELAY_MODULE || join(homedir(), '.local/share/locki-airlock/relay-runtime/node_modules/nemo-relay-node');
  const relay = require(modulePath);
  const plugin = require(join(modulePath, 'plugin'));
  const version = require(join(modulePath, 'package.json')).version;
  if (version !== '0.11.0') throw new Error(`Unvalidated native Relay version ${version}; expected 0.11.0`);
  const directory = resolve(outputDir);
  await mkdir(directory, { recursive: true, mode: 0o700 });
  await chmod(directory, 0o700);
  const config = join(directory, 'plugins.toml');
  const eventsPath = join(directory, 'events.jsonl');
  await writeFile(config, `version = 1\n[policy]\nunknown_component = "error"\nunknown_field = "error"\nunsupported_value = "error"\n[[components]]\nkind = "observability"\nenabled = true\n[components.config]\nversion = 4\nenable_full_payloads = false\n[components.config.atof]\nenabled = true\n[[components.config.atof.sinks]]\ntype = "file"\noutput_directory = ${JSON.stringify(directory)}\nfilename = "events.jsonl"\nmode = "overwrite"\n`, { mode: 0o600 });
  const activation = await plugin.initialize(plugin.defaultConfig(), config);
  let result;
  try {
    result = await relay.withScope('airlock-managed-investigation', relay.ScopeType.Agent, async handle => {
      // Sanitizers redact emitted copies only, never callback input/output.
      relay.registerToolSanitizeRequestGuardrail('airlock-redact-tool-input', 100, () => ({ redacted: true }));
      relay.registerToolSanitizeResponseGuardrail('airlock-redact-tool-output', 100, () => ({ redacted: true }));
      relay.registerLlmSanitizeRequestGuardrail('airlock-redact-llm-input', 100, () => ({ headers: {}, content: { redacted: true } }));
      relay.registerLlmSanitizeResponseGuardrail('airlock-redact-llm-output', 100, () => ({ redacted: true }));
      const context = {
        traceId: handle.uuid, version,
        tool: async (name, args, validate, callback) => {
          const guardName = 'airlock-allowlist-and-authorization';
          relay.registerToolConditionalExecutionGuardrail(guardName, 100, async (toolName, actualArgs) => {
            if (toolName !== name) return 'Unexpected tool substitution';
            try { await validate(actualArgs); return null; }
            catch (error) { return String(error.message || error).slice(0, 250); }
          });
          try {
            return (await relay.toolCallExecuteAsync(name, args, async actualArgs => ({ result: await callback(actualArgs) }), handle)).result;
          } finally { relay.deregisterToolConditionalExecutionGuardrail(guardName); }
        },
        llm: async (model, request, callback) => relay.llmCallExecuteAsync('ollama-loopback', new relay.LlmRequest({}, request), callback, handle, null, null, { transport: 'local-python-ollama', inference_claim: 'actual-request-only' }, model),
      };
      return await operation(context);
    });
  } finally {
    for (const [method, name] of [
      ['deregisterToolSanitizeRequestGuardrail','airlock-redact-tool-input'],
      ['deregisterToolSanitizeResponseGuardrail','airlock-redact-tool-output'],
      ['deregisterLlmSanitizeRequestGuardrail','airlock-redact-llm-input'],
      ['deregisterLlmSanitizeResponseGuardrail','airlock-redact-llm-output'],
    ]) relay[method](name);
    try { await relay.flushSubscribers(); } finally { await activation.close(); }
    await chmod(eventsPath, 0o600).catch(() => {});
  }
  const events = (await readFile(eventsPath, 'utf8')).trim().split(/\r?\n/).filter(Boolean).map(line => JSON.parse(line));
  if (!events.length || events.some(e => e.atof_version !== '0.1')) throw new Error('Native Relay did not produce valid ATOF 0.1 evidence');
  const starts = new Map(events.filter(e => e.kind === 'scope' && e.scope_category === 'start').map(e => [e.uuid, e]));
  const paired = events.filter(e => e.kind === 'scope' && e.scope_category === 'end' && starts.has(e.uuid) && ['name','category','parent_uuid'].every(k => e[k] === starts.get(e.uuid)[k]));
  if (!paired.some(e => e.category === 'agent')) throw new Error('Native Relay agent scope did not close');
  return { result, native_relay: { version, events_path: eventsPath, event_count: events.length, paired_categories: [...new Set(paired.map(e => e.category))] } };
}
