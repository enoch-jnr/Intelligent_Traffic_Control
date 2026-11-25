# Networking (manual setup)

This document describes the network the laptop must be on. Nothing here is
automated by the code: the laptop is dual-homed, and getting that wrong is the
most common cause of "the cameras work but the gateway does not" (or vice
versa). All commands are examples; substitute your interface names.

## Topology

```text
                 wired Ethernet (cameras)                 Wi-Fi (gateway)
  CAM_MAIN_A .12 ─┐                                      ┌─ ESP32-S3 SoftAP
  CAM_MAIN_B .13 ─┼── switch ── laptop eth0 192.168.50.2 ─┤  192.168.4.1:3333
  CAM_SEC    .11 ─┘                                      └─ (no internet)
```

Two independent L2 networks. The laptop is the only device on both.

## Addressing

| Network | Subnet | Devices |
| --- | --- | --- |
| Camera Ethernet | `192.168.50.0/24` (suggested) | cameras `192.168.50.11/.12/.13`, laptop `192.168.50.2` |
| ESP32 SoftAP | `192.168.4.0/24` (fixed by firmware) | gateway `192.168.4.1`, laptop gets DHCP `192.168.4.x` |

Rules:

- **Do not put the camera Ethernet on `192.168.4.0/24`.** It must not overlap the
  ESP32 SoftAP subnet, or traffic to `192.168.4.1` becomes ambiguous.
- Also avoid the subnets of any other adapter (VPN, Docker/WSL `172.17.0.0/16`,
  home router `192.168.0.0/24` or `192.168.1.0/24`). A `ipconfig /all`
  (Windows) or `ip addr` (Linux) check before assigning static IPs saves an hour.
- Prefer static IPs on the camera subnet. Cameras that use DHCP will change
  address after a power cut and every camera config will silently go stale.
- The ESP32 SoftAP has **no internet** and should not be your default route.

## Dual-homed pitfalls

When both interfaces are up, the OS picks one default route. Problems you will
actually hit:

1. **Default route via Wi-Fi.** Windows may make the Wi-Fi adapter the default
   route because it was connected last, which is harmless for the gateway
   (it is on-link) but can break camera streaming if the camera subnet is
   routed weirdly. With correct on-link subnets both directions work; verify,
   do not assume.
2. **Gateway metric.** If the laptop also has internet Wi-Fi, joining
   `Smarti-Gateway` can hijack DNS/internet. Set the ESP32 Wi-Fi profile to not
   be the default gateway.
3. **Captive-portal / auto-connect.** Windows may refuse to auto-join
   `Smarti-Gateway` because it has no internet. Connect manually and set the
   profile to connect automatically, or the gateway link disappears at boot.
4. **VPN clients** install a default route and can swallow `192.168.4.1`.
   Disconnect the VPN while driving the intersection.

### Windows

```bat
:: interface index + metric
netsh interface ipv4 show interfaces
:: raise the metric of the Wi-Fi gateway adapter so Ethernet wins
netsh interface ipv4 set interface "Wi-Fi" metric=50
:: routes
route print
:: verify which interface a destination uses
Find-NetRoute -RemoteIPAddress 192.168.4.1
```

### Linux

```bash
ip addr
ip route
ip route get 192.168.4.1
ip route get 192.168.50.11
# keep the SoftAP link from becoming the default route (NetworkManager)
nmcli connection modify "Smarti-Gateway" ipv4.never-default yes
```

### Verification checklist

1. `ping 192.168.50.11` (and `.12`, `.13`) — one camera at a time.
2. Open the RTSP URL in a player (VLC) over Ethernet before blaming the code.
3. `ping 192.168.4.1` — the ESP32 answers on its AP.
4. Only then start the app. The JSONL decision log records per-camera
   `frame_age_s`, so a silent camera is visible without a debugger.
5. To inspect the link itself, start with `--show-wire` or open
   **Controls → Wire traffic…** in the console. It shows the exact TX/RX JSON
   lines, including heartbeats and the gateway's rejection reason, without
   changing what is sent.

## Inspecting the gateway link

The wire view is a diagnostic copy, not a second client. It records each
complete line after a successful send and each line as received, before JSON
parsing. It includes the handshake, every heartbeat, traffic requests, replies
and malformed inbound lines. It is bounded to the newest 1,000 lines and is
never written to disk or to `decisions.jsonl`.

Use the CLI when you want a terminal record:

```bash
python main.py --config config.yaml --show-wire
```

Output is one line per message, with the JSON unchanged:

```text
TX {"type":"HEARTBEAT"}
RX {"type":"GATEWAY_ACK","status":"HEARTBEAT_OK"}
```

In the console, open **Controls → Wire traffic…** before or during a run. The
window polls every 400 ms while visible and has **Copy all**. Copy the transcript
before shutting down; the oldest lines are discarded once the 1,000-line bound
is reached.

Useful interpretations:

- **No `TX` heartbeat:** the client is not started, the health gate is closed
  because a camera is stale/unhealthy, or an operator has released control.
- **Request `TX` but no matching `RX`:** the request timed out or the link
  dropped. Look for reconnect/session messages in the console log; a new TCP
  session resets the sequence to 1.
- **`RX` with `ERROR`:** the gateway rejected the last message. The `reason`
  (`AI_LINK_OFFLINE`, `INVALID_FIELDS`, `OUT_OF_POLICY`, `STALE_SEQUENCE`,
  `UNO_ACK_TIMEOUT`, `UNO_NACK`) is the firmware's own reply.
- **A reply arrives but the lamp did not change:** that is expected. A gateway
  or controller ACK means the request was accepted, not that a phase changed.

The transcript contains gateway JSON only, not camera URLs or credentials, but
it may still contain operational detail. Review it before sharing.

## Bandwidth and camera settings

Use each camera's **sub-stream**, not the main stream:

- 720p (or 640x480), H.264, 5–15 fps is plenty for detection and counting.
- Three 1080p main streams are pointless for this workload and will saturate a
  cheap switch or the laptop's decoder.
- Set the encoder to CBR with a 1–2 s keyframe interval. Long GOPs make
  reconnects slow and cost you several seconds of vision after a drop.
- Disable audio and any on-camera motion analytics.
- Budget roughly 1–4 Mbps per sub-stream: ~3–12 Mbps total, far below 100 Mbps
  Ethernet. Latency, not bandwidth, is the usual problem.

## RTSP URLs

The URL path is **vendor-specific**; the code does not guess it. Configure a URL
per camera using environment variables for credentials:

```yaml
url: "rtsp://${CAM_USER}:${CAM_PASS}@192.168.50.11:554/stream2"
```

Set `CAM_USER` / `CAM_PASS` in the environment (or an untracked local config).
The app expands `${VAR}` in string config values and logs a redacted URL.

For reference only, common sub-stream paths (confirm in the camera's manual or
web UI — do not assume one works):

- Hikvision: `/Streaming/Channels/102` (channel 1, sub-stream)
- Dahua: `/cam/realmonitor?channel=1&subtype=1`
- Axis: `/axis-media/media.amp?resolution=640x480`
- Generic ONVIF: `/stream2` or `/live/ch0`

Transport is forced to **TCP** (`OPENCV_FFMPEG_CAPTURE_OPTIONS="rtsp_transport;tcp"`)
by `camera/rtsp.py`. UDP RTSP drops frames on a busy switch and is not used.

## Wi-Fi link to the ESP32

- SSID `Smarti-Gateway`, Prototype password `ChangeThis123`. Supply the password
  through an environment variable; never commit it.
- 2.4 GHz only (ESP32 SoftAP). Keep the laptop close during a demo; RSSI below
  about -75 dBm causes reconnects, and every reconnect resets the protocol
  sequence counter by design.
- One TCP client only. Do not run two copies of the app against the same
  gateway: the second connection is refused or steals the session.
- Ethernet cameras and Wi-Fi gateway can be used at the same time; that is the
  intended deployment.
