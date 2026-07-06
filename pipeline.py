#!/usr/bin/env python3
"""MIDI → ビー玉バウンド動画の三段パイプライン。

  1. audio:   主旋律を単音で抽出し、音色+リバーブで音源化 → ここで曲チェック
  2. preview: 簡易3Dレンダラーで動きを確認(数十秒)
  3. final:   Blender(Eevee)でフォトリアルに仕上げる(フル尺で20〜40分)

プロジェクト方式: data/<プロジェクト名>/ の中で入力と出力を分ける。

  data/my-song/
    input/    ユーザーが置く: song.mid(必須), wall.json, 画像など
    output/   生成物: audio.mp3, preview.mp4, final.mp4 など

使い方:
  uv run python pipeline.py audio   data/my-song [--track 1] [--play]
  uv run python pipeline.py preview data/my-song [--duration 15]
  uv run python pipeline.py final   data/my-song

audioで指定した設定はoutput/config.jsonに保存され、preview/finalが引き継ぐ。
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


def resolve_project(project: Path):
    """プロジェクトdirから(midi, indir, outdir)を得る。"""
    indir = project / "input"
    mids = sorted(indir.glob("*.mid")) + sorted(indir.glob("*.midi"))
    if not mids:
        sys.exit(f"エラー: {indir} に .mid がありません")
    if len(mids) > 1:
        print(f"注意: MIDIが複数あるため {mids[0].name} を使います")
    outdir = project / "output"
    outdir.mkdir(parents=True, exist_ok=True)
    return mids[0], indir, outdir


def load_config(outdir: Path) -> dict:
    cfg = outdir / "config.json"
    if not cfg.exists():
        sys.exit(f"エラー: 先にフェーズ1を実行してください:\n"
                 f"  uv run python pipeline.py audio {outdir.parent} --track N")
    return json.loads(cfg.read_text())


# ---------------------------------------------------------------- フェーズ1

def list_tracks(midi: Path):
    import pretty_midi
    pm = pretty_midi.PrettyMIDI(str(midi))
    rows = []
    for i, inst in enumerate(pm.instruments):
        if inst.is_drum or len(inst.notes) < 8:
            continue
        ps = [n.pitch for n in inst.notes]
        name = pretty_midi.program_to_instrument_name(inst.program)
        rows.append((i, name, len(inst.notes), min(ps), max(ps)))
    return rows


def cmd_audio(args):
    midi, indir, outdir = resolve_project(args.project)
    args.midi = midi

    if args.track is None:
        # 聴き比べモード: 全トラックを個別にMP3化する
        tracks_dir = outdir / "tracks"
        tracks_dir.mkdir(exist_ok=True)
        rows = list_tracks(args.midi)
        print(f"トラック指定がないので、候補{len(rows)}本を全部音にします:\n")
        for i, name, n, lo, hi in rows:
            out = tracks_dir / f"track{i}.mp3"
            run([sys.executable, ROOT / "convert.py", args.midi,
                 "--track", i, "--melody",
                 "--min-pitch", args.min_pitch,
                 "--min-velocity", args.min_velocity,
                 "-i", args.instrument, "--octave", args.octave,
                 "--reverb", args.reverb, "--sf2", SF2, "-o", out],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"  track{i}: {name:22s} {n:4d}音 音域{lo}-{hi} → {out}")
        print("\n聴き比べ:")
        for i, *_ in rows:
            print(f"  ffplay -nodisp -autoexit {tracks_dir}/track{i}.mp3")
        print(f"\n決めたら: uv run python pipeline.py audio {args.project} --track <番号>")
        return

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
    print(f"良ければ: uv run python pipeline.py preview {args.project}")
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
    midi, indir, outdir = resolve_project(args.project)
    cfg = load_config(outdir)
    out = outdir / "preview.mp4"
    run(mv_args(midi, cfg, args) + ["--audio", outdir / "audio.mp3",
                                    "-o", out])
    print(f"\nフェーズ2完了: {out}")
    print(f"良ければ: uv run python pipeline.py final {args.project}")


def cmd_final(args):
    midi, indir, outdir = resolve_project(args.project)
    cfg = load_config(outdir)
    scene = outdir / "scene.json"
    run(mv_args(midi, cfg, args) + ["--audio", outdir / "audio.mp3",
                                    "-o", "/dev/null", "--export", scene])
    frames = outdir / "frames"
    br = [BPY, ROOT / "blender_render.py", scene, frames,
          "--engine", args.engine, "--samples", args.samples]
    if (indir / "wall.json").exists():
        br += ["--wall", indir / "wall.json"]
    run(br)
    meta = json.loads(scene.read_text())
    delay = meta["audio_delay_ms"]
    dur = meta["duration_s"]
    out = outdir / "final.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error",
         "-framerate", "30", "-i", frames / "f_%04d.png",
         "-i", outdir / "audio.mp3",
         "-af", f"adelay={delay}:all=1,apad", "-t", dur,
         "-c:v", "libx264", "-preset", "fast", "-crf", "19",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", out])
    print(f"\nフェーズ3完了: {out}")


def main():
    ap = argparse.ArgumentParser(description="MIDI→ビー玉バウンド動画(三段方式)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("audio", help="1. 主旋律抽出+音源化(曲チェック)")
    p.add_argument("project", type=Path, help="プロジェクトdir (data/〜)")
    p.add_argument("--track", type=int, default=None,
                   help="メロディのトラック番号(省略時: 全トラックを聴き比べ用に出力)")
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
        p.add_argument("project", type=Path, help="プロジェクトdir (data/〜)")
        p.add_argument("--duration", type=float, default=None,
                       help="先頭N秒だけ(お試し用)")
        if name == "final":
            p.add_argument("--engine", choices=["eevee", "cycles"],
                           default="eevee")
            p.add_argument("--samples", type=int, default=48)
        p.set_defaults(fn=fn)

    args = ap.parse_args()
    if not args.project.exists():
        sys.exit(f"エラー: {args.project} が見つかりません")
    args.fn(args)


if __name__ == "__main__":
    main()
