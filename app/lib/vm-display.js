'use strict';

// Local VM discovery must work while the voice service is stopped or starting.
async function displayTarget({ override, viewerInfo, localInfo }) {
  if (override) return override;
  try {
    const info = await viewerInfo();
    if (info && typeof info.url === 'string' && info.url) return info.url;
  } catch { /* The VM does not depend on microphone startup. */ }
  const result = await localInfo();
  if (result.ok) {
    try {
      const info = JSON.parse(result.output);
      if (typeof info.url === 'string' && info.url) return info.url;
    } catch { /* Do not expose credentials or raw child output in errors. */ }
  }
  throw new Error('Our shared Mac is unavailable. Try opening it again.');
}

module.exports = { displayTarget };
