/** Minimal authenticated SpacetimeDB HTTP transport. Never logs credentials. */
export class AirlockHttpError extends Error {
  constructor(operation, status, detail) {
    super(`${operation} failed (${status}): ${detail.slice(0, 1500)}`);
    this.status = status;
  }
}

export function airlockEndpoint(value = process.env.AIRLOCK_SPACETIME_URI || 'http://127.0.0.1:3000') {
  const uri = new URL(value.replace(/^ws:/, 'http:').replace(/^wss:/, 'https:'));
  if (!['http:', 'https:'].includes(uri.protocol)) throw new Error('AIRLOCK_SPACETIME_URI must use HTTP(S) or WS(S)');
  return uri.origin;
}

export class AirlockHttp {
  /** @param {{uri?: string, database?: string, token?: string}} [options] */
  constructor({ uri, database = process.env.AIRLOCK_DATABASE || 'locki-airlock-sid', token } = {}) {
    this.uri = airlockEndpoint(uri);
    this.database = database;
    this.token = token;
  }

  async request(path, body, contentType = 'application/json') {
    const response = await fetch(`${this.uri}${path}`, {
      method: body === undefined ? 'GET' : 'POST',
      headers: {
        ...(this.token ? { Authorization: `Bearer ${this.token}` } : {}),
        ...(body === undefined ? {} : { 'Content-Type': contentType }),
      },
      body,
      signal: AbortSignal.timeout(15_000),
    });
    const detail = await response.text();
    if (!response.ok) throw new AirlockHttpError(path.split('/').at(-1), response.status, detail);
    if (!detail) return undefined;
    try { return JSON.parse(detail); } catch { return detail; }
  }

  async newIdentity() {
    return this.request('/v1/identity', '');
  }

  async call(reducer, args) {
    if (!Array.isArray(args)) throw new TypeError('Reducer arguments must be a positional array');
    return this.request(`/v1/database/${encodeURIComponent(this.database)}/call/${encodeURIComponent(reducer)}?confirmed=true`, JSON.stringify(args));
  }

  async sql(query) {
    return this.request(`/v1/database/${encodeURIComponent(this.database)}/sql`, query, 'text/plain');
  }

  async description() {
    return this.request(`/v1/database/${encodeURIComponent(this.database)}`);
  }
}
