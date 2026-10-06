#!/usr/bin/env python3
"""Fast USB Flasher for Model:Cycles using the Elektron Transfer Protocol.

Flashes a custom .syx firmware directly over USB MIDI in ~33 seconds while the synth
is powered on in normal operating mode (no STARTUP MENU needed).

Usage:
    python3 tools/fastflash.py model-cycles_OS1.13_mod.syx
"""
import argparse
import math
import pathlib
import sys
import time
import zlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

MSG_HEADER = bytes([0xF0, 0, 0x20, 0x3C, 0x10, 0])
OS_TRANSF_BLOCK_BYTES = 0x800
REST_TIME_SEC = 0.050  # 50 ms pause after each block
ELEKTRON = (0x00, 0x20, 0x3C)
PRODUCTS = {0x0F: "Model:Samples", 0x11: "Model:Cycles"}


def elektron_encode_payload(src):
    dst = bytearray(len(src) + math.ceil(len(src) / 7))
    i = j = 0
    while j < len(src):
        accum = 0
        for k in range(7):
            accum <<= 1
            if j + k < len(src):
                if src[j + k] & 0x80:
                    accum |= 1
                dst[i + k + 1] = src[j + k] & 0x7F
        dst[i] = accum
        i += 8
        j += 7
    return bytes(dst)


def elektron_decode_payload(src):
    dst = bytearray(len(src) - math.ceil(len(src) / 8))
    i = j = 0
    while i < len(src):
        shift = 0x40
        k = 0
        while k < 7 and i + k + 1 < len(src):
            dst[j + k] = src[i + k + 1] | (0x80 if src[i] & shift else 0)
            shift >>= 1
            k += 1
        i += 8
        j += 7
    return bytes(dst)


def elektron_tx(msg, seq):
    msg = bytearray(msg)
    msg[0:2] = seq.to_bytes(2, "big")
    return MSG_HEADER + elektron_encode_payload(bytes(msg)) + b"\xf7"


def elektron_rx_parse(raw_bytes, seq_expected, cmd_expected):
    if not raw_bytes or len(raw_bytes) < 12 or raw_bytes[-1] != 0xF7:
        return None
    if raw_bytes[:6] != MSG_HEADER:
        return None
    decoded = elektron_decode_payload(raw_bytes[6:-1])
    if len(decoded) < 5:
        return None
    seq = (decoded[2] << 8) | decoded[3]
    cmd = decoded[4]
    if seq == seq_expected and cmd == cmd_expected:
        return decoded
    return None


def elektron_crc(data):
    return zlib.crc32(data, 0xFFFFFFFF)


def split_sysex(raw):
    msgs, i = [], 0
    while True:
        a = raw.find(b"\xf0", i)
        if a < 0:
            break
        b = raw.find(b"\xf7", a)
        if b < 0:
            raise SystemExit(f"!! SysEx non terminé à l'offset {a}")
        msgs.append(raw[a:b + 1])
        i = b + 1
    return msgs


def verify_syx(raw, path):
    msgs = split_sysex(raw)
    if len(msgs) < 3:
        raise SystemExit("!! Pas un fichier firmware Elektron valide (trop peu de messages).")
    head = msgs[0]
    if tuple(head[1:4]) != ELEKTRON:
        raise SystemExit(f"!! En-tête non-Elektron : {head[1:5].hex(' ')}")
    dev = head[4]
    name = PRODUCTS.get(dev, f"Inconnu (0x{dev:02x})")
    print(f"[+] Fichier firmware : {path}")
    print(f"    - Taille         : {len(raw):,} octets ({len(msgs):,} messages SysEx)")
    print(f"    - Produit        : 0x{dev:02x} ({name})")
    return dev, name, msgs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("syx", help="fichier .syx à envoyer")
    ap.add_argument("-p", "--port", default="Model:Cycles", help="sous-chaîne du port USB MIDI")
    ap.add_argument("--timeout", type=float, default=5.0, help="délai de réponse par bloc (s)")
    args = ap.parse_args()

    syx_path = pathlib.Path(args.syx).resolve()
    if not syx_path.exists():
        raise SystemExit(f"!! Fichier {syx_path} introuvable.")

    raw = syx_path.read_bytes()
    dev, dev_name, msgs = verify_syx(raw, syx_path)

    try:
        import mido
    except ImportError:
        venv_mido = HERE.parent / ".venv" / "lib"
        if venv_mido.exists():
            sys.path.insert(0, str(venv_mido))
            import mido
        else:
            raise SystemExit("!! Module 'mido' introuvable.")

    outs = mido.get_output_names()
    ins = mido.get_input_names()

    out_port = next((p for p in outs if args.port.lower() in p.lower()), None)
    in_port = next((p for p in ins if args.port.lower() in p.lower()), None)

    if not out_port or not in_port:
        raise SystemExit(f"!! Impossible de trouver la paire de ports USB MIDI '{args.port}'.\n"
                         f"    Outputs disponibles : {outs}\n"
                         f"    Inputs disponibles  : {ins}")

    print(f"[+] Paire USB MIDI détectée : '{out_port}' / '{in_port}'")
    print(f"[+] Protocole Elektron Transfer direct activé (~33 secondes de flash)...")

    seq = 1
    total_blocks = math.ceil(len(raw) / OS_TRANSF_BLOCK_BYTES)

    with mido.open_output(out_port) as m_out, mido.open_input(in_port) as m_in:
        # Step 1: Send Start Request (cmd 0x50)
        # body: [0x50, size_le (4B), "sysex\0", 0x01]
        size_le = len(raw).to_bytes(4, "little")
        start_body = bytes([0x50]) + size_le + b"sysex\0\x01"
        start_tx = elektron_tx(b"\x00\x00\x00\x00" + start_body, seq)
        seq += 1

        print(f"[+] Initialisation du transfert (OS Upgrade Start)...", flush=True)
        m_out.send(mido.Message.from_bytes(start_tx))

        # Wait response for cmd 0x50 -> response cmd 0xD0 (0x50 | 0x80)
        t_start = time.time()
        start_ack = None
        while time.time() - t_start < args.timeout:
            for msg in m_in.iter_pending():
                if msg.type == "sysex":
                    parsed = elektron_rx_parse(bytes(msg.bin()), seq - 1, 0xD0)
                    if parsed:
                        start_ack = parsed
                        break
            if start_ack:
                break
            time.sleep(0.005)

        if not start_ack:
            raise SystemExit("!! Pas de réponse au démarrage d'upgrade. Vérifie que le Model:Cycles est allumé en mode normal.")
        
        status = start_ack[5]
        if status != 0:
            raise SystemExit(f"!! Le Model:Cycles a refusé le démarrage du flash (code status {status}).")

        print(f"[+] Transfert démarré ! Envoi de {total_blocks} blocs de {OS_TRANSF_BLOCK_BYTES} octets...")
        t0 = time.time()
        offset = 0
        block_idx = 0

        while offset < len(raw):
            n = min(OS_TRANSF_BLOCK_BYTES, len(raw) - offset)
            block = raw[offset:offset + n]
            
            # body: [0x51, crc32 (4B big), len (4B big), offset (4B big), block_bytes]
            crc_val = elektron_crc(block)
            write_body = (bytes([0x51]) + 
                          crc_val.to_bytes(4, "big") + 
                          n.to_bytes(4, "big") + 
                          offset.to_bytes(4, "big") + 
                          block)
            
            write_tx = elektron_tx(b"\x00\x00\x00\x00" + write_body, seq)
            current_seq = seq
            seq += 1

            m_out.send(mido.Message.from_bytes(write_tx))

            # Wait response for cmd 0x51 -> response cmd 0xD1
            t_blk = time.time()
            blk_ack = None
            while time.time() - t_blk < args.timeout:
                for msg in m_in.iter_pending():
                    if msg.type == "sysex":
                        parsed = elektron_rx_parse(bytes(msg.bin()), current_seq, 0xD1)
                        if parsed:
                            blk_ack = parsed
                            break
                if blk_ack:
                    break
                time.sleep(0.002)

            if not blk_ack:
                raise SystemExit(f"!! Interruption sur le bloc {block_idx + 1}/{total_blocks} (délai d'attente dépassé).")

            offset += n
            block_idx += 1

            op_code = int.from_bytes(blk_ack[9:10], "big", signed=True)
            if op_code > 1:
                raise SystemExit(f"!! Erreur d'écriture sur le bloc {block_idx} (code {op_code}).")

            # Progress display
            el = time.time() - t0
            pct = 100.0 * offset / len(raw)
            eta = (el / offset) * (len(raw) - offset) if offset > 0 else 0
            sys.stdout.write(f"\r    [Progress] {block_idx:>4}/{total_blocks} blocs ({pct:5.1f}%) | {el:4.1f}s écoulées | ETA ~{eta:3.1f}s")
            sys.stdout.flush()

            if op_code == 1:
                break

            time.sleep(REST_TIME_SEC)

    elapsed = time.time() - t0
    print(f"\n\n[✅] FLASH REUSSI en {elapsed:.1f} secondes !")
    print("     Confirme l'installation du firmware sur l'écran du Model:Cycles avec [YES].")


if __name__ == "__main__":
    main()
