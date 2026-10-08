// Select playback before importing the renderer: baked mode never loads Orbit/WASM.
try {
  const response = await fetch('/api/config', { cache: 'no-store' });
  const config = response.ok ? await response.json() : null;
  if (config?.settings?.playback === 'prerendered' || new URLSearchParams(location.search).get('renderer') === 'prerendered') {
    const { startBakedDisplay } = await import('./prerendered-display.js');
    await startBakedDisplay(config);
  } else await import('./original.js');
} catch (error) {
  document.querySelector('#app').textContent = `Unable to open robot display: ${error.message}`;
}
