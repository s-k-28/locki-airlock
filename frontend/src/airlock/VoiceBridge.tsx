import { useEffect, useRef, useState } from "react";
import { AudioLines, Mic, MicOff, Settings2, Volume2, VolumeX } from "lucide-react";
import { useLockedIn } from "../hooks/useLockedIn";
import { api } from "../services/api";
import { VoiceStudio } from "../components/VoiceStudio";
import { Badge, Button, Empty, Panel } from "./ui";

export function VoiceBridge() {
  const live = useLockedIn();
  const [studio, setStudio] = useState(false);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const voice = live.snapshot?.voice;
  const holding = useRef(false);
  const [talking, setTalking] = useState(false);
  const stopTalking = () => {
    if (holding.current) live.send({ type: "ptt", action: "stop" });
    holding.current = false;
    setTalking(false);
  };
  const startTalking = () => {
    if (!voice?.input.running || live.link !== "open" || holding.current) return;
    holding.current = true;
    setTalking(true);
    live.send({ type: "ptt", action: "start" });
  };
  useEffect(() => {
    if (!voice?.input.running || live.link !== "open") stopTalking();
  }, [voice?.input.running, live.link]);
  useEffect(() => () => {
    if (holding.current) live.send({ type: "ptt", action: "stop" });
  }, [live.send]);
  const lines = live.events.filter(e => ["voice.transcribed", "voice.response_generated"].includes(e.event_type)).slice(-4);
  const act = async (action: Promise<unknown>) => {
    setPending(true);
    setError("");
    try {
      const result = await action as { ok?: boolean; message?: string } | undefined;
      if (result?.ok === false) setError(result.message || "The local voice service did not complete this action.");
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setPending(false); }
  };
  return <><Panel title="LOCKI voice" icon={<AudioLines size={16} />} aside={<Badge tone={voice && live.link === "open" ? "cyan" : "quiet"} dot>{voice && live.link === "open" ? "Local service" : "Unavailable"}</Badge>}>
    {voice && live.link === "open" ? <><div className="al-voice-state"><span className="al-voice-wave"><AudioLines size={32} /></span><div><strong>{voice.state === "off" ? "Microphone is off" : voice.state.charAt(0).toUpperCase() + voice.state.slice(1)}</strong><small>{voice.input.running ? "Microphone enabled by operator" : "Microphone consent is off"}</small></div><button aria-label="Open existing voice studio" title="Voice studio" className="al-icon-button" onClick={() => setStudio(true)}><Settings2 size={15} /></button></div><div className="al-voice-transcript">{lines.length ? lines.map(e => <div key={e.event_id}><span>{e.event_type === "voice.transcribed" ? "You" : "LOCKI"}</span><p>{e.payload.text}</p></div>) : <p className="al-muted">No transcript received from the local voice pipeline.</p>}</div><div className="al-voice-controls"><Button disabled={pending} onClick={() => act(voice.input.running ? api.voiceStop() : api.voiceStart())}>{voice.input.running ? <MicOff size={13} /> : <Mic size={13} />}{voice.input.running ? "Stop mic" : "Enable mic"}</Button><Button kind="ghost" disabled={pending} onClick={() => act(api.patchSettings({ voice: { muted: !voice.tts.muted } }))}>{voice.tts.muted ? <VolumeX size={13} /> : <Volume2 size={13} />}{voice.tts.muted ? "Unmute" : "Mute speech"}</Button></div></> : <Empty icon={<AudioLines size={25} />} title="Voice adapter disconnected">The existing local voice service is offline. Security state continues through SpacetimeDB.</Empty>}
    {voice?.input.running && live.link === "open" && <Button
      aria-pressed={talking} kind={talking ? "warning" : "primary"}
      onPointerDown={e => { if (e.button !== 0) return; e.preventDefault(); e.currentTarget.setPointerCapture(e.pointerId); startTalking(); }}
      onPointerUp={stopTalking} onPointerCancel={stopTalking} onLostPointerCapture={stopTalking}
      onKeyDown={e => { if ((e.key === " " || e.key === "Enter") && !e.repeat) { e.preventDefault(); startTalking(); } }}
      onKeyUp={e => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); stopTalking(); } }}
      onBlur={stopTalking}
    ><Mic size={13} />{talking ? "Release to send" : "Hold to talk"}</Button>}
    {error && <p role="alert" className="al-inline-error">{error}</p>}<footer className="al-panel-footer al-muted">Speech and mute controls do not arm or disarm the workstation.</footer>
  </Panel>{studio && live.snapshot && <VoiceStudio open={studio} onClose={() => setStudio(false)} snap={live.snapshot} />}</>;
}
