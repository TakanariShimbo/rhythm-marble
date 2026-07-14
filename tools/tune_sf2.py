#!/usr/bin/env python3
"""FluidR3_GM.sf2 のcelestaの調律ずれを直した調律済みコピーを作る。

FluidR3のcelesta(program 8)はサンプルゾーン単位で調律誤差がある
(単音レンダリング+FFTピークで実測、2026-07):

    key 84-89 (Celesta C 6):  +10.0 cent  ← 「半音ずれた」ように聞こえる主犯
    key 96-108 (Celesta C 7):  +5.8 cent
    key 90-95 (Celesta F# 6):  +1.6 cent
    key 78-83 (Celesta F# 5):  -0.8 cent

修正はSF2のサンプルヘッダ(shdr)にある pitchCorrection バイト(int8, cent)の
書き換えのみ。波形データ・ループ・エンベロープには一切触れない。

使い方:
    uv run python tools/tune_sf2.py            # vendor/FluidR3_GM.sf2 → vendor/FluidR3_GM_tuned.sf2
    uv run python tools/tune_sf2.py in.sf2 out.sf2
"""
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# サンプル名 → 補正値(cent)。実測ずれの符号反転。
CORRECTIONS = {
    "Celesta C 6(L)": -10, "Celesta C 6(R)": -10,
    "Celesta C 7(L)": -6,  "Celesta C 7(R)": -6,
    "Celesta F# 6(L)": -2, "Celesta F# 6(R)": -2,
    "Celesta F# 5(L)": +1, "Celesta F# 5(R)": +1,
}

SHDR_REC = 46          # サンプルヘッダのレコード長
PCORR_OFF = 41         # レコード内の pitchCorrection バイト位置


def find_shdr(data: bytes):
    """RIFF→LIST pdta→shdr サブチャンクの(開始オフセット, サイズ)を返す。"""
    pos = 12
    while pos < len(data) - 8:
        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        if cid == b"LIST" and data[pos + 8:pos + 12] == b"pdta":
            sub = pos + 12
            end = pos + 8 + size
            while sub < end - 8:
                scid = data[sub:sub + 4]
                ssize = struct.unpack("<I", data[sub + 4:sub + 8])[0]
                if scid == b"shdr":
                    return sub + 8, ssize
                sub += 8 + ssize + (ssize % 2)
        pos += 8 + size + (size % 2)
    sys.exit("エラー: shdrチャンクが見つかりません")


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "vendor/FluidR3_GM.sf2"
    dst = (Path(sys.argv[2]) if len(sys.argv) > 2
           else src.with_name(src.stem + "_tuned.sf2"))
    data = bytearray(src.read_bytes())
    off, size = find_shdr(bytes(data))
    patched = []
    for i in range(size // SHDR_REC):
        rec = off + i * SHDR_REC
        name = bytes(data[rec:rec + 20]).split(b"\0")[0].decode(errors="replace")
        if name in CORRECTIONS:
            old = struct.unpack("<b", data[rec + PCORR_OFF:rec + PCORR_OFF + 1])[0]
            new = old + CORRECTIONS[name]
            data[rec + PCORR_OFF:rec + PCORR_OFF + 1] = struct.pack("<b", new)
            patched.append(f"  {name}: pitchCorrection {old:+d} → {new:+d} cent")
    if len(patched) != len(CORRECTIONS):
        sys.exit(f"エラー: 期待した8サンプル中{len(patched)}件しか見つかりません")
    dst.write_bytes(bytes(data))
    print(f"{src.name} → {dst.name} ({len(patched)}サンプルを調律)")
    print("\n".join(patched))


if __name__ == "__main__":
    main()
