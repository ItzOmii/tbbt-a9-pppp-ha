# TBBT / YsxLite "A9 mini camera": getting live video into Home Assistant

Protocol notes, a working patch, and a reproduction walkthrough for the **PPPP variant** of the
A9 mini camera — the one sold under names like TBBT-665567-AMFSR, using UDP/32108 and binary
command ids.

> **Status:** solved. Video (MJPEG, 240×320) streams into Home Assistant via
> [`devbis/pppp_camera`](https://github.com/devbis/pppp_camera) once `aiopppp` is patched.
> Every command, packet id and output below came from a real session.

This repo exists because the camera behaves *almost* correctly with an unpatched client: it logs
in, answers status, switches its lamps, hands out a session ticket — and then never starts video.
Three things are wrong in the client, not the camera.

## TL;DR

| # | Delta | Wrong | Right |
|---|-------|-------|-------|
| 1 | Framing bytes | `BinaryCmdPkt.START_CMD = b'\x11\x0a'` | `b'\x01\x0a'` |
| 2 | Login payload encoding | xq-encoded (library default for every command) | plain text, never encoded |
| 3 | Login ACK decoding | xq-decoded 4-byte ticket | raw `ff 00 00 00` + 4-byte ticket |

With all three, the camera ACKs `0x3010` LiveVideoStart with `0x3110` and streams JPEG frames.
With any one missing it stays silent — while still ACKing `0x3018` VideoParamSet, which is what
makes the failure so confusing.

## Is this your camera?

The "A9" name covers at least two unrelated stacks:

- **This page: the PPPP variant.** UDP/32108, PPPP "DRW" framing, binary command ids, a login
  that returns a 4-byte session ticket.
- **Not this page: the AP-mode variant.** `192.168.4.x`, unencrypted two-byte commands to ports
  8070/8080, no PPPP framing — see [`DEEFRAG/A9`](https://github.com/DEEFRAG/A9). It also streams
  JPEG over UDP, but nothing in this patch applies to it.

If your capture shows two-byte commands and no `f1 d0` DRW headers, you are on the other stack.

## Affected devices and environment

| | |
|---|---|
| Camera | TBBT-665567-AMFSR, TBBT-681302-FNECR — two units, same firmware, same behaviour |
| Also known as | "A9 mini camera" (rebadged widely); vendor app **YsxLite** on iOS/Android |
| Firmware | `mcuver 61.2.22.3`, PPPP device id prefixed `TBBT` |
| Auth | `admin` + an 8-character password generated at pairing. **Not** `6666`. |
| Transport | UDP/32108, PPPP "DRW" framing, P2P punch packets to wake the camera |
| Video | MJPEG — JPEG `ff d8 … ff d9` chunks in DRW video packets, 240×320 |
| Home Assistant | 2026.10.4 in Docker, `network_mode: host`, config at `~/homeassistant/config` |
| Integration | [`devbis/pppp_camera`](https://github.com/devbis/pppp_camera) 1.1.2 (HACS custom repo); requires `aiopppp==0.2.2` and `ffmpeg` |
| Topology used | One flat `/24`: cameras `10.0.0.238` / `10.0.0.25`, iPhone with the vendor app `10.0.0.185`, router `10.0.0.1`. Phone and cameras are both Wi-Fi clients, bridged inside the router. |

## Symptoms

- Camera entry loads; white lamp, IR lamp and reboot all work; entity state reads `streaming`.
- No image, ever, with:

```
WARNING (MainThread) [custom_components.pppp_camera] Error getting video frame: Not connected to the camera
```

- On the wire: `0x3018` VideoParamSet is answered with `0x3118`; `0x3010` LiveVideoStart is
  answered with **nothing** — no `0x3110`, no frames. Raw hand-built packets and the Go
  `cam-reverse` implementation fail identically, so this is not a Home Assistant bug.

## Root cause

The camera issues a session ticket to a login it never parsed. Everything that does not need
media keeps working — status reads, lamp commands, PTZ — because those are answered without
checking that the session is really authenticated. Media does check, so it is ignored silently.

### 1. Binary commands use start bytes `01 0A`

The vendor app sends `01 0A` where `aiopppp` sends `11 0A`, and the camera mirrors whatever it
receives, so the receive path must accept both or the ACKs are dropped.

```python
class BinaryCmdPkt(DrwPkt):
    START_CMD = b'\x01\x0a'                       # was b'\x11\x0a'

# packets.py, receive path
elif data[4:6] in (b'\x11\x0a', b'\x01\x0a'):  # accept both
```

### 2. The login payload is plain text

`aiopppp` xq-encodes every payload (each byte ±1 by parity, then rotate by 4). The camera does
not decode the login, so encoded credentials arrive as noise — and it returns a ticket anyway,
so every log line looks like a successful login.

```python
if self.cmd_payload:
    if getattr(self.command, 'value', None) == 0x1020:   # CMD_SYSTEM_USER_CHK
        data += self.cmd_payload                          # plain text: no xq
    else:
        data += xq_bytes_encode(self.cmd_payload, 4)
```

### 3. The login ACK is not xq-encoded either

The ACK body is `ff 00 00 00` followed by the 4-byte session ticket. Decoding it yields a
garbage ticket, so every later command carries credentials the camera does not recognise.

```python
# packets.py, receive path
if len(cmd_bin_payload) and command_num != 0x1120:       # ACK_SYSTEM_USER_CHK
    cmd_bin_payload = xq_bytes_decode(cmd_bin_payload, 4)

# session.py, login()
if len(auth_result) == 8 and auth_result[:4] == b'\xff\x00\x00\x00':
    self.ticket = auth_result[4:8]                       # success
elif auth_result != b'':
    raise AuthError(f'Login failed: [{auth_result.hex(" ")}]')
```

## Install

`aiopppp` is not part of the Home Assistant image — the custom integration pip-installs it at
first start — so patching a running container is lost on the next image pull. Bake it in.

`patch_aiopppp_tbbt.py` applies all six hunks idempotently, matches exact source anchors, and
exits non-zero if a future `aiopppp` release moves an anchor, so a build fails loudly instead of
shipping a broken camera.

<details><summary><code>patch_aiopppp_tbbt.py</code></summary>

```python
#!/usr/bin/env python3
"""Patch aiopppp for TBBT/SmartCam PPPP battery cameras (firmware mcuver 61.2.22.3).

Applied inside the Home Assistant image so the fixes survive container rebuilds.

Discovered by packet-capturing the vendor iPhone app:
  * binary commands use start bytes 0x010A, not 0x110A
  * the login (CMD_SYSTEM_USER_CHK 0x1020) carries PLAIN 'admin'+password, not xq-encoded
  * the login ack is 'ff 00 00 00' + 4-byte session ticket, sent unencoded
  * without all of the above the camera acks VideoParamSet but silently ignores
    LiveVideoStart -> no video. With them, video streams as MJPEG.
"""
import sys

SP = "/usr/local/lib/python3.14/site-packages/aiopppp/session.py"
PP = "/usr/local/lib/python3.14/site-packages/aiopppp/packets.py"
MARK = "TBBT"

session_subs = [
    ("""            if drw_pkt.command == BinaryCommands.ACK_SYSTEM_USER_CHK and len(drw_pkt.cmd_payload) >= 20:
                # this is from cam-reverse code
                self.ticket = drw_pkt.cmd_payload[16:20]""",
     """            if drw_pkt.command == BinaryCommands.ACK_SYSTEM_USER_CHK:
                _p = drw_pkt.cmd_payload
                if len(_p) >= 20:
                    # this is from cam-reverse code
                    self.ticket = _p[16:20]
                elif len(_p) == 8 and _p[:4] == b'\\xff\\x00\\x00\\x00':
                    # TBBT firmware: marker + 4-byte session ticket
                    self.ticket = _p[4:8]"""),
    ("""        if auth_result != b'':
            raise AuthError(f'Login failed: [{auth_result.hex(" ")}]')
        return True""",
     """        if auth_result != b'':
            if len(auth_result) == 8 and auth_result[:4] == b'\\xff\\x00\\x00\\x00':
                # TBBT firmware: ff 00 00 00 + 4-byte ticket == success
                self.ticket = auth_result[4:8]
                logger.info('Login ok (TBBT variant), ticket=%s', self.ticket.hex())
            else:
                raise AuthError(f'Login failed: [{auth_result.hex(" ")}]')
        return True"""),
]

packet_subs = [
    ("""class BinaryCmdPkt(DrwPkt):
    START_CMD = b'\\x11\\x0a'""",
     """class BinaryCmdPkt(DrwPkt):
    START_CMD = b'\\x01\\x0a'  # TBBT"""),
    ("""        if self.cmd_payload:
            data += xq_bytes_encode(self.cmd_payload, 4)
        return data""",
     """        if self.cmd_payload:
            if getattr(self.command, 'value', None) == 0x1020:
                # TBBT: credentials are plain text, not xq-encoded
                data += self.cmd_payload
            else:
                data += xq_bytes_encode(self.cmd_payload, 4)
        return data"""),
    ("""    elif data[4:6] == b'\\x11\\x0a':""",
     """    elif data[4:6] in (b'\\x11\\x0a', b'\\x01\\x0a'):  # TBBT"""),
    ("""                if len(cmd_bin_payload):
                    cmd_bin_payload = xq_bytes_decode(cmd_bin_payload, 4)""",
     """                if len(cmd_bin_payload) and command_num != 0x1120:
                    # TBBT: login ack (0x1120) is not xq-encoded
                    cmd_bin_payload = xq_bytes_decode(cmd_bin_payload, 4)"""),
]


def apply(path, subs):
    src = open(path).read()
    changed = 0
    for old, new in subs:
        if new in src:
            continue  # already patched
        if old not in src:
            print(f"ANCHOR MISS in {path}:\n{old[:100]}")
            sys.exit(1)
        src = src.replace(old, new, 1)
        changed += 1
    if changed:
        open(path, "w").write(src)
    print(f"{path}: {changed} hunk(s) applied")


apply(SP, session_subs)
apply(PP, packet_subs)
print("aiopppp patched for TBBT cameras")
```
</details>

<details><summary><code>Dockerfile</code></summary>

```dockerfile
FROM homeassistant/home-assistant:stable

# TBBT PPPP battery cams (TBBT-665567-AMFSR / TBBT-681302-FNECR) need aiopppp patched:
# 010A start bytes, plain-text login, unencoded 'ff 00 00 00 <ticket>' login ack.
# Without it the camera acks VideoParamSet but never starts video (no MJPEG stream).
# aiopppp is normally installed at first start by the pppp_camera custom component,
# so install it here and patch it in the image instead.
RUN pip3 install --no-cache-dir aiopppp==0.2.2

COPY patch_aiopppp_tbbt.py /tmp/patch_aiopppp_tbbt.py
RUN python3 /tmp/patch_aiopppp_tbbt.py && rm /tmp/patch_aiopppp_tbbt.py
```
</details>

```yaml
# docker-compose.yml
services:
  homeassistant:
    build: .
    image: homeassistant-tbbt:local
    container_name: homeassistant
    restart: unless-stopped
    network_mode: host
```

```bash
docker compose build
docker compose up -d
```

## Verify

Check bytes, not entity state. `streaming` only means Home Assistant is willing to ask; a JPEG
magic number means the camera answered.

```bash
docker exec homeassistant python3 /tmp/ha_save_snaps.py
# camera.tbbt_665567_amfsr: http=200 bytes=11488 jpeg=True
# camera.tbbt_681302_fnecr: http=200 bytes=10780 jpeg=True

$ file /tmp/ha_238.jpg
/tmp/ha_238.jpg: JPEG image data, baseline, precision 8, 240x320, components 3
```

Outside Home Assistant, `harness_frame_test.py` connects, starts video and writes the first
frame — the shortest path from "patched" to "proven".

```bash
pip install aiopppp==0.2.2
python harness_frame_test.py 10.0.0.238 'YOURCAMERAPASSWORD' /tmp/frame.jpg
# frame: 5484 bytes, magic: ff d8 ff db
# saved /tmp/frame.jpg 5484
```

<details><summary><code>harness_frame_test.py</code></summary>

```python
import asyncio, logging, sys, struct
sys.path.insert(0, '/tmp')
logging.basicConfig(level=logging.ERROR, stream=sys.stdout)
from aiopppp import Device
from aiopppp.encrypt import ENC_METHODS

IP = sys.argv[1]
PWD = sys.argv[2]
OUT = sys.argv[3] if len(sys.argv) > 3 else '/tmp/ha_frame.jpg'


def drw(idx, start, cmd, dest, token, payload=b''):
    body = (b'\xd1\x00' + struct.pack('>H', idx) + start
            + struct.pack('<H', cmd) + struct.pack('<H', len(payload) + len(token))
            + struct.pack('<H', dest) + token + payload)
    return b'\xf1\xd0' + struct.pack('>H', len(body)) + body


async def main():
    dev = Device(IP, "admin", PWD)
    await dev.connect(timeout=30)
    sess = dev.session
    enc = ENC_METHODS[sess.dev.encryption][1]

    def send_raw(cmd, dest, payload=b''):
        idx = sess.outgoing_command_idx
        sess.outgoing_command_idx += 1
        sess.transport.sendto(enc(drw(idx, b'\x01\x0a', cmd, dest, sess.ticket, payload)),
                              (sess.dev.addr, sess.dev.port))

    send_raw(0x1008, 0x00ff)
    await asyncio.sleep(0.3)
    for _ in range(6):
        send_raw(0x3018, 0x00ff, bytes.fromhex("0100000000000000"))
        send_raw(0x3010, 0x0000)
        send_raw(0x3018, 0x00ff, bytes.fromhex("0700000020000000"))
    print("ticket:", sess.ticket.hex())

    got = None
    for attempt in range(12):
        try:
            fr = await asyncio.wait_for(dev.get_video_frame(), timeout=8)
        except Exception as e:
            print("frame attempt", attempt, "->", type(e).__name__)
            continue
        data = fr.data if hasattr(fr, 'data') else bytes(fr)
        print("frame:", len(data), "bytes, magic:", data[:4].hex(' '))
        if data[:2] == b'\xff\xd8':
            got = data
            break
        if b'\xff\xd8' in data:
            got = data[data.index(b'\xff\xd8'):]
            break
    if got:
        open(OUT, 'wb').write(got)
        print("saved", OUT, len(got))
    else:
        print("NO JPEG FRAME")
    try:
        await dev.close()
    except Exception:
        pass


asyncio.run(main())
```
</details>

## Reproduction walkthrough

1. **Recover the camera's real password.** Pairing generates an 8-character password and the
   app holds it. If the app cannot show it, read it from a login capture: the `0x1020` payload is
   160 bytes, ASCII username at offset 0, ASCII password at offset 32. Do not settle for `6666`.
2. **Install Home Assistant and the integration.** HACS custom repository `devbis/pppp_camera`,
   then add the camera through the config flow with `admin` + the real password. *Pass:* the
   entry loads and the lamp switches work. That proves the session reaches the camera.
3. **Put a capture point in the network path.** Any wired Linux host on the same subnet. Allow
   forwarding for the phone↔camera pair **first**, then ARP-poison the camera side and capture:

   ```bash
   iptables -I FORWARD 1 -s 10.0.0.185 -d 10.0.0.238 -j ACCEPT
   iptables -I FORWARD 1 -s 10.0.0.238 -d 10.0.0.185 -j ACCEPT
   python3 arp_poison.py 900 10.0.0.238 CAM_MAC 10.0.0.1 10.0.0.238 CAM_MAC 10.0.0.185
   tcpdump -i eth0 -n -s0 -w /tmp/cam.pcap "host 10.0.0.238"
   ```

4. **Stream from the vendor app and read the capture.** Look for `0x1020` login, the video burst
   (`0x3018` and `0x3010` within a few milliseconds), and the camera's `0x3110` reply. *Fail:*
   `0x3118` present and `0x3110` absent means you have this exact bug.
5. **Patch, do not rewrite.** Apply the three deltas to `aiopppp`, inside the image.
6. **Prove it with JPEG magic bytes.** Fetch `/api/camera_proxy/camera.your_entity` or run the
   harness; the body must start `ff d8`. A dark 240×320 frame of a back yard is success.

## Traps that look like answers

- **Docker's `FORWARD DROP` policy.** With a MITM in the path and no `ACCEPT` rules, the camera's
  replies are blackholed. The app's stream stalls, it reconnects through the vendor cloud relay,
  and the capture shows no LAN traffic at all — which reads exactly like "this camera cannot be
  reached locally".
- **The default password `6666`.** It logs in, reads status, issues a ticket, works the lamps —
  and never streams. About 90% working, the worst possible amount.
- **One `0x3110` ACK right after a previous stream.** The encoder is still warm from the vendor
  app's session. It ACKs once and sends nothing. False positive, not a fix.
- **MAC flooding.** `macof` with 42,000 bogus MACs produced an empty capture: two Wi-Fi clients of
  the same AP are bridged inside the router and never touch the wired switch.
- **Poisoning the phone.** iOS re-asserts its own ARP entry, so the app's packets arrive
  intermittently. Poison the camera, which does not defend its cache.
- **Patching the container.** The next image pull removes it. Patch the image.

## Appendix: the app's command sequence

```
0x1020  login        token 00000000, payload 32s "admin" + 128s password  (PLAIN TEXT)
0x1008  status get   token = session ticket
0x3018  vparam       01 00 00 00 00 00 00 00    type 1 (resolution) = 0
0x3010  live start   no payload, dest 0x0000
0x3018  vparam       07 00 00 00 20 00 00 00    type 7 = 32
0x3019  vparam get
0x50ff  passthrough  JSON strings (PTZ)
0x3011  live stop
```

ACKs come back as the request's command id plus `0x100`: `0x3018`→`0x3118`, `0x3010`→`0x3110`
(264-byte payload), `0x1008`→`0x1108`. Video arrives as DRW video packets from UDP/32108 to the
client's source port, JPEG chunks of roughly 1 KB. The whole video group is sent as one burst;
spacing it over hundreds of milliseconds is enough to be ignored.

## References

- [`devbis/pppp_camera`](https://github.com/devbis/pppp_camera) — the Home Assistant integration.
- [`DEEFRAG/A9`](https://github.com/DEEFRAG/A9) — the AP-mode A9 variant.
- [Popular A9 mini Wi-Fi camera & the HA challenge](https://community.home-assistant.io/t/popular-a9-mini-wi-fi-camera-the-ha-challenge/230108)
  — the long-running HA community thread on this camera family.
- [An overview of the PPPP protocol for IoT cameras](https://palant.info/2025/11/05/an-overview-of-the-pppp-protocol-for-iot-cameras/)
  — framing, discovery on UDP 32108, and the A9 lineage.

## Provenance

Captures, patch and reproduction were done on meepBox, an always-on i5 Linux box (no GPU) running
Home Assistant in Docker, against two TBBT/YsxLite A9 cameras on a flat home LAN. No vendor cloud
was involved in the fix.
