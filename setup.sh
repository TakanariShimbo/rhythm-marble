#!/bin/bash
# vendor/ の再構築(消してもこれ一発で戻る)
set -e
cd "$(dirname "$0")"
mkdir -p vendor
if [ ! -f vendor/FluidR3_GM.sf2 ]; then
  echo "サウンドフォントをダウンロード中..."
  curl -sL -o vendor/FluidR3_GM.sf2 \
    "https://github.com/pianobooster/fluid-soundfont/releases/download/v3.1/FluidR3_GM.sf2"
fi
if [ ! -e vendor/bpy-venv/bin/python ]; then
  echo "Blender(bpy)をインストール中(約900MB)..."
  uv venv vendor/bpy-venv --python 3.11
  uv pip install --python vendor/bpy-venv/bin/python bpy numpy
fi
uv sync   # メインのPython環境
echo "セットアップ完了"
