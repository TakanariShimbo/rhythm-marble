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

# 音色プリセット: 楽器+高域の丸め+ホールリバーブのwet/dampingをセットで指定。
# 参照音源(ホール残響のオルゴール)の分析から出発し、聴感で「上品な余韻」
# レベルまで控えめに追い込んだ確定値。
# musicbox_hallのローパス4.2kHzはFluidR3の金属的なシャリつき除去。
TONES = {
    "celesta_hall":  dict(instrument="celesta",   lowpass=5500,
                          lowpass_stages=3, attack_ms=15, attack_ms_low=45,
                          highpass=200, wet=0.32, damping=0.7),
    "musicbox_hall": dict(instrument="music_box", lowpass=4200,
                          wet=0.32, damping=0.45),
    "kalimba_hall":  dict(instrument="kalimba",   lowpass=6000,
                          wet=0.32, damping=0.45),
}
DEFAULT_TONE = "celesta_hall"
DEFAULT_ROOM = 0.82          # リバーブの部屋サイズ(上品な余韻)

DEFAULT_SF2 = "/usr/share/sounds/sf2/TimGM6mb.sf2"
SAMPLE_RATE = 44100


def extract_melody(midi_data, min_pitch: int = 0, group_ms: float = 80.0,
                   tie: str = "merge"):
    """主旋律だけを単音で抜き出す(スカイライン方式)。

    1. 低すぎるノート(伴奏・ベース)を除外
    2. ほぼ同時に鳴るノート群からは最高音だけを残す
    3. 高音が鳴っている間に始まる下の伴奏ノートを除外
    4. 前の音の尾を次の音の頭で切って完全な単音にする

    tie: 同音で前の音に食い込むノートの扱い。
      merge = 1音に結合(タイ表記のMIDI向け、従来どおり)
      cut   = 結合せず前の音を切り詰める(同音連打を保持。歌モノの
              編集済みMIDIで刻みが消えるのを防ぐ)
    """
    notes = [n for inst in midi_data.instruments for n in inst.notes
             if n.pitch >= min_pitch]
    notes.sort(key=lambda n: (n.start, -n.pitch))

    if tie == "cut":
        # 入力は仕上げ済みの単旋律とみなし、先に全ノートの尾を次の音の頭で
        # クリップする。長い尾が原因の「タイ結合」も「鳴っている最中の
        # 低音を伴奏として捨てる」誤爆も起きなくなる
        for a, b in zip(notes, notes[1:]):
            if a.end > b.start:
                a.end = max(a.start + 0.02, b.start)

    group_s = group_ms / 1000.0
    kept = []
    for n in notes:
        if kept and n.start - kept[-1].start < group_s:
            continue  # 同時打鍵グループ: 最初(=最高音)だけ残す
        if kept and kept[-1].end > n.start and kept[-1].pitch > n.pitch:
            continue  # 上のメロディが鳴っている最中の下の伴奏音は捨てる
        kept.append(n)
    # タイ(同音で前の音に食い込んで続くノート)の扱い。
    # 隙間0で隣接するのは同音連打(きらきら星のドド等)なのでどちらでも残る。
    if tie == "merge":
        merged = []
        for n in kept:
            if merged and n.pitch == merged[-1].pitch \
                    and n.start < merged[-1].end - 0.01:
                merged[-1].end = max(merged[-1].end, n.end)
                continue
            merged.append(n)
        kept = merged
    # tie=cut は後段の「前の音の尾を次の音の頭で切る」処理がそのまま担う

    for a, b in zip(kept, kept[1:]):
        if a.end > b.start:
            a.end = b.start

    if not kept:
        sys.exit("エラー: メロディ抽出後のノートが0件です(--min-pitchが高すぎる等)")
    inst = midi_data.instruments[0]
    midi_data.instruments = [inst]
    inst.notes = kept
    print(f"      メロディ抽出後: {len(kept)}ノート")
    return midi_data


def process_long_notes(midi_data, mode: str, max_len: float):
    """max_lenより長いノートの扱いを決める。

    keep:  そのまま(従来どおり)
    cut:   max_lenで切り詰める(残響で自然に減衰)
    split: オルゴールのトレモロ風にmax_len間隔で刻み直す。
           動画側も同じ処理を通るので、刻んだ分だけバウンドが増えて同期する。
    """
    if mode == "keep":
        return midi_data
    import copy
    n_hit = 0
    for inst in midi_data.instruments:
        new_notes = []
        for n in inst.notes:
            dur = n.end - n.start
            if dur <= max_len:
                new_notes.append(n)
                continue
            n_hit += 1
            if mode == "cut":
                n.end = n.start + max_len
                new_notes.append(n)
            else:  # split
                t, vel = n.start, n.velocity
                while t < n.end - 1e-6:
                    seg = copy.copy(n)
                    seg.start = t
                    seg.end = min(t + max_len, n.end)
                    seg.velocity = max(1, int(vel))
                    new_notes.append(seg)
                    vel *= 0.82  # 繰り返すほど減衰させてトレモロらしく
                    t += max_len
        inst.notes = sorted(new_notes, key=lambda x: x.start)
    if n_hit:
        print(f"      長音処理({mode}, {max_len}s超 {n_hit}音): "
              f"計{sum(len(i.notes) for i in midi_data.instruments)}ノート")
    return midi_data


def trim_leading_silence(midi_data):
    """曲頭の無音をカットする(最初のノートが0秒から始まるように全体をずらす)。

    音源と動画の両方で同じ処理を通すことで同期が保たれる。
    中間の無音はそのまま残す。
    """
    starts = [n.start for inst in midi_data.instruments for n in inst.notes]
    if not starts:
        return midi_data
    shift = min(starts)
    if shift <= 0.01:
        return midi_data
    for inst in midi_data.instruments:
        for n in inst.notes:
            n.start -= shift
            n.end -= shift
    print(f"      曲頭の無音カット: {shift:.2f}s")
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
           reverb: float = 0.0, lowpass: float = 9500,
           wet: float = 0.45, damping: float = 0.25,
           lowpass_stages: int = 1, attack_ms: float = 0.0,
           attack_ms_low: float = None, highpass: float = None):
    """FluidSynthでWAVにレンダリングし、ffmpegでMP3化する。"""
    import numpy as np
    import soundfile as sf

    print(f"[2/3] レンダリング中 (soundfont: {sf2_path.name})")
    audio = midi_data.fluidsynth(fs=SAMPLE_RATE, sf2_path=str(sf2_path))

    # アタック軟化: 各ノートの頭に短いフェードインをかけ、
    # ビー玉が金属に当たったような硬い打撃音を削る(残響・音色は無傷)。
    # 低音の打撃は「ゴツッ」と重く響くので、attack_ms_low指定時は
    # 音程に応じてフェードを延ばす(pitch55でlow、84でattack_msに線形補間)
    if attack_ms > 0:
        lo = attack_ms_low if attack_ms_low else attack_ms
        for inst in midi_data.instruments:
            for note in inst.notes:
                t = min(max((note.pitch - 55) / (84 - 55), 0.0), 1.0)
                fms = lo + (attack_ms - lo) * t
                n_fade = int(SAMPLE_RATE * fms / 1000)
                i = int(note.start * SAMPLE_RATE)
                if 0 <= i and i + n_fade < len(audio):
                    audio[i:i + n_fade] *= np.linspace(0.0, 1.0, n_fade)

    # リバーブ等の空間系エフェクト(心地よい残響)
    if reverb > 0:
        from pedalboard import (Pedalboard, Reverb, LowpassFilter,
                                HighpassFilter)
        # LowpassFilterは6dB/octと緩いので、多段重ねで傾斜を稼ぐ。
        # highpassは低音打撃の「ゴツッ」というこもった芯を除去
        chain = ([HighpassFilter(cutoff_frequency_hz=highpass)]
                 if highpass else [])
        chain += [LowpassFilter(cutoff_frequency_hz=lowpass)
                  for _ in range(max(1, lowpass_stages))]
        chain.append(Reverb(room_size=reverb, damping=damping,
                            wet_level=wet, dry_level=0.65, width=1.0))
        board = Pedalboard(chain)
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
    parser.add_argument("--tone", choices=TONES, default=DEFAULT_TONE,
                        help=f"音色プリセット(楽器+質感+残響のセット。"
                             f"デフォルト: {DEFAULT_TONE})")
    parser.add_argument("-i", "--instrument", choices=INSTRUMENTS,
                        default=None,
                        help="楽器を個別指定(toneの楽器を上書き)")
    parser.add_argument("--octave", type=int, default=None,
                        help="オクターブシフト (デフォルトは音色ごとの推奨値)")
    parser.add_argument("--sf2", type=Path, default=Path(DEFAULT_SF2),
                        help="サウンドフォント(.sf2)のパス")
    parser.add_argument("--min-velocity", type=int, default=48,
                        help="ノートの最小ベロシティ (デフォルト: 48)")
    parser.add_argument("--track", type=str, default=None,
                        help="使うトラック番号。カンマ区切りで複数可 (例: 0,2,5)")
    parser.add_argument("--melody", action="store_true",
                        help="主旋律だけを単音で抜き出す(動画同期向け)")
    parser.add_argument("--min-pitch", type=int, default=55,
                        help="--melody時にこれ未満の低音を捨てる (MIDIノート番号, デフォルト: 55=G3)")
    parser.add_argument("--tie", choices=["merge", "cut"], default="merge",
                        help="同音で食い込むノート: merge=タイとして結合(既定) / "
                             "cut=前を切り詰めて連打を保持(歌モノの編集済みMIDI向け)")
    parser.add_argument("--long-note", choices=["keep", "cut", "split"],
                        default="keep",
                        help="長い音の扱い: keep=そのまま / cut=切り詰め / "
                             "split=トレモロ風に刻む (デフォルト: keep)")
    parser.add_argument("--long-note-len", type=float, default=0.5,
                        help="長音とみなす秒数(cutの上限、splitの刻み間隔。デフォルト: 0.5)")
    parser.add_argument("--max-len", type=float, default=0,
                        help="曲を先頭N秒に切り詰める(無音カット後の時刻基準。"
                             "make_video --durationと同じ境界。0で無効)")
    parser.add_argument("--skip", type=float, default=0,
                        help="曲の先頭N秒を捨てて詰める(無音カット後・元テンポの"
                             "時刻基準。max-lenより後に適用されない=窓は[skip, max-len])")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="テンポ倍率(1.1で1割速く。音程は変わらない。"
                             "make_video --speedと同値にすること)")
    parser.add_argument("--gain", type=float, default=0.0,
                        help="出力ゲイン調整dB (デフォルト: 0)")
    parser.add_argument("--reverb", type=float, default=DEFAULT_ROOM,
                        help="リバーブの部屋サイズ 0-1 "
                             f"(0で無効, デフォルト: {DEFAULT_ROOM})")
    parser.add_argument("--save-midi", action="store_true",
                        help="中間MIDIファイルも保存する")
    args = parser.parse_args()

    if not args.input.exists():
        sys.exit(f"エラー: 入力ファイルが見つかりません: {args.input}")
    if not args.sf2.exists():
        sys.exit(f"エラー: サウンドフォントが見つかりません: {args.sf2}")

    tone = TONES[args.tone]
    instrument = args.instrument or tone["instrument"]
    output = args.output or args.input.with_name(
        f"{args.input.stem}_{instrument}.mp3")
    octave = args.octave if args.octave is not None \
        else DEFAULT_OCTAVE.get(instrument, 0)

    if args.input.suffix.lower() not in (".mid", ".midi"):
        sys.exit("エラー: 入力はMIDIファイル(.mid)にしてください")
    import pretty_midi
    midi_data = pretty_midi.PrettyMIDI(str(args.input))
    if args.track is not None:
        idx = [int(x) for x in str(args.track).split(",")]
        midi_data.instruments = [midi_data.instruments[i] for i in idx]
    print(f"[1/3] MIDI読み込み: {args.input} "
          f"({sum(len(i.notes) for i in midi_data.instruments)}ノート)")
    if args.melody:
        midi_data = extract_melody(midi_data, args.min_pitch, tie=args.tie)
    midi_data = process_long_notes(midi_data, args.long_note,
                                   args.long_note_len)
    midi_data = trim_leading_silence(midi_data)
    if args.max_len > 0:
        # make_video --duration と同じ境界(start <= N)。音の尾は自然に残す
        for inst in midi_data.instruments:
            inst.notes = [n for n in inst.notes if n.start <= args.max_len]
    if args.skip > 0:
        # 窓の先頭を捨てて0秒に詰める
        for inst in midi_data.instruments:
            inst.notes = [n for n in inst.notes if n.start >= args.skip]
            for n in inst.notes:
                n.start -= args.skip
                n.end -= args.skip
    if args.speed != 1.0:
        # テンポ変更(max-len適用後の時間軸を一様に縮尺)
        for inst in midi_data.instruments:
            for n in inst.notes:
                n.start /= args.speed
                n.end /= args.speed
    midi_data = restyle(midi_data, INSTRUMENTS[instrument],
                        octave, args.min_velocity)

    if args.save_midi:
        midi_path = output.with_suffix(".mid")
        midi_data.write(str(midi_path))
        print(f"      MIDI保存: {midi_path}")

    render(midi_data, args.sf2, output, args.gain, args.reverb,
           lowpass=tone["lowpass"], wet=tone["wet"], damping=tone["damping"],
           lowpass_stages=tone.get("lowpass_stages", 1),
           attack_ms=tone.get("attack_ms", 0.0),
           attack_ms_low=tone.get("attack_ms_low"),
           highpass=tone.get("highpass"))
    print(f"完了: {output}")


if __name__ == "__main__":
    main()
