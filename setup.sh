#!/bin/bash
# vendor/ の再構築(消してもこれ一発で戻る)
set -e
cd "$(dirname "$0")"

# ---- システム依存の事前チェック(なくても続行するが警告する) ----
command -v ffmpeg >/dev/null || \
  echo "⚠ ffmpeg が見つかりません: sudo apt install ffmpeg"
ldconfig -p 2>/dev/null | grep -q libfluidsynth || \
  echo "⚠ libfluidsynth が見つかりません(フェーズ1が動きません): sudo apt install libfluidsynth3"
[ -f /usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc ] || \
  echo "⚠ Noto CJKフォントが見つかりません(銘板の日本語が化けます): sudo apt install fonts-noto-cjk"
mkdir -p vendor
if [ ! -f vendor/FluidR3_GM.sf2 ]; then
  echo "サウンドフォントをダウンロード中..."
  curl -sL -o vendor/FluidR3_GM.sf2 \
    "https://github.com/pianobooster/fluid-soundfont/releases/download/v3.1/FluidR3_GM.sf2"
fi
if [ ! -f vendor/FluidR3_GM_tuned.sf2 ]; then
  echo "サウンドフォントを調律中(celestaの調律ずれ修正)..."
  uv run python tools/tune_sf2.py
fi
if [ ! -e vendor/bpy-venv/bin/python ]; then
  echo "Blender(bpy)をインストール中(約900MB)..."
  uv venv vendor/bpy-venv --python 3.11
  uv pip install --python vendor/bpy-venv/bin/python "bpy==5.0.*" numpy
fi
uv sync   # メインのPython環境
echo "セットアップ完了"
