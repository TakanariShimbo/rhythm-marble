#!/usr/bin/env python
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "yt-dlp>=2025.1.1",
# ]
# ///
"""前処理ツール: YouTube等の動画サイトから音声をMP3でダウンロードする。

yt-dlp(youtube-dlの後継、事実上の標準)を使う。MP3化にはffmpegが必要
(このプロジェクトのセットアップ済み環境なら入っている)。

このスクリプトは依存をインラインメタデータで宣言しているので、
プロジェクトの環境を汚さず `uv run tools/download.py ...` だけで動く。

使い方:
  # ダウンロードして downloads/<動画タイトル>.mp3 に保存
  uv run tools/download.py "https://www.youtube.com/watch?v=XXXX"

  # 保存先を指定
  uv run tools/download.py "https://..." -o data/my-song/input/source.mp3

  # プロジェクトを作って直接配置(採譜の前段として)
  uv run tools/download.py "https://..." --project my-song
  → data/my-song/input/source.mp3 に保存。続きは:
     uv run tools/transcribe.py data/my-song/input/source.mp3 \
         -o data/my-song/input/song.mid
"""
import argparse
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description="YouTube等から音声をMP3で取得")
    ap.add_argument("url", help="動画URL")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="出力MP3パス (省略時: downloads/<タイトル>.mp3)")
    ap.add_argument("--project", type=str, default=None,
                    help="data/<name>/input/source.mp3 に保存(プロジェクト新規作成)")
    ap.add_argument("--bitrate", type=str, default="192",
                    help="MP3ビットレート kbps (デフォルト: 192)")
    args = ap.parse_args()

    if args.project and args.output:
        sys.exit("エラー: -o と --project は同時に指定できません")

    if args.project:
        outdir = Path("data") / args.project / "input"
        outdir.mkdir(parents=True, exist_ok=True)
        outtmpl = str(outdir / "source.%(ext)s")
    elif args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # yt-dlpは拡張子を付け替えるのでstemを渡す
        outtmpl = str(args.output.with_suffix("")) + ".%(ext)s"
    else:
        Path("downloads").mkdir(exist_ok=True)
        outtmpl = "downloads/%(title)s.%(ext)s"

    import yt_dlp

    opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": args.bitrate,
        }],
        "noplaylist": True,          # プレイリストURLでも1本だけ
        "quiet": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(args.url, download=True)

    # 実際の出力パスを表示(後段のコマンドにコピペしやすいように)
    out = Path(ydl.prepare_filename(info)).with_suffix(".mp3")
    print(f"\n完了: {out}")
    print(f"  タイトル: {info.get('title')}")
    print(f"  長さ: {info.get('duration')}秒")
    if args.project:
        print(f"次: uv run tools/transcribe.py {out} "
              f"-o data/{args.project}/input/song.mid")


if __name__ == "__main__":
    main()
