# Medius Bridge (VisionLabs API v7, Python) — targets VisionLabs 3.9.48+ (built against 3.9.59)

> **v1.3.0 — requires Medius firmware 3.4.1 (protocol 8, control link at 6 Mbaud).**
> Firmware 3.4.1 changed the wire protocol and library ABI. The `medius` Python package must be
> the 3.4.1 build to talk to it. An older package on a 3.4.1 box (or the reverse) shows up as
> `open failed: ... NO_REPLY / BAD_PROTO_VER / QUERY_TIMEOUT` in the plugin log, or as a connection
> that appears and drops. See *Upgrading* below — and note the PyPI gotcha there.

Makes a **Medius** USB passthrough box the mouse input for VisionLabs.

```
 real mouse ──USB3──► Medius box ──USB1──► game PC
                          ▲
                          │ USB2 (CH343 serial, 6 Mbaud)
                          │
              VisionLabs PC running this plugin
```

Two directions run at once:

| Direction | VisionLabs side | Medius side |
|---|---|---|
| Out (aim) | `detections` + `tracking.state` subscriptions | `dev.move_rel(dx, dy)` — relative motion layered on top of the real mouse |
| In (activate) | plugin-internal gate | `dev.input_events(...)` — the real mouse's physical buttons, read back from the box |

Nothing is ever *held* on the box. If the plugin dies, the box's 1 s silence timeout returns it to plain passthrough.

## Files

```
plugin.json        manifest (API v7, runtime python, managed env)
plugin.py          the plugin
kmboxnet.py        kmbox-net UDP emulator (VisionLabs' KMBoxNet backend -> Medius)
ui.schema.json     settings window rendered by VisionLabs
requirements.txt   medius  (installed by VisionLabs into its PythonEnvs)
```

## Install

1. Plug the box's **USB2** (control) port into the PC running VisionLabs. Windows shows it as a CH343 COM port (VID `1A86` / PID `55D3`).
2. Either (a) this folder is already unpacked at `<VisionLabs>\Plugins\MediusBridge\` — restart VisionLabs and it appears in the Plugins page; or (b) VisionLabs → Plugins → Install → pick `Medius_Bridge.vlplugin` (a copy sits in `<VisionLabs>\PluginExamples\`).
3. Enable it and approve the permission review.
4. First start takes a little longer while VisionLabs builds the Python environment and pip-installs `medius`. Watch the plugin's log line: it should say `connected fw 3.4.1 proto 8 ...`.

## Upgrading (firmware 3.4.1 / plugin 1.3.0)

**Important:** firmware 3.4.1 uses control protocol 8 and library ABI 8. Do not keep an older `medius` package in VisionLabs' cached environment.

1. Update the box first: https://medius.k4tech.net/dashboard/update (one click). After updating, the box uses protocol 8.
2. Install this plugin package over the old one (VisionLabs → Plugins → Install → `Medius_Bridge.vlplugin`). The manifest version is now 1.3.0 and `requirements.txt` points to the 3.4.1 wheel. Check `PluginLogs\PythonSetup\com_staybeaming_medius_bridge-pip-requirements.log` if setup does not complete.
3. If the pip log still shows PyPI's `medius-3.3.1` or was not rewritten at all (VisionLabs reused the old environment): quit VisionLabs, delete `<VisionLabs>\PythonEnvs\com_staybeaming_medius_bridge`, start VisionLabs again. The environment is recreated from scratch on the next plugin start.
4. Manual fallback: quit VisionLabs, remove the cached environment, and start VisionLabs again so it recreates the environment from `requirements.txt`.
5. The plugin's first log line should print `medius lib 3.4.1, abi 8` and, on connect, `fw 3.4.1 proto 8`. If either is older, repeat step 4.

The plugin's existing mouse controls continue to work. Medius 3.4.1 also adds packet-based CLIP triggers and CLIP entries for pan, raw reports, and control transfers. Those capabilities are available in the updated library, but this end-user plugin does not require users to configure them.

## Recommended mode (v1.2.0): this plugin drives the box, VisionLabs only detects

*Output source = This plugin* is now the default. With *Steer from VisionLabs' tracking target directly* on (default),
the plugin aims at `tracking.state.aimX/aimY` whenever VisionLabs reports a target — VisionLabs does target choice,
FOV and confidence; the plugin does gain / scale / step cap / deadzone / activation. Detections are only used for the
frame size, the overlay and as a fallback when tracking has no target (then the Aim-tab filters apply). Every 10 s the
log prints a `Medius aim:` line saying what came in, what was filtered, how many moves went out, and — if nothing
moved — why. The *Log box + kmbox state* button prints the same line on demand.

## Alternative: VisionLabs native tracking through KMBoxNet emulation (currently a dead end — VisionLabs never opens the UDP socket)

VisionLabs's mouse output backend can't be turned off and has no plugin hook, but it *does* ship a kmbox-net backend, and kmbox-net is plain UDP. So the plugin listens on `127.0.0.1:8808`, pretends to be a kmbox, and forwards everything to Medius:

```
VisionLabs tracking ──KMBoxNet backend──► UDP 127.0.0.1:8808 ──► plugin ──► Medius box ──► game PC
                    ◄── kmbox "monitor" stream (real buttons/motion) ◄── Medius catch
```

Setup, once the plugin shows `connected fw 3.4.1 proto 8`:

1. VisionLabs → Settings → mouse output backend = **KMBoxNet**, IP `127.0.0.1`, port `8808`, UUID anything (e.g. `ABCD12345`).
2. Plugin → General → *Output source* = **VisionLabs native tracking (KMBoxNet emulation)** (the default).
3. For the first run turn on *Hex-dump every kmbox packet* and watch the log: you should see `kmbox client connected from 127.0.0.1:…` followed by `CMD_MOUSE_MOVE` traffic. If the packets look scrambled rather than `28 28 3c af` style headers, VisionLabs's kmNetLib is an encrypted build — send me the dump.
4. Every 10 s the log prints packets/s and the last command, so you can see VisionLabs talking to it without the game running.

What is translated: `move` / `automove` / `bezier` → `move_rel`; button bits → `press` / `soft_release`; `wheel` → `wheel`; `mask_*` → Medius `lock` (block the user's own input on that axis/button), `unmask_all` → unlock; `reboot` → `reset`. Keyboard, LCD and VID/PID commands are acknowledged and ignored. The kmbox *monitor* stream is fed from Medius's catch, so VisionLabs sees the real mouse's button state and motion exactly as it would from a physical kmbox.

Switch *Output source* to **This plugin** to fall back to the detection-driven aim below (useful for A/B comparison on the same hardware).

## Settings

**General**
- *Serial port* — blank = auto-detect the first box. Set `COMx` if you have more than one.
- *Activation* — `Always on`, `While mouse button is held` (default: right button, i.e. ADS), or `Toggle with mouse button`. The button is the **physical** one on the real mouse, read through the box, so it works even if the game PC is a different machine.

**Aim**
- *Minimum confidence*, *Class names*, *Field of view radius* — which detections qualify.
- *Follow VisionLabs' selected target* — when `tracking.state.hasTarget` is true, the detection with that `targetTrackId` wins; otherwise the nearest qualifying box to frame centre.
- *Aim point* — % down from the top of the box (20 % ≈ head on a full-body box, 50 % = centre).
- *Gain* — fraction of the remaining pixel error sent each step. Start at 0.25.
- *Pixels per mouse count* — the one number that must match your game. Move the target until it is exactly N px off-centre, set gain 1.0 / max step 127, press the button once, and adjust until it lands. 
- *Max counts per step*, *Deadzone*, *Send rate*, *Invert Y*.

**Advanced**
- *Show target overlay* — draws the chosen box, the aim point, and a status line on the VisionLabs preview (green = active, amber = idle).
- *Log tracking.state values* — prints raw `aimX` / `aimY` once a second. The SDK does not document those units, so this is how you find out whether they are useful for your profile; the plugin currently steers from detections, not from `aimX/aimY`.
- *Target hold* — how long to keep chasing the last target after detections stop.

## How the send loop behaves

The VisionLabs callbacks only store the latest target; a separate thread runs at *Send rate* and each tick sends `gain × remaining_error / mouse_scale` counts (clamped to *Max counts per step*, sub-count remainder carried forward). After each send the stored error is reduced by the amount just sent, so the plugin does not resend the same error several times before the next detection frame arrives. Detections always overwrite the stored error, so latency between a move and the next frame shows up as mild overshoot — lower *Gain* or *Send rate* if you see oscillation.

## Notes / limits

- Only mouse **movement** and **button read-back** are used. Injecting clicks (`dev.press`) or locking the user's own aim (`dev.lock`) are one-liners with the same `Device` handle if you want them later, but they are deliberately not exposed here.
- The Medius Python streams are synchronous, so the input listener runs on its own thread with `recv_timeout(50)`.
- Reconnect is automatic with back-off (1 s → 10 s). Changing *Serial port* or *Enabled* forces a reconnect.
- Tested against the API v7 contract with a stubbed `medius` module; tune on real hardware.
