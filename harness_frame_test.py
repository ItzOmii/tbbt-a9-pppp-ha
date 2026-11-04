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
