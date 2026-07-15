# rhythm-marble

## 概要

MIDIの楽曲から「音に同期してビー玉がバウンドしながら落ちていく」動画(9:16, 1080×1920, 30fps)を自動生成するパイプライン。

## デモ

サンプルプロジェクト `data/twinkle-star/`(きらきら星)から生成した動画:

<a href="https://youtube.com/shorts/EEdEKNEo22o"><img src="https://img.youtube.com/vi/EEdEKNEo22o/maxresdefault.jpg" width="270" alt="Twinkle, Twinkle, Little Star – Rhythm Marble (YouTube Shorts)"></a>

▶ https://youtube.com/shorts/EEdEKNEo22o

## セットアップ

```bash
# 1. システム依存(Ubuntu/Debian系)
sudo apt install ffmpeg libfluidsynth3 fonts-noto-cjk

# 2. uv (未導入なら)  https://docs.astral.sh/uv/
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3. このリポジトリ
git clone <このリポジトリ>
cd rhythm-marble
./setup.sh     # .venv(uv) + vendor/(Blender bpy 約900MB + サウンドフォント 142MB)
```

- `ffmpeg`: 動画/音声の変換全般 / `libfluidsynth3`: MIDIの音源化 /
  `fonts-noto-cjk`: 銘板の日本語表示(ないとエラーにならず文字化けするので注意)
- NVIDIA GPU 推奨(フェーズ3のレンダリングと採譜ツールが速くなる。なくても動く)
- `vendor/` と `.venv` はいつ消しても `./setup.sh` で再構築できる

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

パターンD: 音源がYouTube上にある
  URL ──► tools/download.py ──► (以降パターンCと同じ)
         (yt-dlpでMP3取得)
```

```bash
# YouTubeからMP3を取得(downloads/<タイトル>.mp3 に保存)
uv run tools/download.py "https://www.youtube.com/watch?v=XXXX"

# プロジェクトを作って直接配置(→ そのまま採譜へ)
uv run tools/download.py "https://..." --project my-song
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
| `--speed` | 1.0 | テンポ倍率(1.1で10%速く。音程は変わらない) |
| `--max-len` | なし | 曲を先頭N秒に切り詰める(preview/finalにも自動反映)。**曲頭の無音カット後**の時間で数える点に注意 |
| `--skip` | 0 | 曲の先頭N秒を捨てる(イントロカット) |
| `--tie` | merge | 前の音に食い込む同音ノートの扱い: merge=1音に結合 / cut=前の音を切って連打を残す |
| `--min-pitch` | 55 | これ未満の低音を捨てる(伴奏・ベース除去) |
| `--min-velocity` | 48 | 弱いノートの底上げ |
| `--long-note` | keep | 長い音の扱い: keep=そのまま / cut=切り詰め / split=トレモロ風に刻む(動画のバウンドも増える) |
| `--long-note-len` | 0.5 | 長音の閾値秒数(cutの上限、splitの刻み間隔) |

音色プリセットは「オルゴール調の澄んだ単音+上品なホール余韻」を
実音源の分析から追い込んだ確定値(wet 0.32 / damping 0.7 / room 0.82、
高域はLP5500Hz×3段で丸め、低域はHP200Hzで打撃の芯を除去)。
さらにノートごとに音程依存のアタック軟化(高音15ms〜低音45msのフェードイン)を
かけて「ビー玉が金属に当たったような硬い音」を抑えている。
まず既定の celesta_hall で聴き、キャラクターを変えたいときだけ
musicbox_hall(よりオルゴール的) / kalimba_hall(より丸く素朴)を試すとよい。

自動で行われる整形: タイの結合(`--tie`)、曲頭の無音カット。
音源・動画とも同じ整形を通るため同期は保たれる。

音源は**調律済みサウンドフォント**(`vendor/FluidR3_GM_tuned.sf2`)を使う。
FluidR3のcelestaはサンプルゾーン単位で調律ずれがあり(84-89が+10セント等、実測)、
メロディがゾーン境界をまたぐと「半音ずれた」ように聞こえる。
`tools/tune_sf2.py`(setup.shが自動実行)がサンプルヘッダのpitchCorrectionだけを
書き換えた調律済みコピーを生成し、全鍵±1セント以内に揃える(波形は無傷)。

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

### 3. final — フォトリアル仕上げ(1分曲で1時間前後)

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

### 夜モード — "mood": "night"

`wall.json` に `"mood": "night"` を足すと、同じシーンが夜のルックで描かれる。
フル版=昼(明るいギャラリー) / サビ版=夜(闇の中で光る仕掛け)という
シリーズの対比を作るためのスイッチ。

物体の素材は昼と同じまま、**照明の当て方だけ**を変える(自己発光には
頼らない — 金属の質感と影が生きる):

- 環境光・壁が深夜の紺〜紫に。キーライトは青白い**月光**になり、
  小さく強め(既定180W)なのでビー玉や板がシャープな影を壁に落とす
- ビー玉に暖色のポイントライトが追随し、通り道が接近で浮かび上がる
- 板1枚ごと・レールごとに小さな薄暗いスポット(夜間美術館の個別照明)。
  板の出現に合わせて点灯・消灯する
- 経路照明(lights)は残る(既定40W) — 近くを通る影が伸び縮みする動的な影用
- 壁の文字は行灯風の発光素材になる(黒文字は夜に沈むため)
- 額のスポットライトはそのまま(暗闇に額が浮かぶ=サムネイル向き)

夜専用の調整キー(いずれも任意):
`moon_energy`(月光、既定180。影の濃さ) /
`marble_light`(追随ライト、既定24。減らすほど闇が深い) /
`plate_spot`(板の個別スポット、既定10、0で無効) /
`rail_spot`(レール、既定8、0で無効)。
夜は `lights_energy` の既定が40、`marble_glow` の既定が2.2 に変わる。
確定レシピの例(やさしさサビ版): moon 180 / marble_light 12 /
plate_spot 6 / rail_spot 5 / lights_energy 45 / bloom 0.5。

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
- 選択を**メロディ(金)としてマーク** → 書き出すと track0=メロディ になり
  そのまま `pipeline.py audio --track 0` に流せる(元のトラック構造は壊さない)
- トラックパネルで色変更・表示/非表示・ミュート
- タップ修正モード: リズムを叩いてノートのタイミングを打ち直せる(3秒助走付き)
- 「長さ統一」で選択ノートを左詰めで一定長に(減衰楽器では長さは音に影響しない)
- celesta_hall相当の音で再生確認(メロディのみ再生も可、先読みスケジューラで
  大曲でも固まらない)。操作方法は❔ボタン

## よくある操作レシピ

### 音だけ変える(オクターブ・音色など) — 再レンダリング不要

オクターブや音色は映像に影響しない(シーンはノートの時刻から作られ、
色は音程クラス基準で不変)。audioを作り直してfinalに音を差し替えるだけでよい:

```bash
uv run python pipeline.py audio data/my-song --track 0 --octave 1  # 他の設定も忘れず引き継ぐこと
# scene.json の audio_delay_ms / duration_s を使って再多重化
ffmpeg -y -i output/final.mp4 -i output/audio.mp3 -map 0:v -map 1:a -c:v copy \
    -af "adelay=2496:all=1,apad" -t <duration_s> -c:a aac -b:a 192k out.mp4
```

**注意**: audioの再生成時は `output/config.json` の全設定(特に `--speed` と
`--min-pitch`)を引き継ぐこと。落とすと音と映像がずれる/音数が変わる。
逆に**速度やMIDIの変更は映像から作り直し**(フル再レンダリング)になる。

### サビ版(夜)を作る

フル版とは別プロジェクトにする(例: `data/my-song-sabi`)。

1. 元MIDIからサビ区間だけメロディを残した `song.mid` を作る(曲頭の無音は自動カット)
2. `artwork.jpg` と `wall.json` をコピーし、wall.jsonに `"mood": "night"` を追加
3. `output/config.json` の設定を引き継いで audio → preview → final

### その他のコマンド

```bash
# 衝突検証だけ実行(レンダリングなし)
uv run python make_video.py data/my-song/input/song.mid --track 4 \
    --audio x -o /dev/null --check

# 1フレームだけ描いて見た目確認(壁・夜モードの調整に便利)
vendor/bpy-venv/bin/python blender_render.py \
    data/my-song/output/scene.json /tmp/chk --frame 0 --engine eevee \
    --wall data/my-song/input/wall.json

# 区間だけ再レンダリング(画像差し替え等でフレームの一部だけ変わったとき)
vendor/bpy-venv/bin/python blender_render.py \
    data/my-song/output/scene.json data/my-song/output/frames \
    --start 0 --end 90 --engine eevee --wall data/my-song/input/wall.json
```

## 仕組み

```
input/song.mid
  │ convert.py        トラック選択 → 単音メロディ抽出(スカイライン) →
  │                   GM音色に差し替え → FluidSynth(調律済みSF2) →
  │                   アタック軟化+リバーブ → audio.mp3
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
  弾く。行き詰まったらバックトラック(分岐幅25)。密集ノート(音間0.12秒未満)の
  手前では着地速度の下限を先読み評価して詰まりを予防する。
  同じ入力なら必ず同じ配置。探索は2万ステップごとに進捗を表示
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
tools/download.py    前処理: YouTube→MP3取得(yt-dlp、依存は独立)
tools/tune_sf2.py    サウンドフォントの調律修正(setup.shが自動実行)
tools/transcribe.py  前処理: 音源→MIDI自動採譜(依存は独立、uv runで自動解決)
tools/midi_editor.html 前処理: ブラウザで動くMIDI編集GUI(メロディ仕分け)
setup.sh             vendor/ と .venv の再構築
data/<プロジェクト>/  input/(ユーザー) と output/(生成物)
data/twinkle-star/   サンプルプロジェクト(inputのみgit管理)
vendor/              Blender(bpy)とサウンドフォント(git対象外・再構築可)
```

