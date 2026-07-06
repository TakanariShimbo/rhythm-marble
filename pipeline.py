#!/usr/bin/env python3
"""MIDI → ビー玉バウンド動画の二段パイプライン。

  preview: 簡易3Dレンダラーで素早く確認する(数十秒)
  final:   Blender(Eevee)でフォトリアルに仕上げる(フル尺で20〜40分)

使い方:
  uv run python pipeline.py preview input/曲.mid --track 1 [--duration 15]
  uv run python pipeline.py final   input/曲.mid --track 1 [--duration 15]

出力は output/<曲名>/ にまとまる:
  audio.mp3    音源(チェレスタ+リバーブ等)
  scene.json   物理シーン(Blender用)
  preview.mp4  簡易3D確認動画
  frames/      Blenderの連番PNG
  final.mp4    完成動画
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BPY = ROOT / "vendor" / "bpy-venv" / "bin" / "python"
SF2 = ROOT / "vendor" / "FluidR3_GM.sf2"


def run(cmd, **kw):
    print("$", " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def make_audio(midi: Path, outdir: Path, args) -> Path:
    audio = outdir / "audio.mp3"
    if audio.exists() and not args.redo_audio:
        print(f"音源は生成済み: {audio} (作り直すには --redo-audio)")
        return audio
    run([sys.executable, ROOT / "convert.py", midi,
         "--track", args.track, "--melody",
         "-i", args.instrument, "--octave", args.octave,
         "--reverb", args.reverb, "--sf2", SF2, "-o", audio])
    return audio


def common_mv_args(midi: Path, args):
    mv = [sys.executable, ROOT / "make_video.py", midi, "--track", args.track]
    if args.duration:
        mv += ["--duration", args.duration]
    return mv


def cmd_preview(args):
    midi = args.midi
    outdir = ROOT / "output" / midi.stem
    outdir.mkdir(parents=True, exist_ok=True)
    audio = make_audio(midi, outdir, args)
    out = outdir / "preview.mp4"
    run(common_mv_args(midi, args) + ["--audio", audio, "-o", out])
    print(f"\nプレビュー完成: {out}")
    print(f"良ければ: uv run python pipeline.py final {midi} --track {args.track}")


def cmd_final(args):
    midi = args.midi
    outdir = ROOT / "output" / midi.stem
    outdir.mkdir(parents=True, exist_ok=True)
    audio = make_audio(midi, outdir, args)
    scene = outdir / "scene.json"
    run(common_mv_args(midi, args) + ["--audio", audio, "-o", "/dev/null",
                                      "--export", scene])
    frames = outdir / "frames"
    run([BPY, ROOT / "blender_render.py", scene, frames,
         "--engine", args.engine, "--samples", args.samples])
    delay = json.loads(scene.read_text())["audio_delay_ms"]
    out = outdir / "final.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error",
         "-framerate", "30", "-i", frames / "f_%04d.png", "-i", audio,
         "-af", f"adelay={delay}:all=1",
         "-c:v", "libx264", "-preset", "fast", "-crf", "19",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest", out])
    print(f"\n完成: {out}")


def main():
    ap = argparse.ArgumentParser(description="MIDI→ビー玉バウンド動画(二段方式)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("preview", cmd_preview), ("final", cmd_final)]:
        p = sub.add_parser(name)
        p.add_argument("midi", type=Path, help="入力MIDI (input/〜.mid)")
        p.add_argument("--track", type=int, default=0, help="メロディのトラック番号")
        p.add_argument("--duration", type=float, default=None,
                       help="先頭N秒だけ(お試し用)")
        p.add_argument("--instrument", default="celesta", help="音色")
        p.add_argument("--octave", type=int, default=0, help="オクターブシフト")
        p.add_argument("--reverb", type=float, default=0.6, help="リバーブ量 0-1")
        p.add_argument("--redo-audio", action="store_true", help="音源を作り直す")
        if name == "final":
            p.add_argument("--engine", choices=["eevee", "cycles"], default="eevee")
            p.add_argument("--samples", type=int, default=48)
        p.set_defaults(fn=fn)
    args = ap.parse_args()
    if not args.midi.exists():
        sys.exit(f"エラー: {args.midi} が見つかりません")
    args.fn(args)


if __name__ == "__main__":
    main()
