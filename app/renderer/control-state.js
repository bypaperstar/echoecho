'use strict';
(function (root) {
  function voiceState(status) {
    const voice = status.voice;
    if (status.microphonePermission === 'denied' || status.microphonePermission === 'restricted') {
      return { title: 'Microphone access is off', detail: 'Allow echoecho in System Settings → Privacy & Security → Microphone.', badge: 'Needs permission', healthy: false };
    }
    if (!status.viewer || !voice) {
      return { title: 'Voice is offline', detail: status.startupError || 'Start listening to use the wake word. Your workspace is still on this Mac.', badge: 'Offline', healthy: false };
    }
    if (voice.phase === 'paused') return { title: 'Listening is paused', detail: 'Your microphone is closed. Background tasks keep working.', badge: 'Paused', healthy: false };
    if (voice.phase === 'connecting') return { title: 'Opening a conversation…', detail: 'Connecting the voice model. You can speak after the chime.', badge: 'Connecting', healthy: false };
    if (!voice.captureActive || !Number.isFinite(voice.captureAge) || voice.captureAge > 3) return { title: 'Waiting for microphone audio', detail: 'Check your selected microphone and its permission. Try restarting voice.', badge: 'Check microphone', healthy: false };
    if (voice.session === 'ENDING') return { title: 'Finishing the conversation', detail: 'The wake word will be ready again in a moment.', badge: 'Finishing', healthy: true };
    if (voice.session === 'ACTIVE') return { title: 'I’m listening', detail: 'Talk naturally. You can interrupt or say “that’s it” to finish.', badge: 'In conversation', healthy: true };
    return { title: 'Ready when you are', detail: 'Say “echo echo,” wait for the chime, then tell me what you need.', badge: 'Listening', healthy: true };
  }
  const api = { voiceState };
  if (typeof module !== 'undefined') module.exports = api;
  else root.echoControlState = api;
})(typeof window === 'undefined' ? globalThis : window);
