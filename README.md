# rhythm-orbit

MIDIの楽曲から「音に同期してビー玉がバウンドしながら落ちていく」
Instagramリール用動画(9:16)を自動生成するパイプライン。

## パイプライン

```
input/曲.mid
  │ convert.py        … トラック選択+単音メロディ抽出+音色変換+リバーブ → audio.mp3
  │ make_video.py     … 物理シミュレーション+板/レール配置+衝突検証
  │                      ├─ preview: 簡易3Dレンダラー(PIL) → preview.mp4 (数十秒)
  │                      └─ final:   scene.json 書き出し
  │ blender_render.py … Blender(bpy+Eevee/Cycles)フォトリアル描画 → frames/
  └ ffmpeg            … 音声と合成 → final.mp4

pipeline.py が上記を audio / preview / final の3コマンドに束ねる
```

## 使い方(プロジェクト方式・三段)

```bash
# 0. プロジェクトを作る(サンプル: data/pokemon-center-gs)
mkdir -p data/my-song/input
cp 曲.mid data/my-song/input/song.mid
#    任意: wall.json(壁の額・タイトル), 画像

# 1. 主旋律抽出+音源化 → 曲チェック(トラック未指定なら全トラック聴き比べ)
uv run python pipeline.py audio data/my-song --track 1 --play
#    音の変数: --instrument celesta --octave 0 --reverb 0.6

# 2. 簡易3Dで動き確認(数十秒)
uv run python pipeline.py preview data/my-song

# 3. フォトリアル仕上げ(Eevee、フル尺で20〜40分)
uv run python pipeline.py final data/my-song
```

生成物はすべて `data/my-song/output/` に入る。
gitにはサンプルプロジェクトのinputのみコミットされる(他プロジェクトは対象外)。

### wall.json(壁の演出)

```json
{
  "texts":  [{"text": "タイトル\n2行目", "at": "start", "dy": -0.72, "size": 0.155}],
  "frames": [{"file": "artwork.jpg", "at": "start", "dy": 1.0, "width": 0.95}]
}
```
額(frames)はどんな画像もそのまま飾れる。テキストはメタリックブラックの刻印。
導入は「何もない壁が割れてビー玉が出てくる」演出(0秒フレーム=サムネイル)。

## 設計の要点

- **音の時刻が正**: ノート発音時刻のボール位置に板を置く(物理エンジン任せでは同期不可能)
- **物理は本物**: 重力+空気抵抗(終端速度)+反発係数一定の反射。エネルギーは増えない
- **板の傾きだけが設計変数**: 81角度を網羅評価し、すり抜け・めり込みを弾く
  バックトラック付き探索(行き詰まったら前のバウンドへ戻る)
- **休符はレール**: 0.75秒超の休符は無音のゴムレール(反発ゼロ=転がり)で
  落下速度を抑える。音が鳴るのは金属板だけ
- **検証器**: find_collisions()が最終軌道×全パーツのめり込みを機械検証
- **速い連打では方向転換しない**: 流れに沿わせると幾何が破綻しない
- **描画は分離**: scene.json経由。簡易レンダラー(PIL)とBlenderが同じデータを描く

## 環境

- Python(uv管理)。音源系: basic-pitch / pretty_midi / pyfluidsynth / pedalboard
- Blender: vendor/bpy-venv(bpy 5.0, pipのBlenderフルエンジン)。GPU(Eevee/Cycles-OptiX)
- サウンドフォント: vendor/FluidR3_GM.sf2

## 権利メモ

既存楽曲のMIDIを使った音源・動画は編曲・二次利用にあたる。
公開する場合は自作曲・権利フリー曲を使うこと。
