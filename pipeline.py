#!/usr/bin/env python3
"""MIDI → ビー玉バウンド動画の三段パイプライン。

  1. audio:   主旋律を単音で抽出し、音色+リバーブで音源化 → ここで曲チェック
  2. preview: 簡易3Dレンダラーで動きを確認(数十秒)
  3. final:   Blender(Eevee)でフォトリアルに仕上げる(フル尺で20〜40分)

使い方:
  uv run python pipeline.py audio   input/曲.mid --track 1 [--instrument celesta --reverb 0.6] [--play]
  uv run python pipeline.py preview input/曲.mid [--duration 15]
  uv run python pipeline.py final   input/曲.mid

audioで指定した設定(トラック・音色など)は output/<曲名>/config.json に
保存され、preview / final はそれを引き継ぐ。

出力は output/<曲名>/ にまとまる:
  audio.mp3    音源(曲チェック用にして最終版の音)
  audio.mid    抽出された単音メロディMIDI(参考)
  config.json  フェーズ1で決めた設定
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


def outdir_for(midi: Path) -> Path:
    d = ROOT / "output" / midi.stem
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_config(outdir: Path) -> dict:
    cfg = outdir / "config.json"
    if not cfg.exists():
        sys.exit(f"エラー: 先にフェーズ1を実行してください:\n"
                 f"  uv run python pipeline.py audio input/{outdir.name}.mid --track N")
    return json.loads(cfg.read_text())


# ---------------------------------------------------------------- フェーズ1

def cmd_audio(args):
    outdir = outdir_for(args.midi)
    audio = outdir / "audio.mp3"
    run([sys.executable, ROOT / "convert.py", args.midi,
         "--track", args.track, "--melody",
         "--min-pitch", args.min_pitch,
         "--min-velocity", args.min_velocity,
         "-i", args.instrument, "--octave", args.octave,
         "--reverb", args.reverb, "--sf2", SF2,
         "--save-midi", "-o", audio])
    (outdir / "config.json").write_text(json.dumps({
        "track": args.track,
        "instrument": args.instrument,
        "octave": args.octave,
        "reverb": args.reverb,
        "min_pitch": args.min_pitch,
        "min_velocity": args.min_velocity,
    }, ensure_ascii=False, indent=2))
    print(f"\nフェーズ1完了: {audio}")
    print("曲チェック: ffplay -nodisp -autoexit " + str(audio))
    print(f"良ければ: uv run python pipeline.py preview {args.midi}")
    if args.play:
        subprocess.run(["ffplay", "-nodisp", "-autoexit", "-loglevel", "error",
                        str(audio)])


# ---------------------------------------------------------------- フェーズ2/3

def mv_args(midi: Path, cfg: dict, args):
    mv = [sys.executable, ROOT / "make_video.py", midi,
          "--track", cfg["track"]]
    if args.duration:
        mv += ["--duration", args.duration]
    return mv


def cmd_preview(args):
    outdir = outdir_for(args.midi)
    cfg = load_config(outdir)
    out = outdir / "preview.mp4"
    run(mv_args(args.midi, cfg, args) + ["--audio", outdir / "audio.mp3",
                                         "-o", out])
    print(f"\nフェーズ2完了: {out}")
    print(f"良ければ: uv run python pipeline.py final {args.midi}")


def cmd_final(args):
    outdir = outdir_for(args.midi)
    cfg = load_config(outdir)
    scene = outdir / "scene.json"
    run(mv_args(args.midi, cfg, args) + ["--audio", outdir / "audio.mp3",
                                         "-o", "/dev/null", "--export", scene])
    frames = outdir / "frames"
    run([BPY, ROOT / "blender_render.py", scene, frames,
         "--engine", args.engine, "--samples", args.samples])
    delay = json.loads(scene.read_text())["audio_delay_ms"]
    out = outdir / "final.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error",
         "-framerate", "30", "-i", frames / "f_%04d.png",
         "-i", outdir / "audio.mp3",
         "-af", f"adelay={delay}:all=1",
         "-c:v", "libx264", "-preset", "fast", "-crf", "19",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest", out])
    print(f"\nフェーズ3完了: {out}")


def main():
    ap = argparse.ArgumentParser(description="MIDI→ビー玉バウンド動画(三段方式)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("audio", help="1. 主旋律抽出+音源化(曲チェック)")
    p.add_argument("midi", type=Path)
    p.add_argument("--track", type=int, default=0, help="メロディのトラック番号")
    p.add_argument("--instrument", default="celesta",
                   help="音色: celesta/music_box/kalimba/harp/vibraphone/marimba等")
    p.add_argument("--octave", type=int, default=0, help="オクターブシフト")
    p.add_argument("--reverb", type=float, default=0.6,
                   help="リバーブ(残響)の量 0-1、0で無効")
    p.add_argument("--min-pitch", type=int, default=55,
                   help="これ未満の低音を捨てる(MIDIノート番号)")
    p.add_argument("--min-velocity", type=int, default=48,
                   help="弱いノートの底上げ")
    p.add_argument("--play", action="store_true", help="生成後すぐ再生する")
    p.set_defaults(fn=cmd_audio)

    for name, fn, help_ in [("preview", cmd_preview, "2. 簡易3Dで動き確認"),
                            ("final", cmd_final, "3. フォトリアル仕上げ")]:
        p = sub.add_parser(name, help=help_)
        p.add_argument("midi", type=Path)
        p.add_argument("--duration", type=float, default=None,
                       help="先頭N秒だけ(お試し用)")
        if name == "final":
            p.add_argument("--engine", choices=["eevee", "cycles"],
                           default="eevee")
            p.add_argument("--samples", type=int, default=48)
        p.set_defaults(fn=fn)

    args = ap.parse_args()
    if not args.midi.exists():
        sys.exit(f"エラー: {args.midi} が見つかりません")
    args.fn(args)


if __name__ == "__main__":
    main()
