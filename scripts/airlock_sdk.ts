import { DbConnection } from '../frontend/src/airlock-bindings/index';

export const AIRLOCK_VIEWS = [
  'my_workstations', 'my_memberships', 'my_devices', 'my_sessions',
  'my_device_events', 'my_incidents', 'my_device_health', 'my_lock_requests',
  'my_protected_actions', 'my_approvals', 'my_agent_runs', 'my_agent_steps',
  'my_acknowledgements', 'my_audit_events', 'my_baseline_snapshots',
];

export type AirlockClient = {
  conn: DbConnection;
  token: string;
  identity: string;
};

export async function connectAirlock(token?: string): Promise<AirlockClient> {
  let connection: DbConnection | undefined;
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => { connection?.disconnect(); reject(new Error('SpacetimeDB connection/subscription timed out')); }, 15_000);
    const fail = (error: unknown) => { clearTimeout(timeout); connection?.disconnect(); reject(error instanceof Error ? error : new Error(String(error))); };
    connection = DbConnection.builder()
      .withUri(process.env.AIRLOCK_SPACETIME_URI || 'http://127.0.0.1:3000')
      .withDatabaseName(process.env.AIRLOCK_DATABASE || 'locki-airlock-sid')
      .withToken(token)
      .onConnect((conn, identity, savedToken) => {
        conn.subscriptionBuilder()
          .onApplied(() => { clearTimeout(timeout); resolve({ conn, token: savedToken, identity: identity.toHexString() }); })
          .onError((ctx: any) => fail(ctx.event ?? 'SpacetimeDB subscription error'))
          .subscribe(AIRLOCK_VIEWS.map(name => `SELECT * FROM ${name}`));
      })
      .onConnectError((_ctx, error) => fail(error))
      .build();
  });
}

export function rows(client: AirlockClient, accessor: string, workstationId?: string): any[] {
  const table = (client.conn.db as any)[accessor];
  if (!table?.iter) throw new Error(`Generated binding has no ${accessor} accessor; regenerate it for the deployed module`);
  const result = [...table.iter()];
  return workstationId ? result.filter(row => row.workstationId === workstationId || row.id === workstationId) : result;
}

export function call(client: AirlockClient, reducer: string, args: Record<string, unknown>): Promise<void> {
  const fn = (client.conn.reducers as any)[reducer];
  if (typeof fn !== 'function') throw new Error(`Generated binding has no reducer ${reducer}`);
  return fn(args);
}

export async function until<T>(getValue: () => T | undefined | false | null, label: string, timeoutMs = 8_000): Promise<T> {
  const deadline = performance.now() + timeoutMs;
  do {
    const value = getValue();
    if (value) return value as T;
    await new Promise(resolve => setTimeout(resolve, 10));
  } while (performance.now() < deadline);
  throw new Error(`Timed out waiting for ${label}`);
}

export function closeAirlock(clients: AirlockClient[]) {
  for (const client of clients) client.conn.disconnect();
}
