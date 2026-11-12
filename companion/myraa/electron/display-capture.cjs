'use strict';

const TRUSTED_ORIGINS = new Set([
  'http://127.0.0.1:3000',
  'http://localhost:3000',
]);

function isTrustedMainFrame(details = {}) {
  if (details.isMainFrame !== true) return false;
  const candidate = details.requestingUrl || details.securityOrigin || '';
  try {
    return TRUSTED_ORIGINS.has(new URL(candidate).origin);
  } catch {
    return false;
  }
}

function installDisplayCaptureHandler({ session, desktopCapturer, dialog, getMainWindow, logger = console }) {
  // Electron 43 checks display capture through its permission handlers before
  // invoking setDisplayMediaRequestHandler. Keep both checks local-only and
  // allow microphone audio for MYRAA's voice session, but not camera access.
  session.defaultSession.setPermissionCheckHandler((_webContents, permission, requestingOrigin, details = {}) => {
    if (permission === 'display-capture') {
      return isTrustedMainFrame({ ...details, requestingUrl: details.requestingUrl || requestingOrigin });
    }
    if (permission === 'media') {
      return isTrustedMainFrame({ ...details, requestingUrl: details.requestingUrl || requestingOrigin })
        && details.mediaType === 'audio';
    }
    return false;
  });

  session.defaultSession.setPermissionRequestHandler((_webContents, permission, callback, details = {}) => {
    const trusted = isTrustedMainFrame(details);
    if (permission === 'display-capture') {
      callback(trusted);
      return;
    }
    if (permission === 'media') {
      callback(trusted && Array.isArray(details.mediaTypes)
        && details.mediaTypes.includes('audio')
        && !details.mediaTypes.includes('video'));
      return;
    }
    callback(false);
  });

  session.defaultSession.setDisplayMediaRequestHandler((request, callback) => {
    const frame = request?.frame;
    const isTrustedFrame = Boolean(frame && frame === frame.top);
    if (!request?.userGesture || !request.videoRequested || !isTrustedFrame || !TRUSTED_ORIGINS.has(request.securityOrigin)) {
      callback(null);
      return;
    }

    desktopCapturer.getSources({
      types: ['screen'],
      thumbnailSize: { width: 0, height: 0 },
      fetchWindowIcons: false,
    }).then(async (sources) => {
      if (!sources.length) {
        callback(null);
        return;
      }

      if (sources.length === 1) {
        // The user explicitly pressed Share Screen; send only the display source.
        callback({ video: sources[0] });
        return;
      }

      const buttons = sources.map((source, index) => `Display ${index + 1}${source.name ? ` · ${source.name}` : ''}`);
      const cancelId = buttons.length;
      buttons.push('Cancel');
      const options = {
        type: 'question',
        title: 'Choose a display to share',
        message: 'Select which display MYRAA can see.',
        detail: 'Only the selected display is sent to your active MYRAA session. Stop sharing any time.',
        buttons,
        defaultId: 0,
        cancelId,
        noLink: true,
      };
      const window = getMainWindow?.();
      const choice = window
        ? await dialog.showMessageBox(window, options)
        : await dialog.showMessageBox(options);
      callback(choice.response >= 0 && choice.response < sources.length
        ? { video: sources[choice.response] }
        : null);
    }).catch((error) => {
      logger.warn(`[Display Capture] Screen source selection failed: ${error?.message || error}`);
      callback(null);
    });
  });
}

module.exports = { installDisplayCaptureHandler, isTrustedMainFrame, TRUSTED_ORIGINS };
