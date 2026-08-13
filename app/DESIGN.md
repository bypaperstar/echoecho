# echoecho Orb — the menu-bar portal app

The viewer grows a second face: a menu-bar (tray) Electron app. echoecho lives in the
menu bar as a small orb; summoning it (tray click, or the "echoecho" wake event
arriving over the existing SSE feed) pours a black amorphous blob out of the menu
bar — a genie-style reveal — which coalesces mid-screen and becomes the stage.
Items (documents, the live screen of echoecho's Mac, transcript wisps) emerge *out of
the blob*, and any item can grow to take over the whole scene while the blob
shrinks to a companion in the corner.

The web viewer at :8765 keeps working unchanged — the app is a client of the same
data plane, not a replacement for it.

## Non-negotiables

- **Everything drawn is code.** No image assets, no video. The tray icon is
  generated pixels (nativeImage from a raw buffer), the blob is procedural
  (shader/canvas), chimes/motion all synthesized. (Vendored *libraries* — noVNC —
  are fine; *artwork* is not.)
- **Python 3.9 / stdlib rule holds on the Python side.** The only Python change
  is a small `/vnc-info` endpoint on the existing viewer server (subprocess to
  `lume get`, env override for tests). Everything Node lives under `app/`.
- **Keyless/Linux testable.** The app must launch under Xvfb on Linux; the VNC
  chain must be exercisable against any RFB server (x11vnc in CI/sandbox), not
  just a Mac VM. `ECHOECHO_VNC_URL` overrides discovery end-to-end.

## Architecture

```
menu bar tray ──click──▶ main.js ──IPC──▶ renderer (transparent frameless window)
                             │                 │
                             │                 ├── scene.js   blob + items + genie reveal
                             │                 ├── blob        procedural, constantly animated
                             │                 ├── vnc.js      noVNC RFB canvas item
                             │                 └── SSE client  http://127.0.0.1:8765/events + /transcript
                             │                        └── wake event ──IPC──▶ summon window
                             └── vnc-proxy.js  WebSocket ⇄ raw TCP bridge to the VM's VNC
                                                    (target from /vnc-info or ECHOECHO_VNC_URL)

python viewer server (:8765)  ──  GET /vnc-info → {"url": "vnc://:pass@ip:port"} | 503
                                   source: ECHOECHO_VNC_URL else `lume get <ECHOECHO_VM_NAME> -f json` .vncUrl
```

- **Window**: frameless, transparent, always-on-top, resizable "scene" anchored
  near the tray icon; hidden (not destroyed) on dismiss so the blob keeps its
  state. `Esc`, tray click, or `Cmd-Shift-E` dismisses (reverse genie).
- **Click-through**: the window spans most of the screen, but only the blob's
  rendered silhouette (field-sampled hit test, small slack) and the items eat
  clicks — everywhere else clicks fall through to whatever is behind
  (`setIgnoreMouseEvents` toggled from hover; mousemove keeps forwarding while
  ignored). So losing focus is routine, and clicking away does *not* dismiss.
- **Dragging**: grab the blob to move it; it trails the cursor liquidly and the
  spot persists (localStorage, as window fractions) across dismissals/restarts.
- **Genie reveal**: macOS reserves the real genie warp for its own windows, so we
  fake it *inside* the transparent window: a reveal parameter `t ∈ [0,1]` drives
  the blob shader — the blob streams from the tray anchor point and coalesces
  into its resting form. Dismissal runs it backwards. (First stab; deeper
  exploration of the warp is an open thread.)
- **Blob**: black, amorphous, never still — breathing/flowing at rest. Items are
  DOM elements composited over the blob canvas; emergence is coordinated (the
  blob bulges toward the item's spawn point, the item scales/unfurls from it).
- **echoecho's Mac item**: a live, *interactive* VNC view (noVNC RFB) of the Lume VM.
  Input forwarded by default — it's echoecho's Mac, blast radius is the sandbox; the
  read-only user-doc mounts still protect real files. A view-only toggle exists.
  Note: user input over VNC is injected by Lume at the virtual-HID level
  (`_VZVNCServer`), so it works even where the agent's `osascript` keystrokes
  are blocked by TCC — the human can type where the agent (today) cannot.

## File ownership (for parallel work)

- `app/package.json`, `app/main.js`, `app/preload.js` — shell
- `app/vnc-proxy.js`, `app/renderer/vnc.js` — VNC chain
- `app/renderer/index.html`, `app/renderer/scene.js`, `app/renderer/blob.js`,
  `app/renderer/style.css` — scene
- `app/prototypes/*.html` — standalone blob explorations (kept, they're the lab)
- `echoecho_app/viewer/server.py` + `tests/test_viewer.py` — `/vnc-info` only

## Out of scope (noted for later)

- True cross-window genie warp; LAN/remote serving with auth (Mac-mini-as-body
  deployment needs Tailscale or TLS+auth); VncGuiDriver for the agent's own
  input (PR 16 candidate); packaging/signing/notarization.
