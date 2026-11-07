export type RelayContext = {
  traceId: string;
  version: string;
  tool<T>(name: string, args: unknown, validate: (args: any) => unknown | Promise<unknown>, callback: (args: any) => T | Promise<T>): Promise<T>;
  llm<T>(model: string, request: unknown, callback: () => T | Promise<T>): Promise<T>;
};
export function withNativeRelay<T>(outputDir: string, operation: (relay: RelayContext) => T | Promise<T>): Promise<{
  result: T;
  native_relay: { version: string; events_path: string; event_count: number; paired_categories: string[] };
}>;
