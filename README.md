# rhythm-marble

## 概要

MIDIの楽曲から「音に同期してビー玉がバウンドしながら落ちていく」動画(9:16, 1080×1920, 30fps)を自動生成するパイプライン。

## セットアップ

```bash
git clone <このリポジトリ>
cd rhythm-marble
./setup.sh     # .venv(uv) + vendor/(Blender bpy 約900MB + サウンドフォント 142MB)
```

前提: `uv` / `ffmpeg` / NVIDIA GPU(フェーズ3のレンダリングに使用)。
`vendor/` と `.venv` はいつ消しても `./setup.sh` で再構築できる。

## 使い方

### 0. プロジェクトを作る

```bash
mkdir -p data/my-song/input
cp どこかの曲.mid data/my-song/input/song.mid
```

1プロジェクト=1曲。`input/` に置くのは MIDI(必須)と、任意で
`wall.json`・画像(額に飾るアートワーク)。生成物はすべて `output/` に入る。
サンプル: `data/pokemon-center-gs/`

### 1. audio — 主旋律抽出+音源化(耳でチェック)

```bash
# どのトラックがメロディか分からないとき: 全トラックを聴き比べ用にMP3化
uv run python pipeline.py audio data/my-song
#   → output/tracks/track0.mp3, track1.mp3, ... を聴いて番号を決める

# トラックを決めて本命の音源を作る(--playで即再生)
uv run python pipeline.py audio data/my-song --track 4 --play
```

音の調整(すべてこのフェーズに集約。設定は `output/config.json` に保存され
以降のフェーズが引き継ぐ):

| オプション | 既定 | 意味 |
|---|---|---|
| `--instrument` | celesta | celesta / music_box / kalimba / harp / vibraphone / marimba / glockenspiel / xylophone |
| `--octave` | 0 | オクターブシフト |
| `--reverb` | 0.6 | 残響量 0-1(0で無効)。心地よさの要 |
| `--min-pitch` | 55 | これ未満の低音を捨てる(伴奏・ベース除去) |
| `--min-velocity` | 48 | 弱いノートの底上げ |

出力: `audio.mp3`(音源) / `audio.mid`(抽出された単音メロディ、確認用)

### 2. preview — 簡易3Dで動きチェック(数十秒)

```bash
uv run python pipeline.py preview data/my-song              # フル尺
uv run python pipeline.py preview data/my-song --duration 15  # 先頭だけ
```

物理シミュレーション+板/レール配置を実行し、簡易レンダラーで
`output/preview.mp4` を出力。**配置は決定的**(同じ入力→必ず同じ結果)なので、
ここで見た動きがそのままフェーズ3の動きになる。
衝突検証(めり込みゼロ確認)もここで自動実行される。

### 3. final — フォトリアル仕上げ(フル尺で20〜40分)

```bash
uv run python pipeline.py final data/my-song
uv run python pipeline.py final data/my-song --engine cycles  # 最高品質(数時間)
```

Blender(Eevee)でレンダリングして `output/final.mp4` を出力。
`--duration 10` で冒頭だけ試すこともできる。

## wall.json — 壁の演出(額・タイトル)

`input/wall.json` に書く。パスは `input/` からの相対でよい。

```json
{
  "texts": [
    {"text": "ポケットモンスター金銀\nポケモンセンター",
     "at": "start", "dy": -0.72, "size": 0.155}
  ],
  "frames": [
    {"file": "artwork.jpg", "at": "start", "dy": 1.0, "width": 0.95}
  ]
}
```

- `frames` = 額に入れて壁に飾る画像。**どんな画像でもそのまま使える**
  (切り抜き不要)。`title`を付けると額の下に真鍮の銘板が付く
- `texts` = 壁に直接刻印されるメタリックブラックの文字(`\n`で複数行可)
- `at`: `"start"`(ビー玉が出てくる壁の位置) / `"first_plate"` / `[x, y]`座標
- `dy`: 上下オフセット(m) / `size`: 文字の高さ(m) / `width`: 額の画像幅(m)

構図の目安: 額(`dy +1.0`)/ 壁割れハッチ(start)/ タイトル(`dy -0.72`)で
0秒フレームがサムネイルとして成立する。

## その他のコマンド

```bash
# 衝突検証だけ実行(レンダリングなし)
uv run python make_video.py data/my-song/input/song.mid --track 4 \
    --audio x -o /dev/null --check

# 1フレームだけ描いて見た目確認(壁の調整に便利)
vendor/bpy-venv/bin/python blender_render.py \
    data/my-song/output/scene.json /tmp/chk --frame 0 --engine eevee \
    --wall data/my-song/input/wall.json
```

## 仕組み

```
input/song.mid
  │ convert.py        トラック選択 → 単音メロディ抽出(スカイライン) →
  │                   GM音色に差し替え → FluidSynth → リバーブ → audio.mp3
  │ make_video.py     物理シミュレーション+板/レール配置+衝突検証
  │                     ├ preview: 簡易3Dレンダラー(PIL) → preview.mp4
  │                     └ final:   scene.json 書き出し
  │ blender_render.py Blender(bpy+Eevee/Cycles)フォトリアル描画 → frames/
  └ ffmpeg            音声と合成 → final.mp4

pipeline.py が上記を audio / preview / final の3コマンドに束ねる
```

### 設計の要点

- **音の時刻が正**: ノート発音時刻のボール位置に板を置く。物理エンジンに
  任せると音との同期が取れないため、逆に「板の傾き」だけを設計変数にする
- **物理は本物**: 重力+空気抵抗(終端速度)+反発係数一定の反射。
  跳ね返りでエネルギーが増えることはない。回転も慣性を持つ転がりモデル
- **配置は決定的な探索**: 81angleを網羅評価し、すり抜け・板同士のめり込みを
  弾く。行き詰まったらバックトラック。同じ入力なら必ず同じ配置
- **休符はレール**: 0.75秒超の休符は無音のゴムレール(反発ゼロ=転がり)で
  落下速度を抑える。音が鳴るのは金属板だけ、という素材の意味論
- **検証器**: find_collisions() が最終軌道×全パーツのめり込みを機械検証
- **描画は分離**: scene.json 経由。簡易レンダラーとBlenderが同じデータを描く
  ので、プレビューと完成品の動きは同一

## リポジトリ構成

```
pipeline.py          3フェーズの入口
convert.py           MIDI→音源(フェーズ1の中身)
make_video.py        物理+配置+検証+簡易レンダラー(フェーズ2の中身)
blender_render.py    フォトリアル描画(フェーズ3の中身)
setup.sh             vendor/ と .venv の再構築
data/<プロジェクト>/  input/(ユーザー) と output/(生成物)
data/pokemon-center-gs/  サンプルプロジェクト(inputのみgit管理)
vendor/              Blender(bpy)とサウンドフォント(git対象外・再構築可)
```

