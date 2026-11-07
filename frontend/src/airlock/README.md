# AIRLOCK control room

The default product surface is `AirlockApp`. `?product=legacy` preserves the existing focus/voice console. AIRLOCK never calls the legacy robot arming, movement, motor-power or autonomy endpoints.

The UI connects directly through the generated SpacetimeDB SDK in `../airlock-bindings`. It subscribes only to the fourteen caller-scoped `my_*` views. Security changes await committed reducers; disconnected controls are disabled. The server enforces roles regardless of UI controls.

## Local setup

Run `scripts/airlock_dev.sh` from the repository root. Defaults are `http://127.0.0.1:3000`, database `locki-airlock`. The Connection button can select another server/database, including the host Mac's reachable LAN address. Build-time defaults accept `VITE_SPACETIMEDB_URI` and `VITE_SPACETIMEDB_DATABASE`.

1. Create a workstation. This connection identity becomes its owner.
2. Open `?client=reviewer` in a second tab, or use a separate browser on another Mac. Copy its **public identity** from Connection. In the owner console, choose Enroll identity and grant reviewer access.
3. Arm a synthetic session for a safe DB-backed rehearsal. Expand Demo fixtures and send the labeled person/USB scenario. The second enrolled console receives the same incident through its subscription.
4. Acknowledge or request information as an owner/reviewer. Resolution requires an owner and a verification note. Owner disarm is separate from incident resolution.
5. For hardware, enroll distinct Mac, Jetson and service identities. Enter the observed Mac USB/app baseline when arming a live session. This console does not scan USB devices or infer a baseline. Automatic lock requests require explicit policy consent; only an enrolled Mac adapter can execute/report them.

Profiles namespace persisted browser credentials. A profile name does not grant any role. The token namespace includes server, database and profile, and tokens never appear in URLs or the UI. The brand link preserves the active profile. Share public identities only.

## Provenance and health

Synthetic observations, incidents and sessions carry amber labels. Owner fixture health is excluded from physical sensor availability. A generic publisher heartbeat is not treated as model inference or per-sensor health. Missing/stale coverage remains visibly degraded. Model runs and tool steps are shown only when an enrolled adapter reports them; an empty investigation is not a successful model demonstration.

The local voice WebSocket is an auxiliary transcript/voice-control source. It does not own AIRLOCK session or incident state. Existing VoiceStudio remains available when that local service is connected. Voice controls do not authorize security reducers.

The protected gate displays exact arguments, immutable action digest, expiry, approval and reported callback outcome. Owner-proposed requests are review-only. To execute a harmless callback with `scripts/airlock_gateway.ts`, the same enrolled service must propose the action and consume a separate human's approval. The managed runner supports `airlock:read-only` and `fixture:read-only`; the UI defaults to `fixture_word_count` with harmless text. The UI does not claim native NeMo Relay, Code Quest or model integration by displaying an approval row.
