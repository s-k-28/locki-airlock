/** SecurityEventV1 transport contract. Times are UTC Unix milliseconds. */
export type AirlockSource = 'mac' | 'jetson' | 'fixture' | 'service';
export type AirlockEventKind = 'usb_added' | 'usb_removed' | 'app_launched' | 'person_in_zone' | 'person_cleared' | 'camera_offline' | 'camera_online' | 'heartbeat';
export interface AirlockMetadata {
  deviceKey?: string;
  appId?: string;
  label?: string;
  detail?: string;
  captureMode?: string;
  bootId?: string;
}
export interface SecurityEventV1 {
  schema_version: 1;
  workstation_id: string;
  session_id: string;
  device_id: string;
  event_id: string;
  source: AirlockSource;
  kind: AirlockEventKind;
  observed_at: number;
  /** Positive safe-integer counter starting at 1; persist across sessions.
   * Server accepts a forward delta of at most 1,000,000 from its last value.
   * This is a counter, not an epoch timestamp.
   */
  seq: number;
  confidence?: number;
  metadata: AirlockMetadata;
  /** Mandatory provenance. A fixture must always use true. */
  simulated: boolean;
}
export interface IncidentUpdateV1 {
  schema_version: 1;
  workstation_id: string;
  session_id: string;
  incident_id: string;
  severity: 'medium' | 'high';
  state: 'open' | 'acknowledged' | 'reviewing' | 'resolved';
  evidence_ids: string[];
  action_required: boolean;
  simulated: boolean;
  updated_at: number;
}
export interface LockRequestV1 {
  schema_version: 1;
  workstation_id: string;
  session_id: string;
  incident_id: string;
  action_id: string;
  target_device_id: string;
  expiry: number;
  reason: string;
  owner_policy_hash: string;
  simulated: boolean;
}
export interface AgentStepV1 {
  schema_version: 1;
  workstation_id: string;
  incident_id: string;
  run_id: string;
  step_id: string;
  step: number;
  tool_name: string;
  status: 'running' | 'succeeded' | 'failed' | 'complete';
  evidence_id: string;
  duration_ms: number;
  input_type: string;
  args: Record<string, unknown>;
  result_summary: string;
  trace_id: string;
  model: string;
}
