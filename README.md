# rhythm-orbit

MIDIの楽曲から「音に同期してビー玉がバウンドしながら落ちていく」
Instagramリール用動画(9:16)を自動生成するパイプライン。

## パイプライン

```
MIDI(人が打ち込んだ譜面)
  │ convert.py      … トラック選択+単音メロディ抽出+音色変換+リバーブ → MP3
  │ make_video.py   … 物理シミュレーション+板/レール配置+衝突検証 → scene.json
  │ blender_render.py … Blender(bpy+Eevee/Cycles)でフォトリアル描画 → 連番PNG
  └ ffmpeg          … 音声と合成 → MP4
```

## 使い方

```bash
# 1. 音源(チェレスタ+リバーブの単音メロディ)
uv run python convert.py 曲.mid --track 1 --melody -i celesta --octave 0 \
    --sf2 vendor/FluidR3_GM.sf2 -o 曲_audio.mp3

# 2. 経路計算+シーン書き出し(衝突検証付き)
uv run python make_video.py 曲.mid --track 1 --audio 曲_audio.mp3 \
    -o /dev/null --export scene.json

# 3. Blenderレンダリング(Eevee: 約0.5-1秒/フレーム)
vendor/bpy-venv/bin/python blender_render.py scene.json frames/ --engine eevee

# 4. 音声と合成(delayはscene.jsonのaudio_delay_ms)
ffmpeg -framerate 30 -i frames/f_%04d.png -i 曲_audio.mp3 \
    -af "adelay=<ms>:all=1" -c:v libx264 -pix_fmt yuv420p -c:a aac out.mp4
```

`make_video.py --check` で衝突検証のみ実行。`--duration N` で先頭N秒だけ。
`make_video.py` 単体でも簡易レンダラー(PIL)による確認動画を出力できる。

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
