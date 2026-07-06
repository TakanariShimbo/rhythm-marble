#!/usr/bin/env python3
"""MIDIをオルゴール/チェレスタ/マリンバ等の音色のMP3に変換する。

処理の流れ:
  1. MIDIからトラック選択、必要なら単音メロディ抽出(--melody)
  2. 指定した音色(GM音源)に差し替え、オクターブシフト
  3. FluidSynth + サウンドフォントでレンダリング、リバーブ
  4. ffmpegでMP3出力

使い方:
  uv run python convert.py input.mid --track 1 --melody -i celesta
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

# GMプログラム番号 (0始まり)
INSTRUMENTS = {
    "celesta": 8,
    "glockenspiel": 9,   # 鉄琴
    "music_box": 10,     # オルゴール
    "vibraphone": 11,
    "marimba": 12,
    "xylophone": 13,
    "kalimba": 108,
    "harp": 46,
}

# 音色ごとの推奨オクターブシフト(オルゴール系は1オクターブ上げると映える)
DEFAULT_OCTAVE = {
    "music_box": 1,
    "glockenspiel": 1,
    "celesta": 1,
}

DEFAULT_SF2 = "/usr/share/sounds/sf2/TimGM6mb.sf2"
SAMPLE_RATE = 44100


def extract_melody(midi_data, min_pitch: int = 0, group_ms: float = 80.0):
    """主旋律だけを単音で抜き出す(スカイライン方式)。

    1. 低すぎるノート(伴奏・ベース)を除外
    2. ほぼ同時に鳴るノート群からは最高音だけを残す
    3. 高音が鳴っている間に始まる下の伴奏ノートを除外
    4. 前の音の尾を次の音の頭で切って完全な単音にする
    """
    notes = [n for inst in midi_data.instruments for n in inst.notes
             if n.pitch >= min_pitch]
    notes.sort(key=lambda n: (n.start, -n.pitch))

    group_s = group_ms / 1000.0
    kept = []
    for n in notes:
        if kept and n.start - kept[-1].start < group_s:
            continue  # 同時打鍵グループ: 最初(=最高音)だけ残す
        if kept and kept[-1].end > n.start and kept[-1].pitch > n.pitch:
            continue  # 上のメロディが鳴っている最中の下の伴奏音は捨てる
        kept.append(n)
    for a, b in zip(kept, kept[1:]):
        if a.end > b.start:
            a.end = b.start

    inst = midi_data.instruments[0]
    midi_data.instruments = [inst]
    inst.notes = kept
    print(f"      メロディ抽出後: {len(kept)}ノート")
    return midi_data


def restyle(midi_data, program: int, octave_shift: int, min_velocity: int):
    """全トラックを指定音色に差し替え、オクターブシフトを適用する。"""
    # ドラムは音程楽器に変換すると破綻するので除外する
    midi_data.instruments = [i for i in midi_data.instruments if not i.is_drum]
    for inst in midi_data.instruments:
        inst.program = program
        inst.is_drum = False
        inst.pitch_bends = []
        for note in inst.notes:
            note.pitch = min(127, max(0, note.pitch + 12 * octave_shift))
            # 弱すぎるノートを持ち上げて、オルゴールらしい均一な鳴りに寄せる
            note.velocity = max(min_velocity, note.velocity)
    return midi_data


def render(midi_data, sf2_path: Path, out_mp3: Path, gain_db: float,
           reverb: float = 0.0):
    """FluidSynthでWAVにレンダリングし、ffmpegでMP3化する。"""
    import numpy as np
    import soundfile as sf

    print(f"[2/3] レンダリング中 (soundfont: {sf2_path.name})")
    audio = midi_data.fluidsynth(fs=SAMPLE_RATE, sf2_path=str(sf2_path))

    # リバーブ等の空間系エフェクト(心地よい残響)
    if reverb > 0:
        from pedalboard import Pedalboard, Reverb, LowpassFilter
        board = Pedalboard([
            LowpassFilter(cutoff_frequency_hz=9500),   # 高域を少し丸める
            Reverb(room_size=reverb, damping=0.5,
                   wet_level=0.28, dry_level=0.75, width=1.0),
        ])
        if audio.ndim == 1:
            audio = np.stack([audio, audio])           # ステレオ化(広がり)
        audio = board(audio.astype(np.float32), SAMPLE_RATE).T

    # ノーマライズ(ピークを-1dBFSに)
    peak = np.abs(audio).max()
    if peak > 0:
        audio = audio / peak * 10 ** ((-1.0 + gain_db) / 20)

    print(f"[3/3] MP3出力中: {out_mp3}")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
        sf.write(tmp.name, audio, SAMPLE_RATE)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-i", tmp.name, "-codec:a", "libmp3lame", "-b:a", "192k",
             str(out_mp3)],
            check=True,
        )


def main():
    parser = argparse.ArgumentParser(
        description="MP3をオルゴール/鉄琴/マリンバ風のMP3に変換する")
    parser.add_argument("input", type=Path, help="入力MIDI (.mid)")
    parser.add_argument("-o", "--output", type=Path,
                        help="出力MP3パス (省略時: <入力名>_<音色>.mp3)")
    parser.add_argument("-i", "--instrument", choices=INSTRUMENTS,
                        default="music_box", help="音色 (デフォルト: music_box)")
    parser.add_argument("--octave", type=int, default=None,
                        help="オクターブシフト (デフォルトは音色ごとの推奨値)")
    parser.add_argument("--sf2", type=Path, default=Path(DEFAULT_SF2),
                        help="サウンドフォント(.sf2)のパス")
    parser.add_argument("--min-velocity", type=int, default=48,
                        help="ノートの最小ベロシティ (デフォルト: 48)")
    parser.add_argument("--track", type=int, default=None,
                        help="MIDI入力時に使うトラック番号 (省略時: 全トラック)")
    parser.add_argument("--melody", action="store_true",
                        help="主旋律だけを単音で抜き出す(動画同期向け)")
    parser.add_argument("--min-pitch", type=int, default=55,
                        help="--melody時にこれ未満の低音を捨てる (MIDIノート番号, デフォルト: 55=G3)")
    parser.add_argument("--gain", type=float, default=0.0,
                        help="出力ゲイン調整dB (デフォルト: 0)")
    parser.add_argument("--reverb", type=float, default=0.6,
                        help="リバーブの部屋サイズ 0-1 (0で無効, デフォルト: 0.6)")
    parser.add_argument("--save-midi", action="store_true",
                        help="中間MIDIファイルも保存する")
    args = parser.parse_args()

    if not args.input.exists():
        sys.exit(f"エラー: 入力ファイルが見つかりません: {args.input}")
    if not args.sf2.exists():
        sys.exit(f"エラー: サウンドフォントが見つかりません: {args.sf2}")

    output = args.output or args.input.with_name(
        f"{args.input.stem}_{args.instrument}.mp3")
    octave = args.octave if args.octave is not None \
        else DEFAULT_OCTAVE.get(args.instrument, 0)

    if args.input.suffix.lower() not in (".mid", ".midi"):
        sys.exit("エラー: 入力はMIDIファイル(.mid)にしてください")
    import pretty_midi
    midi_data = pretty_midi.PrettyMIDI(str(args.input))
    if args.track is not None:
        midi_data.instruments = [midi_data.instruments[args.track]]
    print(f"[1/3] MIDI読み込み: {args.input} "
          f"({sum(len(i.notes) for i in midi_data.instruments)}ノート)")
    if args.melody:
        midi_data = extract_melody(midi_data, args.min_pitch)
    midi_data = restyle(midi_data, INSTRUMENTS[args.instrument],
                        octave, args.min_velocity)

    if args.save_midi:
        midi_path = output.with_suffix(".mid")
        midi_data.write(str(midi_path))
        print(f"      MIDI保存: {midi_path}")

    render(midi_data, args.sf2, output, args.gain, args.reverb)
    print(f"完了: {output}")


if __name__ == "__main__":
    main()
