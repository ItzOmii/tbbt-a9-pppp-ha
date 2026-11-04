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
