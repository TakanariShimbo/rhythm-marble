# rhythm-marble

## 概要

MIDIの楽曲から「音に同期してビー玉がバウンドしながら落ちていく」動画(9:16, 1080×1920, 30fps)を自動生成するパイプライン。

## デモ

サンプルプロジェクト `data/twinkle-star/`(きらきら星)から生成した動画:

<a href="https://youtube.com/shorts/8o9QeMwsXNY"><img src="https://img.youtube.com/vi/8o9QeMwsXNY/maxresdefault.jpg" width="270" alt="Twinkle, Twinkle, Little Star – Rhythm Marble (YouTube Shorts)"></a>

▶ https://youtube.com/shorts/8o9QeMwsXNY

## セットアップ

```bash
git clone <このリポジトリ>
cd rhythm-marble
./setup.sh     # .venv(uv) + vendor/(Blender bpy 約900MB + サウンドフォント 142MB)
```

前提: `uv` / `ffmpeg` / NVIDIA GPU(フェーズ3のレンダリングに使用)。
`vendor/` と `.venv` はいつ消しても `./setup.sh` で再構築できる。

## 使い方

パイプライン本体(audio → preview → final)は「MIDIから」始まる。
手元の素材に応じて、任意の**前処理ツール**(`tools/`)を手前に挟む:

```
パターンA: 完成したMIDIがある
  song.mid ──────────────────────────────► pipeline.py audio → preview → final

パターンB: MIDIはあるがメロディが混沌(多声・伴奏混在)
  song.mid ──► tools/midi_editor.html ───► pipeline.py audio → ...
               (メロディ/伴奏を手で仕分け)

パターンC: ピアノ演奏の音源(mp3/mp4等)しかない
  音源 ──► tools/transcribe.py ──► tools/midi_editor.html ──► pipeline.py audio → ...
          (自動採譜)              (仕上げ)
```

### 0. プロジェクトを作る

```bash
mkdir -p data/my-song/input
cp どこかの曲.mid data/my-song/input/song.mid
```

1プロジェクト=1曲。`input/` に置くのは MIDI(必須)と、任意で
`wall.json`・画像(額に飾るアートワーク)。生成物はすべて `output/` に入る。
サンプル: `data/twinkle-star/`(きらきら星)

```bash
# サンプルをそのまま動かす
uv run python pipeline.py audio   data/twinkle-star --track 0
uv run python pipeline.py preview data/twinkle-star
uv run python pipeline.py final   data/twinkle-star
```

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
| `--tone` | celesta_hall | 音色プリセット(楽器+高域の丸め+残響のセット): celesta_hall / musicbox_hall / kalimba_hall |
| `--instrument` | (toneに従う) | 楽器を個別指定してtoneの楽器だけ上書き: celesta / music_box / kalimba / harp / vibraphone / marimba / glockenspiel / xylophone |
| `--octave` | 0 | オクターブシフト。メロディの中央値がC5前後になると映える |
| `--reverb` | (toneに従う=0.82) | 残響の部屋サイズ 0-1(0で無効)。心地よさの要 |
| `--max-len` | なし | 曲を先頭N秒に切り詰める(preview/finalにも自動反映) |
| `--min-pitch` | 55 | これ未満の低音を捨てる(伴奏・ベース除去) |
| `--min-velocity` | 48 | 弱いノートの底上げ |
| `--long-note` | keep | 長い音の扱い: keep=そのまま / cut=切り詰め / split=トレモロ風に刻む(動画のバウンドも増える) |
| `--long-note-len` | 0.5 | 長音の閾値秒数(cutの上限、splitの刻み間隔) |

音色プリセットは「オルゴール調の澄んだ単音+上品なホール余韻」を
実音源の分析から追い込んだ確定値(wet 0.32 / damping 0.45 / room 0.82)。
まず既定の celesta_hall で聴き、キャラクターを変えたいときだけ
musicbox_hall(よりオルゴール的) / kalimba_hall(より丸く素朴)を試すとよい。

自動で行われる整形: タイ(前の音に食い込んで続く同音ノート)の結合、
曲頭の無音カット。どちらも音源と動画で同じ処理を通るため同期は保たれる。

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

Blender(Eevee)でレンダリングした後、映像ポスト処理(カラーグレーディング+
コントラスト+情報量スポット+弱ブルーム+ビネット+シャープ)を全フレームに
並列適用してから `output/final.mp4` にエンコードする。
`--duration 10` で冒頭だけ試すこともできる。

- ポスト処理のプリセットは `--postfx` で変更(`none` でスキップ)。
  中身とパラメータは `postfx_lab.py` の `PRESETS` を参照
- 加工前フレームは `output/frames/`、加工後は `output/frames_fx/` に残るので、
  音声だけ差し替えたいときは ffmpeg の再多重化だけで済む(再レンダリング不要)

ポスト処理の調整は試作モードが便利(代表フレームを自動選定して
プリセット比較シートを作る):

```bash
uv run python postfx_lab.py data/my-song/output/frames -o /tmp/fxlab
```

## wall.json — 壁の演出(額・タイトル)

`input/wall.json` に書く。パスは `input/` からの相対でよい。

```json
{
  "texts": [
    {"text": "Twinkle, Twinkle,\nLittle Star",
     "at": "start", "dy": -0.75, "size": 0.155}
  ],
  "frames": [
    {"file": "starry_sky.jpg", "at": "start", "dy": 0.95, "width": 0.95}
  ],
  "lights": ["#fff3cc", "#8fa8e0"],
  "marble_colors": ["#1c3a73", "#ffd98c", "#e8f0ff", "#0e1f4d"],
  "lights_energy": 110,
  "marble_glow": 1.8
}
```

- `frames` = 額に入れて壁に飾る画像。**どんな画像でもそのまま使える**
  (切り抜き不要)。`title`を付けると額の下に真鍮の銘板が付く。
  各額には美術館風のスポットライトが自動で当たる
- `texts` = 壁に直接刻印されるメタリックブラックの文字(`\n`で複数行可)。
  サムネイルで読ませたい銘板は `size: 0.24` 程度が目安
- `at`: `"start"`(ビー玉が出てくる壁の位置) / `"end"` / `"first_plate"` / `[x, y]`座標
- `dy`: 上下オフセット(m) / `size`: 文字の高さ(m) / `width`: 額の画像幅(m)
- `lights`: 経路沿いの照明色(HEX、交互に配置) / `marble_colors`: ビー玉の渦の色4つ
  (未指定ならライト色から自動導出)
- 明るさ・演出の微調整キー(いずれも任意):
  `lights_energy`(経路照明の強さ、既定55) / `marble_glow`(ビー玉の発光、既定1.1) /
  `frame_spot_energy`(額のスポット、既定120、0で無効) /
  `bloom`(既定0.35) / `vignette`(既定0.30)

構図の目安: 額(`dy +1.0`)/ 壁割れハッチ(start)/ タイトル(`dy -0.72`)で
0秒フレームがサムネイルとして成立する。

## 前処理ツール (tools/)

### tools/transcribe.py — 音源→MIDI自動採譜

ピアノ系の音源(mp3/wav/mp4/webm等)からMIDIを起こす。ByteDanceの
高精度ピアノ採譜モデル(ノートF1≈0.97)を使用。依存(torch等)はスクリプト内の
インラインメタデータで宣言されており、プロジェクトの環境には入らない。

```bash
uv run tools/transcribe.py 演奏動画.mp4 -o data/my-song/input/song.mid
```

出力は track0=メロディ候補(各瞬間の最高音) / track1=残り の2トラック。
初回はモデル(約170MB)を自動ダウンロード。GPUがあれば速い(CPUだと曲の2〜3倍の時間)。

### tools/midi_editor.html — MIDI編集GUI

ブラウザで開くだけで動くピアノロールエディタ(依存なし)。採譜結果の
メロディ仕分けや、既存MIDIの手直しに使う。

- ノートの選択/移動/伸縮/追加/削除、全選択、戻す/進む
- 選択を**メロディ(金)/伴奏(青)**に振り分け → 書き出すと track0=メロディ になり
  そのまま `pipeline.py audio --track 0` に流せる
- 「長さ統一」で選択ノートを左詰めで一定長に(減衰楽器では長さは音に影響しない)
- celesta_hall相当の音で再生確認(メロディのみ再生も可)。操作方法は❔ボタン

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
  │ postfx_lab.py     映像ポスト処理(E_refined) → frames_fx/
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
postfx_lab.py        映像ポスト処理: 試作(比較シート)と全フレーム適用
tools/transcribe.py  前処理: 音源→MIDI自動採譜(依存は独立、uv runで自動解決)
tools/midi_editor.html 前処理: ブラウザで動くMIDI編集GUI(メロディ仕分け)
setup.sh             vendor/ と .venv の再構築
data/<プロジェクト>/  input/(ユーザー) と output/(生成物)
data/twinkle-star/   サンプルプロジェクト(inputのみgit管理)
vendor/              Blender(bpy)とサウンドフォント(git対象外・再構築可)
```

