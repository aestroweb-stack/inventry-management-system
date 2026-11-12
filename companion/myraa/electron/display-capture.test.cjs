'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { installDisplayCaptureHandler } = require('./display-capture.cjs');

function makeHarness({ sources = [], choice = 0 } = {}) {
  let handler;
  let permissionCheck;
  let permissionRequest;
  let sourceReads = 0;
  let dialogCalls = 0;
  const session = {
    defaultSession: {
      setDisplayMediaRequestHandler(value) { handler = value; },
      setPermissionCheckHandler(value) { permissionCheck = value; },
      setPermissionRequestHandler(value) { permissionRequest = value; },
    },
  };
  const dialog = {
    async showMessageBox(...args) {
      dialogCalls += 1;
      assert.equal(args.length, 2, 'uses the MYRAA window for the monitor picker');
      return { response: choice };
    },
  };
  installDisplayCaptureHandler({
    session,
    desktopCapturer: {
      async getSources() {
        sourceReads += 1;
        return sources;
      },
    },
    dialog,
    getMainWindow: () => ({ id: 'main-window' }),
    logger: { warn() {} },
  });
  return {
    get sourceReads() { return sourceReads; },
    get dialogCalls() { return dialogCalls; },
    checkPermission: (...args) => permissionCheck(...args),
    requestPermission: (webContents, permission, details) => new Promise((resolve) =>
      permissionRequest(webContents, permission, resolve, details)),
    request: (overrides = {}) => new Promise((resolve) => handler({
      userGesture: true,
      videoRequested: true,
      securityOrigin: 'http://127.0.0.1:3000',
      frame: { top: null },
      ...overrides,
    }, resolve)),
  };
}

test('allows an explicit trusted screen-share request for a single display', async () => {
  const display = { id: 'screen:1:0', name: 'Display 1' };
  const harness = makeHarness({ sources: [display] });
  const frame = {};
  frame.top = frame;
  const result = await harness.request({ frame });
  assert.deepEqual(result, { video: display });
  assert.equal(harness.dialogCalls, 0);
});

test('denies requests without a user gesture or from an untrusted origin', async () => {
  const harness = makeHarness({ sources: [{ id: 'screen:1:0' }] });
  const frame = {};
  frame.top = frame;
  assert.equal(await harness.request({ userGesture: false, frame }), null);
  assert.equal(await harness.request({ securityOrigin: 'https://example.com', frame }), null);
  assert.equal(await harness.request({ frame: { top: {} } }), null);
  assert.equal(harness.sourceReads, 0);
});

test('lets the user choose a display or cancel when multiple are connected', async () => {
  const sources = [{ id: 'screen:1:0', name: 'Main' }, { id: 'screen:2:0', name: 'Second' }];
  const harness = makeHarness({ sources, choice: 1 });
  const frame = {};
  frame.top = frame;
  assert.deepEqual(await harness.request({ frame }), { video: sources[1] });
  assert.equal(harness.dialogCalls, 1);

  const cancel = makeHarness({ sources, choice: 2 });
  const cancelFrame = {};
  cancelFrame.top = cancelFrame;
  assert.equal(await cancel.request({ frame: cancelFrame }), null);
});

test('grants only local main-frame display capture and microphone permissions', async () => {
  const harness = makeHarness();
  const local = { requestingUrl: 'http://127.0.0.1:3000/', isMainFrame: true, mediaType: 'audio' };
  const remote = { requestingUrl: 'https://example.com/', isMainFrame: true, mediaType: 'audio' };
  const subframe = { requestingUrl: 'http://127.0.0.1:3000/', isMainFrame: false, mediaType: 'audio' };

  assert.equal(harness.checkPermission(null, 'display-capture', 'http://127.0.0.1:3000', local), true);
  assert.equal(harness.checkPermission(null, 'display-capture', 'https://example.com', remote), false);
  assert.equal(harness.checkPermission(null, 'display-capture', 'http://127.0.0.1:3000', subframe), false);
  assert.equal(harness.checkPermission(null, 'media', 'http://127.0.0.1:3000', local), true);
  assert.equal(harness.checkPermission(null, 'media', 'http://127.0.0.1:3000', { ...local, mediaType: 'video' }), false);

  assert.equal(await harness.requestPermission(null, 'display-capture', { ...local }), true);
  assert.equal(await harness.requestPermission(null, 'display-capture', { ...remote }), false);
  assert.equal(await harness.requestPermission(null, 'media', { ...local, mediaTypes: ['audio'] }), true);
  assert.equal(await harness.requestPermission(null, 'media', { ...local, mediaTypes: ['audio', 'video'] }), false);
  assert.equal(await harness.requestPermission(null, 'notifications', { ...local }), false);
});
