'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const { randomUUID } = require('crypto');
const MODELS = ['gpt-live-1', 'gpt-realtime-2.1', 'gpt-realtime-2.1-mini'];
const DEFAULTS = { voiceModel: 'gpt-live-1', inputDevice: '', outputDevice: '', recordSessions: true };
const preferencePath = () => process.env.ECHOECHO_PREFERENCES_FILE || path.join(os.homedir(), '.echoecho', 'preferences.json');
function validate(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new Error('Invalid preferences');
  for (const [key, value] of Object.entries(input)) {
    if (!Object.hasOwn(DEFAULTS, key)) throw new Error('Unknown preference');
    if (key === 'recordSessions') {
      if (typeof value !== 'boolean') throw new Error('Recording must be on or off');
    } else if (typeof value !== 'string' || value.length > 200 || /[\x00-\x1f]/.test(value)) {
      throw new Error('Invalid preference value');
    }
    if (key === 'voiceModel' && !MODELS.includes(value)) throw new Error('Unsupported voice model');
  }
  return { ...input };
}
function read() {
  try { return validate(JSON.parse(fs.readFileSync(preferencePath(), 'utf8'))); }
  catch { return {}; }
}
function write(input) {
  const data = { ...read(), ...validate(input) };
  const file = preferencePath();
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const temp = `${file}.${randomUUID()}.tmp`;
  try {
    fs.writeFileSync(temp, JSON.stringify(data, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
    fs.renameSync(temp, file);
  } finally {
    try { fs.unlinkSync(temp); } catch { /* renamed or not created */ }
  }
  return data;
}
module.exports = { MODELS, DEFAULTS, read, write, validate };
