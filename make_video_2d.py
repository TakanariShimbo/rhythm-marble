#!/usr/bin/env python3
"""メロディMIDIに同期してボールがバウンドするリール動画(9:16)を生成する。

各ノートの発音時刻にボールが足場に着地するよう放物線を逆算する方式。
音声はconvert.pyで作ったMP3をそのまま重ねるので、ズレは原理的に起きない。

使い方:
  uv run python make_video.py data/What~928.mid --track 1 \
      --audio data/What~928_solo_celesta_hq.mp3 -o data/What~928_video.mp4
"""

import argparse
import colorsys
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from convert import extract_melody

W, H = 1080, 1920          # 9:16 リール解像度
FPS = 30
X_SPEED = 420.0            # ボールの水平速度 (world px/s)
PITCH_Y_SCALE = 34.0       # 音程1半音あたりの足場の高さ差 (px)
BOUNCE_H_MIN = 130.0       # バウンドの最低高さ (px)
PLATFORM_W, PLATFORM_H = 120, 22
BALL_R = 26
TRAIL_LEN = 22
BG_TOP = (13, 16, 34)      # 背景グラデーション上端
BG_BOTTOM = (24, 18, 48)   # 同下端
BALL_COLOR = (255, 236, 190)


def load_bounces(midi_path: Path, track: int):
    """MIDIからバウンド地点列 [(time, pitch)] を作る。"""
    import pretty_midi
    pm = pretty_midi.PrettyMIDI(str(midi_path))
    if track is not None:
        pm.instruments = [pm.instruments[track]]
    pm = extract_melody(pm, min_pitch=0)
    notes = sorted(pm.instruments[0].notes, key=lambda n: n.start)
    end_time = max(n.end for n in notes)
    return [(n.start, n.pitch) for n in notes], end_time


def pitch_color(pitch: int):
    """音程→パステルカラー(音名で色相を回す)。"""
    hue = (pitch % 12) / 12.0
    r, g, b = colorsys.hsv_to_rgb(hue, 0.45, 1.0)
    return int(r * 255), int(g * 255), int(b * 255)


def build_path(bounces):
    """バウンド地点列から、時刻→ボール座標の関数に必要な区間データを作る。

    world座標: xは時間に比例して右へ、yは音程が高いほど上(値が小さい)。
    """
    mid_pitch = np.median([p for _, p in bounces])
    pts = []  # (time, x, y_platform, pitch)
    for t, p in bounces:
        x = t * X_SPEED
        y = (mid_pitch - p) * PITCH_Y_SCALE
        pts.append((t, x, y, p))
    return pts


def ball_pos(pts, t):
    """時刻tのボール座標。区間ごとに放物線(頂点は区間中央)を描く。"""
    if t <= pts[0][0]:
        # 開始前: 最初の足場の真上から落下してくる
        t0, x0, y0, _ = pts[0]
        dt = max(t0 - t, 0.0)
        return x0, y0 - BOUNCE_H_MIN * 2 * min(dt / 0.8, 1.0) ** 2 - 4
    for i in range(len(pts) - 1):
        t0, x0, y0, _ = pts[i]
        t1, x1, y1, _ = pts[i + 1]
        if t0 <= t <= t1:
            s = (t - t0) / (t1 - t0)
            x = x0 + (x1 - x0) * s
            h = max(BOUNCE_H_MIN, abs(y1 - y0) * 0.55)
            y = y0 + (y1 - y0) * s - 4 * h * s * (1 - s)
            return x, y
    # 最後のバウンド後: その場で減衰バウンド
    t0, x0, y0, _ = pts[-1]
    dt = t - t0
    h = BOUNCE_H_MIN * max(0.0, 1.0 - dt) ** 2
    s = (dt % 0.5) / 0.5
    return x0 + dt * X_SPEED * 0.25, y0 - 4 * h * s * (1 - s)


def make_background():
    """縦グラデーション+星の背景(毎フレーム使い回す)。"""
    grad = np.linspace(0, 1, H)[:, None] * np.ones((1, W))
    img = np.zeros((H, W, 3), dtype=np.uint8)
    for c in range(3):
        img[:, :, c] = (BG_TOP[c] + (BG_BOTTOM[c] - BG_TOP[c]) * grad).astype(np.uint8)
    bg = Image.fromarray(img)
    draw = ImageDraw.Draw(bg)
    rng = np.random.default_rng(7)
    for _ in range(90):
        x, y = rng.integers(0, W), rng.integers(0, H)
        r = int(rng.integers(1, 3))
        a = int(rng.integers(40, 110))
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, a))
    return bg


def render(pts, end_time, audio: Path, output: Path):
    total = end_time + 1.5
    n_frames = int(total * FPS)
    bg = make_background()

    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
         "-i", "-", "-i", str(audio),
         "-c:v", "libx264", "-preset", "fast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest",
         str(output)],
        stdin=subprocess.PIPE,
    )

    cam_x, cam_y = ball_pos(pts, 0.0)
    trail = []
    for f in range(n_frames):
        t = f / FPS
        bx, by = ball_pos(pts, t)
        # カメラはボールを滑らかに追う(画面中央やや上にボールが来る)
        cam_x += (bx - cam_x) * 0.10
        cam_y += (by - cam_y) * 0.06

        def to_screen(wx, wy):
            return wx - cam_x + W * 0.42, wy - cam_y + H * 0.44

        frame = bg.copy()
        draw = ImageDraw.Draw(frame, "RGBA")

        # 足場(画面内のものだけ)
        for pt, px, py, pitch in pts:
            sx, sy = to_screen(px, py)
            if sx < -200 or sx > W + 200 or sy < -200 or sy > H + 200:
                continue
            col = pitch_color(pitch)
            # 着地の瞬間に光る
            glow = max(0.0, 1.0 - abs(t - pt) * 6.0)
            alpha = int(150 + 105 * glow)
            wpad = int(PLATFORM_W / 2 * (1 + 0.25 * glow))
            draw.rounded_rectangle(
                [sx - wpad, sy, sx + wpad, sy + PLATFORM_H],
                radius=10, fill=(*col, alpha))
            if glow > 0:
                rr = int(60 + 90 * glow)
                draw.ellipse([sx - rr, sy - rr * 0.4, sx + rr, sy + rr * 0.4],
                             fill=(*col, int(60 * glow)))

        # 軌跡
        trail.append((bx, by))
        if len(trail) > TRAIL_LEN:
            trail.pop(0)
        for i, (tx, ty) in enumerate(trail[:-1]):
            sx, sy = to_screen(tx, ty)
            k = i / TRAIL_LEN
            r = BALL_R * (0.25 + 0.5 * k)
            draw.ellipse([sx - r, sy - r, sx + r, sy + r],
                         fill=(*BALL_COLOR, int(70 * k)))

        # ボール(グロー付き)
        sx, sy = to_screen(bx, by)
        for rr, aa in [(BALL_R * 2.2, 40), (BALL_R * 1.5, 80)]:
            draw.ellipse([sx - rr, sy - rr, sx + rr, sy + rr],
                         fill=(*BALL_COLOR, aa))
        draw.ellipse([sx - BALL_R, sy - BALL_R, sx + BALL_R, sy + BALL_R],
                     fill=BALL_COLOR)
        draw.ellipse([sx - BALL_R * 0.45 - 6, sy - BALL_R * 0.45 - 6,
                      sx - 6, sy - 6], fill=(255, 255, 255, 200))

        try:
            ff.stdin.write(np.asarray(frame.convert("RGB")).tobytes())
        except BrokenPipeError:
            break  # -shortestにより音声終了時点でffmpegが先に閉じる
        if f % (FPS * 5) == 0:
            print(f"  {t:5.1f}s / {total:.1f}s")

    ff.stdin.close()
    ff.wait()
    if ff.returncode != 0:
        sys.exit("エラー: ffmpegが失敗しました")


def main():
    parser = argparse.ArgumentParser(description="メロディ同期バウンドボール動画を生成")
    parser.add_argument("midi", type=Path, help="メロディMIDI")
    parser.add_argument("--track", type=int, default=None, help="使用トラック番号")
    parser.add_argument("--audio", type=Path, required=True, help="重ねる音声(MP3)")
    parser.add_argument("-o", "--output", type=Path, required=True, help="出力MP4")
    parser.add_argument("--duration", type=float, default=None,
                        help="先頭からこの秒数だけ描画(プレビュー用)")
    args = parser.parse_args()

    pts_raw, end_time = load_bounces(args.midi, args.track)
    if args.duration:
        end_time = min(end_time, args.duration)
        pts_raw = [b for b in pts_raw if b[0] <= end_time]
    pts = build_path(pts_raw)
    print(f"バウンド数: {len(pts)}, 長さ: {end_time:.1f}s")
    render(pts, end_time, args.audio, args.output)
    print(f"完了: {args.output}")


if __name__ == "__main__":
    main()
