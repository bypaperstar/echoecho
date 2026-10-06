'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const api = window.ctl;
  let status = {};
  let refreshing = false;
  let busy = false;
  let settingsDirty = false;
  let settingsFromVoice = false;
  let tasksSignature = '';
  let refreshFailures = 0;
  const report = (event, fields) => {
    try { window.echoDiagnostics?.report(event, fields); } catch { /* optional */ }
  };
  const note = (text, error = false) => {
    $('note').textContent = text;
    $('note').classList.toggle('error', error);
  };
  const cv = $('icon');
  const ctx = cv.getContext('2d');
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  function draw(now = 0) {
    const size = cv.width, center = size / 2;
    ctx.clearRect(0, 0, size, size);
    ctx.beginPath();
    for (let a = 0; a <= Math.PI * 2 + 0.08; a += 0.08) {
      const r = size * .34 * (1 + Math.sin(a * 3 + now / 1300) * .07);
      const x = center + Math.cos(a) * r, y = center + Math.sin(a) * r;
      if (a === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.closePath();
    const gradient = ctx.createRadialGradient(center - 12, center - 18, 2, center, center, 45);
    gradient.addColorStop(0, '#d5f6e5'); gradient.addColorStop(.22, '#93cbb1'); gradient.addColorStop(1, '#20372a');
    ctx.fillStyle = gradient; ctx.fill();
    if (!reduceMotion.matches && !document.hidden) requestAnimationFrame(draw);
  }
  draw();
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { draw(); refresh(); } });

  function setDevices(id, devices, input, selected) {
    const select = $(id);
    const names = [...new Set(devices.filter((d) => input ? d.input : d.output).map((d) => d.name))];
    if (selected && !names.includes(selected)) names.unshift(selected);
    const signature = JSON.stringify(names);
    if (select.dataset.devices !== signature) {
      select.replaceChildren();
      const add = (value, label) => { const option = document.createElement('option'); option.value = value; option.textContent = label; select.append(option); };
      add('', 'System default');
      names.forEach((name) => add(name, name));
      select.dataset.devices = signature;
    }
    select.value = selected || '';
  }

  function renderTasks(tasks) {
    const running = tasks.filter((t) => ['queued', 'running'].includes(t.status));
    $('task-count').textContent = running.length ? `${running.length} active ${running.length === 1 ? 'task' : 'tasks'}` : 'No active tasks';
    const shown = [...running, ...tasks.filter((t) => !['queued', 'running'].includes(t.status)).reverse().slice(0, 3)];
    const signature = JSON.stringify(shown);
    if (signature === tasksSignature) return;
    tasksSignature = signature;
    $('tasks').replaceChildren();
    if (!shown.length) {
      const empty = document.createElement('p'); empty.className = 'empty'; empty.textContent = 'Ask for something out loud or type it here. Progress will appear as it happens.'; $('tasks').append(empty);
    }
    for (const task of shown) {
      const row = document.createElement('div'); row.className = `task ${task.status}`;
      const copy = document.createElement('div'); copy.className = 'task-copy';
      const state = document.createElement('div'); state.className = 'task-state'; state.textContent = ({ queued: 'Queued', running: 'Working', done: 'Completed', error: 'Needs attention', cancelled: 'Canceled' })[task.status] || task.status;
      const title = document.createElement('div'); title.className = 'task-title'; title.textContent = task.title;
      const detail = document.createElement('div'); detail.className = 'task-detail'; detail.textContent = (task.status === 'running' ? task.progress || 'Starting the agent…' : task.say || '') .slice(0, 500);
      copy.append(state, title, detail); row.append(copy);
      if (['queued', 'running'].includes(task.status)) {
        const cancel = document.createElement('button'); cancel.textContent = 'Cancel'; cancel.setAttribute('aria-label', `Cancel ${task.title}`);
        cancel.addEventListener('click', () => perform(() => api.command({ action: 'cancel', task_id: task.id }), 'Canceling task…', 'Task canceled. Partial work stays in your workspace.'));
        row.append(cancel);
      }
      $('tasks').append(row);
    }
  }

  function render(st) {
    status = st;
    const voice = st.voice || {};
    const view = window.echoControlState.voiceState(st);
    $('voice-heading').textContent = view.title;
    $('voice-detail').textContent = view.detail;
    $('connection').textContent = view.badge;
    $('connection').classList.toggle('on', view.healthy);
    $('voice-dot').classList.toggle('on', view.healthy);
    $('input-name').textContent = voice.inputDevice || 'Microphone unavailable';
    $('input-meter').value = Number(voice.inputLevel) || 0;
    $('model-label').textContent = voice.model ? `${voice.model} · ${voice.outputDevice || 'system audio'}` : 'Voice connects when you start a conversation';
    $('notice').hidden = !voice.error && !st.startupError;
    $('notice').textContent = voice.error || st.startupError || '';
    $('vm-state').textContent = st.vm === null ? 'Checking VM…' : st.vm ? 'Shared Mac is running' : 'VM opens when you need it';
    $('b-talk').textContent = voice.session === 'ACTIVE' ? 'End conversation' : st.viewer ? 'Talk now' : 'Start listening';
    $('b-talk').disabled = busy || voice.phase === 'connecting' || voice.session === 'ENDING';
    $('b-pause').textContent = voice.phase === 'paused' ? 'Resume listening' : 'Pause listening';
    $('b-pause').disabled = busy || !st.viewer || voice.phase === 'connecting' || voice.session === 'ENDING';
    $('b-send').disabled = busy || !st.viewer;
    $('b-daemon').textContent = st.viewer ? 'Restart voice' : 'Start voice';
    $('version').textContent = `v${st.version || '?'}`;
    $('version').title = `${st.sha || ''}${st.builtAt ? ` · built ${new Date(st.builtAt).toLocaleString()}` : ''}`;
    $('login').checked = !!st.loginItem;
    renderTasks(voice.tasks || []);
    if (!settingsDirty) {
      const prefs = st.preferences || {};
      if (!settingsFromVoice || voice.configuredVoiceModel) $('voice-model').value = voice.configuredVoiceModel || prefs.voiceModel || 'gpt-live-1';
      setDevices('input-device', voice.devices || [], true, voice.configuredInputDevice ?? prefs.inputDevice);
      setDevices('output-device', voice.devices || [], false, voice.configuredOutputDevice ?? prefs.outputDevice);
      $('record').checked = voice.recordSessions ?? prefs.recordSessions ?? true;
      settingsFromVoice = !!voice.configuredVoiceModel;
    }
  }

  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      const next = await api.status();
      refreshFailures = 0;
      render(next);
    } catch (err) {
      refreshFailures++;
      if (refreshFailures === 1) report('control.refresh_failed', { message: err.message });
      render({ ...status, viewer: false, voice: null, startupError: 'The app could not refresh its status. Try restarting voice.' });
    } finally { refreshing = false; }
  }

  async function perform(fn, pending = '', success = '') {
    if (busy) return;
    busy = true;
    document.querySelectorAll('main button:not(#b-quit)').forEach((b) => { b.disabled = true; });
    note(pending);
    try {
      const result = await fn();
      if (result?.ok === false || result?.error) throw new Error(result.output || result.error || 'The action did not finish. Try again.');
      note(success);
      return result;
    } catch (err) {
      note((err.message || 'The action failed. Try again.').slice(0, 800), true);
      report('control.action_failed', { message: err.message });
    } finally {
      busy = false;
      document.querySelectorAll('main button').forEach((b) => { b.disabled = false; });
      render(status); await refresh();
    }
  }

  const action = (id, name, pending, success = '') => $(id).addEventListener('click', () => perform(() => api.action(name), pending, success));
  action('b-open-vm', 'vm-open', 'Opening your shared Mac. The first boot can take a moment.', 'Shared VM opened. You can move or minimize its window.');
  action('b-workspace', 'workspace', 'Opening your workspace…');
  action('b-livewriter', 'live-writer', 'Opening Live Writer…');
  action('b-summon', 'summon', '', 'Press Escape or ⌘⇧E to hide the orb.');
  action('b-daemon', 'daemon-restart', 'Restarting voice…', 'Voice restarted.');
  action('b-mic-settings', 'microphone-settings', 'Opening microphone permissions…');
  action('b-network-settings', 'network-settings', 'Opening Local Network permissions…');
  $('b-talk').addEventListener('click', () => perform(() => !status.viewer ? api.action('daemon-start') : api.command({ action: status.voice?.session === 'ACTIVE' ? 'end' : 'wake' }), !status.viewer ? 'Starting voice…' : 'Opening conversation…'));
  $('b-pause').addEventListener('click', () => perform(() => api.command({ action: status.voice?.phase === 'paused' ? 'resume' : 'pause' }), 'Updating listening…'));
  $('task-form').addEventListener('submit', (event) => {
    event.preventDefault(); const text = $('task-input').value.trim();
    if (!text) return;
    perform(async () => { const result = await api.command({ action: 'submit', text }); if (result.ok) $('task-input').value = ''; return result; }, 'Starting your task…', 'Task queued. You can keep talking while it works.');
  });
  ['voice-model', 'input-device', 'output-device', 'record'].forEach((id) => $(id).addEventListener('change', () => { settingsDirty = true; }));
  $('b-save').addEventListener('click', () => perform(async () => {
    await api.preferences({ voiceModel: $('voice-model').value, inputDevice: $('input-device').value, outputDevice: $('output-device').value, recordSessions: $('record').checked });
    if (status.viewer) await api.command({ action: 'configure' });
    settingsDirty = false;
    return { ok: true };
  }, 'Saving voice settings…', 'Saved. During a conversation, model and device changes apply to the next one.'));
  $('b-reset').addEventListener('click', () => {
    if (confirm('Reset the shared VM? This replaces its disk with a fresh copy and removes apps and files stored only inside the VM. Shared workspace files stay on your Mac.')) perform(() => api.action('vm-reset'), 'Resetting the shared VM…', 'Shared VM reset.');
  });
  action('b-update', 'update', 'Updating. Echoecho will reopen when the new build is ready.');
  $('b-quit').addEventListener('click', () => api.action('quit-app').catch((err) => note(err.message, true)));
  $('login').addEventListener('change', async (event) => {
    const enabled = event.target.checked;
    try { await api.setLoginItem(enabled); } catch (err) { event.target.checked = !enabled; note('Could not change start-at-login. Try again.', true); }
  });
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 750);
})();
