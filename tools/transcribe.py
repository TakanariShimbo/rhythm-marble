#!/usr/bin/env python
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "piano-transcription-inference>=0.0.5",
#   "torch>=2.0",
#   "librosa>=0.10",
#   "pretty-midi>=0.2.10",
# ]
# ///
"""前処理ツール: ピアノ系の音源(mp3/wav/mp4/webm等)からMIDIを自動採譜する。

ByteDanceの高精度ピアノ採譜モデル(ノートF1≈0.97)を使う。ピアノ以外の
音源でも動くが精度は落ちる。初回はモデル(約170MB)を自動ダウンロードする。

このスクリプトは依存(torch等)をインラインメタデータで宣言しているので、
プロジェクトの環境を汚さず `uv run tools/transcribe.py ...` だけで動く。

使い方:
  uv run tools/transcribe.py <音源> -o data/my-song/input/song.mid
  → track0=メロディ候補(スカイライン) / track1=残り の2トラックMIDI。
    tools/midi_editor.html で開いてメロディを仕上げてから pipeline へ。
"""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


def extract_wav(src: Path, dst: Path):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                    "-ac", "1", str(dst)], check=True)


def split_skyline(midi_path: Path, group_ms: float = 80.0):
    """採譜結果を track0=メロディ候補(各瞬間の最高音) / track1=残り に再構成。

    ざっくりした出発点を作るのが目的(仕上げはmidi_editor.htmlで)。
    """
    import pretty_midi
    pm = pretty_midi.PrettyMIDI(str(midi_path))
    notes = sorted((n for i in pm.instruments for n in i.notes),
                   key=lambda n: n.start)
    out = pretty_midi.PrettyMIDI()
    mel = pretty_midi.Instrument(program=0, name="melody")
    acc = pretty_midi.Instrument(program=0, name="accomp")
    group = []

    def flush():
        if not group:
            return
        g = sorted(group, key=lambda n: n.pitch, reverse=True)
        mel.notes.append(g[0])
        acc.notes.extend(g[1:])
        group.clear()

    for n in notes:
        if group and n.start - group[0].start > group_ms / 1000:
            flush()
        group.append(n)
    flush()
    out.instruments.extend([mel, acc])
    out.write(str(midi_path))
    return len(mel.notes), len(acc.notes)


def main():
    ap = argparse.ArgumentParser(description="音源→MIDI自動採譜(前処理ツール)")
    ap.add_argument("input", type=Path, help="音源(mp3/wav/mp4/webm/m4a等)")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="出力MIDIパス (省略時: <入力名>.mid)")
    ap.add_argument("--no-split", action="store_true",
                    help="メロディ候補/残りの2トラック分割をしない")
    args = ap.parse_args()
    if not args.input.exists():
        sys.exit(f"エラー: 入力ファイルが見つかりません: {args.input}")
    output = args.output or args.input.with_suffix(".mid")
    output.parent.mkdir(parents=True, exist_ok=True)

    import librosa
    import torch
    from piano_transcription_inference import PianoTranscription, sample_rate

    print(f"[1/3] 音声抽出: {args.input.name}")
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "audio.wav"
        extract_wav(args.input, wav)
        audio, _ = librosa.load(str(wav), sr=sample_rate, mono=True)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[2/3] 採譜中 (ByteDance piano model, device={device}。"
          "CPUだと曲の2〜3倍の時間)")
    tr = PianoTranscription(device=device)
    result = tr.transcribe(audio, str(output))
    n = len(result["est_note_events"])
    if n == 0:
        sys.exit("エラー: ノートを検出できませんでした")

    if args.no_split:
        print(f"完了: {output} ({n}ノート、1トラック)")
    else:
        print("[3/3] メロディ候補(スカイライン)と残りに分割")
        nm, na = split_skyline(output)
        print(f"完了: {output}")
        print(f"  track0 melody候補 {nm}ノート / track1 accomp {na}ノート")
    print(f"仕上げ: tools/midi_editor.html で {output} を開いて編集")


if __name__ == "__main__":
    main()
