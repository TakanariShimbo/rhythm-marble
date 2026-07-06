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

pipeline.py が上記を preview / final の2コマンドに束ねる
```

## 使い方(二段方式)

```bash
# 1. MIDIを input/ に置く

# 2. プレビュー(簡易3D、数十秒で確認できる)
uv run python pipeline.py preview input/曲.mid --track 1
#    お試しは --duration 15 で先頭だけ

# 3. 良ければフォトリアル仕上げ(Eevee、フル尺で20〜40分)
uv run python pipeline.py final input/曲.mid --track 1
```

出力は `output/<曲名>/` にまとまる:
`audio.mp3`(音源) / `preview.mp4`(簡易3D) / `scene.json` / `frames/` / `final.mp4`

音色などのオプション: `--instrument celesta --octave 0 --reverb 0.6`
検証のみ: `uv run python make_video.py 曲.mid --track 1 --audio x -o /dev/null --check`

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
