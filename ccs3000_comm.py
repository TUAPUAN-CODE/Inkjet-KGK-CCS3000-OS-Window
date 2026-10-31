"""
CCS3000 (KGK JET) LAN Communication System
============================================
Implements the protocol described in the "Instruction manual - Communication"
(sections 1-4 to 1-8, and command STM p.96) so a PC / Windows tablet can send
text data (from a QR-code scan or any other source) to the inkjet printer
over LAN (TCP/IP).

Frame format:
    STX(02H) + CMD(3 chars) + Parameters + ETX(03H) [+ Checksum(1 byte)]

Checksum (section 1-7):
    Low-order byte of the sum of every byte between STX and ETX
    (CMD + Parameters), NOT including STX or ETX themselves.

Response:
    ACK (06H)                          -> command executed OK
    NAK (15H)                          -> rejected, no detail
    NAK (15H)+STX+ErrorCode(4)+ETX     -> rejected, with error code
    For G-commands: ACK+STX+Data+ETX   -> success with data

Requires: Python 3.8+ (built-in `socket` module only, no extra installs).
Works on Windows (Surface), macOS, Linux — same code, since it only uses
standard TCP sockets.
"""

import socket
import sys

# ---------------------------------------------------------------------------
# Configuration - edit to match your printer's Basic communications settings
# ---------------------------------------------------------------------------
PRINTER_IP = "192.168.1.110"   # Fixed IP set on the printer's settings screen
PRINTER_PORT = 1024           # The single fixed TCP port configured on printer
USE_CHECKSUM = True           # Must match "Checksum: YES/NO" in printer settings
TIMEOUT_SEC = 5                # Socket timeout (seconds)

STX = 0x02
ETX = 0x03
ACK = 0x06
NAK = 0x15


# ---------------------------------------------------------------------------
# Low-level frame building / checksum
# ---------------------------------------------------------------------------
def calc_checksum(payload: bytes) -> int:
    """Low-order byte of the sum of all bytes (CMD + Parameters)."""
    return sum(payload) & 0xFF


def build_frame(cmd: str, parameters: str, use_checksum: bool = USE_CHECKSUM) -> bytes:
    """Build a full STX...ETX[+checksum] frame ready to send on the socket.

    CRITICAL (verified live against the printer, test5.py 2026-09-04):
    the manual requires a ':' delimiter immediately after the 3-char CMD,
    before the first parameter (e.g. "GMN:0:1:", "STM:0:497::4...:").
    Omitting it (i.e. "GMN0:1:") is a documented negative-control case
    that the printer rejects with NAK — this exact bug existed here
    (cmd + parameters concatenated with no separator) and was the real,
    still-unfixed cause of every NAK the live app was producing, even
    after the message-number fix in build_stm_content_only().
    """
    if len(cmd) != 3:
        raise ValueError("CMD must be exactly 3 characters, e.g. 'STM'")
    payload = (cmd + ":" + parameters).encode("ascii")
    frame = bytes([STX]) + payload + bytes([ETX])
    if use_checksum:
        frame += bytes([calc_checksum(payload)])
    return frame


# ---------------------------------------------------------------------------
# Sending a command and interpreting the reply
# ---------------------------------------------------------------------------
def send_command(sock: socket.socket, cmd: str, parameters: str,
                 use_checksum: bool = USE_CHECKSUM) -> dict:
    """
    Sends one command and waits for the printer's response.
    Returns a dict: {"ok": bool, "data": str|None, "error_code": str|None, "raw": bytes}
    """
    frame = build_frame(cmd, parameters, use_checksum)
    sock.sendall(frame)

    first = sock.recv(1)
    if not first:
        raise ConnectionError("Printer closed the connection")

    if first[0] == ACK:
        # For G(get) commands, response data follows: STX + data + ETX[+checksum]
        # For S/R commands, ACK alone means success -> peek for extra data non-blocking
        sock.settimeout(0.3)
        try:
            rest = sock.recv(4096)
        except socket.timeout:
            rest = b""
        sock.settimeout(TIMEOUT_SEC)
        if rest and rest[0] == STX:
            end = rest.index(bytes([ETX]))
            data = rest[1:end].decode("ascii", errors="replace")
            return {"ok": True, "data": data, "error_code": None, "raw": first + rest}
        return {"ok": True, "data": None, "error_code": None, "raw": first + rest}

    if first[0] == NAK:
        sock.settimeout(0.3)
        try:
            rest = sock.recv(4096)
        except socket.timeout:
            rest = b""
        sock.settimeout(TIMEOUT_SEC)
        if rest and rest[0] == STX:
            end = rest.index(bytes([ETX]))
            err = rest[1:end].decode("ascii", errors="replace")
            return {"ok": False, "data": None, "error_code": err, "raw": first + rest}
        return {"ok": False, "data": None, "error_code": None, "raw": first + rest}

    raise ValueError(f"Unexpected first byte from printer: {first!r}")


# ---------------------------------------------------------------------------
# STM (Set Text Module) helper — the command used to change printed text
# ---------------------------------------------------------------------------
CODE_SYSTEM = {
    "JIS": "0", "ASCII": "1", "SHIFT_JIS": "2", "ASCII+SHIFT_JIS": "3",
    "UNICODE": "4", "GB": "6", "ASCII+GB": "7", "KS": "8",
    "ASCII+KS": "9", "BIG5": "A", "ASCII+BIG5": "B",
}


def build_stm_content_only(module_no, text: str, code_system: str = "UNICODE", msg_no=0) -> str:
    """
    Change ONLY the printed content of an existing Text module, keeping its
    current attributes (matches the manual's example: STM: 0: 1: : 3ABC: ).
    `text` should already be the character-data string in the chosen code
    system (see build_unicode_payload() for how to embed user-font codes).

    VERIFIED 2026-09-04 by live protocol test against the printer (test5.py):
    the first field of STM is NOT a message number — it is a fixed
    "Command version" and the printer only ever accepts "0" here, exactly
    as shown in the manual's example (STM: 0: 1: : 3ABC: ). Text modules
    are addressed globally by module number alone (1-500), the same way
    GTM/GDV/GMN all use "0" too — the on-screen "message" number (e.g. 999)
    plays no part in addressing.

    Proof from the live test:
        STM:0:1::3ABC:        -> ACK   (fixed "0")
        STM:999:497::43132:   -> NAK 1570H  (real message number instead of "0")

    `msg_no` is accepted only for backward compatibility with older
    callers and is intentionally IGNORED — do not wire a real message
    number into it, doing so reproduces the NAK 1570H bug above.
    """
    code = CODE_SYSTEM[code_system.upper()]
    return f"0:{module_no}::{code}{text}:"


def build_unicode_payload(chars) -> str:
    """
    Builds the ASCII string to send for UNICODE code system.
    `chars` is a list where each item is either:
      - a normal string of plain characters (sent as literal ASCII), or
      - an int codepoint (e.g. 0xE000 for user-font slot #1) which is sent
        as its 4-digit hex text, per the manual's encoding rule
        (section 1-4: "The UNICODE of each character is sent in the ASCII
        code converted from it").
    Example: build_unicode_payload(["LOT-", 0xE000, "-A"])  -> "LOT-E000-A"
    """
    out = []
    for item in chars:
        if isinstance(item, int):
            out.append(f"{item:04X}")
        else:
            out.append(item)
    return "".join(out)


def text_to_unicode_hex(text: str) -> str:
    """
    Converts a plain text string into the wire format the printer expects
    when the module's code system is UNICODE: every character becomes its
    4-digit hex codepoint (e.g. "A1" -> "00410031").

    Sending plain literal text under code system UNICODE is a silent bug —
    the printer accepts the frame (correct length/format, so no NAK/error
    code) but decodes every 4 raw characters as one hex codepoint, which
    almost never maps to a real printable glyph. Result: ACK, no error,
    but nothing visible gets printed. This function is what actually needs
    to be sent instead.
    """
    return "".join(f"{ord(ch):04X}" for ch in text)


# ---------------------------------------------------------------------------
# Example end-to-end usage
# ---------------------------------------------------------------------------
def send_text_to_module(module_no: int, text: str, code_system: str = "UNICODE"):
    """Opens a connection, sends one STM command, prints the result, closes."""
    with socket.create_connection((PRINTER_IP, PRINTER_PORT), timeout=TIMEOUT_SEC) as sock:
        params = build_stm_content_only(module_no, text, code_system)
        result = send_command(sock, "STM", params)
        if result["ok"]:
            print(f"[OK] Module {module_no} updated.")
        else:
            print(f"[NAK] Rejected. Error code: {result['error_code']}")
        return result


if __name__ == "__main__":
    # --- Demo: pretend a QR scanner (keyboard-wedge) fed us this line ---
    # A handheld/webcam QR scanner on a barcode-scan setup normally types the
    # decoded text + Enter, just like a keyboard. Read it from stdin here.
    #
    #   Expected QR payload format (define this yourself, e.g.):
    #       <module_no>:<text>
    #   e.g.  1:LOT2026-09-02
    #
    print(f"Connecting to printer at {PRINTER_IP}:{PRINTER_PORT} ...")
    line = input("Scan QR (or type '<module>:<text>') then press Enter: ").strip()
    try:
        module_str, text = line.split(":", 1)
        module_no = int(module_str)
    except ValueError:
        print("Format must be <module_no>:<text>, e.g. 1:HELLO")
        sys.exit(1)

    # Example: to print custom user-font #1 in the middle of the text,
    # build the payload with build_unicode_payload(["PRE-", 0xE000, "-POST"])
    # instead of passing `text` directly.
    send_text_to_module(module_no, text)
