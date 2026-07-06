#!/usr/bin/env python3
"""メロディMIDIに同期した3Dバウンドボール動画(9:16)を生成する。

物理: 重力は一定(G)。各ノート間を放物運動でつなぎ、発音時刻ちょうどに
足場へ着地する初速を逆算する(音とのズレは原理的に起きない)。
足場は1バウンドごとに一段下がり、ボールは全体として落下していく。
音程はボールの左右位置に反映される。

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
from PIL import Image, ImageDraw

from convert import extract_melody

W, H = 1080, 1920            # 9:16 リール解像度
FPS = 30
FOCAL = H * 0.62             # 透視投影の焦点距離(px)

G = 9.8                      # 重力加速度 (m/s^2)
SPEED_Z = 2.6                # 前進速度 (m/s)
DROP = 0.32                  # 1バウンドごとの下降量 (m)
X_PER_SEMITONE = 0.14        # 音程1半音あたりの左右オフセット (m)
BALL_R = 0.16                # ボール半径 (m)
PLAT_W, PLAT_D, PLAT_H = 0.42, 0.42, 0.14  # 足場の寸法 (m)

FOG_START, FOG_END = 8.0, 18.0   # フォグ開始/完全消失距離 (m)
BG_TOP = (10, 13, 30)
BG_BOTTOM = (28, 20, 52)
BALL_COLOR = np.array([255, 236, 190], dtype=float)
LIGHT_DIR = np.array([0.35, 0.85, -0.4])
LIGHT_DIR = LIGHT_DIR / np.linalg.norm(LIGHT_DIR)


# ---------------------------------------------------------------- 物理・経路

def load_bounces(midi_path: Path, track: int):
    import pretty_midi
    pm = pretty_midi.PrettyMIDI(str(midi_path))
    if track is not None:
        pm.instruments = [pm.instruments[track]]
    pm = extract_melody(pm, min_pitch=0)
    notes = sorted(pm.instruments[0].notes, key=lambda n: n.start)
    return [(n.start, n.pitch) for n in notes]


def build_points(bounces):
    """バウンド地点 [(t, x, y, z, pitch)] を作る。yは一段ずつ下がる。"""
    mid = float(np.median([p for _, p in bounces]))
    pts = []
    for i, (t, p) in enumerate(bounces):
        x = (p - mid) * X_PER_SEMITONE
        y = -i * DROP
        z = t * SPEED_Z
        pts.append((t, x, y, z, p))
    return pts


def ball_pos(pts, t):
    """時刻tのボール位置。重力Gの放物運動で各バウンド地点を正確に通る。"""
    t0, x0, y0, z0, _ = pts[0]
    if t <= t0:
        # 開始前: 最初の足場へ自由落下で進入
        dt = t0 - t
        return x0, y0 + 0.5 * G * dt * dt, z0
    for i in range(len(pts) - 1):
        ta, xa, ya, za, _ = pts[i]
        tb, xb, yb, zb, _ = pts[i + 1]
        if ta <= t <= tb:
            dt_seg = tb - ta
            s = t - ta
            # y(t) = ya + v0*s - G/2 s^2 が y(tb)=yb を満たす初速
            v0 = ((yb - ya) + 0.5 * G * dt_seg * dt_seg) / dt_seg
            x = xa + (xb - xa) * s / dt_seg
            z = za + (zb - za) * s / dt_seg
            return x, ya + v0 * s - 0.5 * G * s * s, z
    # 最後のバウンド後: 跳ね上がってそのまま落下していく
    ta, xa, ya, za, _ = pts[-1]
    s = t - ta
    return xa, ya + 2.0 * s - 0.5 * G * s * s, za + SPEED_Z * s


# ---------------------------------------------------------------- 描画

def pitch_color(pitch: int):
    hue = (pitch % 12) / 12.0
    r, g, b = colorsys.hsv_to_rgb(hue, 0.5, 1.0)
    return np.array([r * 255, g * 255, b * 255])


def make_background():
    grad = np.linspace(0, 1, H)[:, None] * np.ones((1, W))
    img = np.zeros((H, W, 3), dtype=np.uint8)
    for c in range(3):
        img[:, :, c] = (BG_TOP[c] + (BG_BOTTOM[c] - BG_TOP[c]) * grad).astype(np.uint8)
    bg = Image.fromarray(img)
    draw = ImageDraw.Draw(bg, "RGBA")
    rng = np.random.default_rng(7)
    for _ in range(110):
        x, y = int(rng.integers(0, W)), int(rng.integers(0, H))
        r = int(rng.integers(1, 3))
        a = int(rng.integers(40, 110))
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, a))
    return bg


class Camera:
    """ボールを後ろ上方から追う追従カメラ。"""

    OFFSET = np.array([0.0, 2.1, -4.6])    # ボールからの相対位置
    LOOK_AHEAD = np.array([0.0, -0.9, 2.6])

    def __init__(self, ball):
        self.smooth = np.array(ball)

    def update(self, ball):
        ball = np.array(ball)
        self.smooth += (ball - self.smooth) * np.array([0.10, 0.055, 0.10])
        self.pos = self.smooth + self.OFFSET
        target = self.smooth + self.LOOK_AHEAD
        fwd = target - self.pos
        fwd /= np.linalg.norm(fwd)
        right = np.cross(fwd, np.array([0.0, 1.0, 0.0]))
        right /= np.linalg.norm(right)
        up = np.cross(right, fwd)
        self.mat = np.stack([right, up, fwd])  # world→view 回転

    def project(self, p):
        """world座標→(screen_x, screen_y, depth)。depth<=0.05は画面外扱い。"""
        v = self.mat @ (np.asarray(p, dtype=float) - self.pos)
        if v[2] < 0.05:
            return None
        sx = W / 2 + v[0] * FOCAL / v[2]
        sy = H * 0.46 - v[1] * FOCAL / v[2]
        return sx, sy, v[2]


def fog(depth):
    """距離→不透明度係数 1.0(手前)〜0.0(彼方)。"""
    return float(np.clip((FOG_END - depth) / (FOG_END - FOG_START), 0.0, 1.0))


def shade(base, normal, k=1.0):
    lam = 0.5 + 0.5 * max(0.0, float(normal @ LIGHT_DIR))
    c = np.clip(base * lam * k, 0, 255)
    return tuple(int(v) for v in c)


def draw_platform(draw, cam, x, y, z, pitch, t, hit_t):
    """足場(直方体)を描く。上面+カメラ向きの2側面、簡易ランバート照明。"""
    top = y - BALL_R
    hw, hd = PLAT_W / 2, PLAT_D / 2
    glow = max(0.0, 1.0 - abs(t - hit_t) * 5.0)
    base = pitch_color(pitch) * (1.0 + 0.6 * glow)

    # 8頂点
    xs, zs = (x - hw, x + hw), (z - hd, z + hd)
    ys = (top - PLAT_H, top)
    v = {}
    for xi, xv in enumerate(xs):
        for yi, yv in enumerate(ys):
            for zi, zv in enumerate(zs):
                v[(xi, yi, zi)] = cam.project((xv, yv, zv))
    if any(p is None for p in v.values()):
        return
    depth = np.mean([p[2] for p in v.values()])
    f = fog(depth)
    if f <= 0.01:
        return
    alpha = int(235 * f)

    def quad(keys, normal, k=1.0):
        pts2d = [(v[k][0], v[k][1]) for k in keys]
        draw.polygon(pts2d, fill=(*shade(base, np.array(normal), k), alpha))

    # 上面
    quad([(0, 1, 0), (1, 1, 0), (1, 1, 1), (0, 1, 1)], (0, 1, 0))
    # 手前面(カメラは-z側から見る)
    quad([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], (0, 0, -1), 0.75)
    # 側面(カメラのx位置に応じて見える方)
    if cam.pos[0] < x:
        quad([(0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0)], (-1, 0, 0), 0.6)
    else:
        quad([(1, 0, 0), (1, 0, 1), (1, 1, 1), (1, 1, 0)], (1, 0, 0), 0.6)

    # 着地の瞬間の発光
    if glow > 0.02:
        p = cam.project((x, top, z))
        if p:
            rr = (0.9 + 0.8 * glow) * FOCAL * PLAT_W / p[2]
            col = tuple(int(c) for c in np.clip(base, 0, 255))
            draw.ellipse([p[0] - rr, p[1] - rr * 0.35, p[0] + rr, p[1] + rr * 0.35],
                         fill=(*col, int(70 * glow * f)))


def draw_ball(draw, cam, pos, trail):
    # 軌跡
    n = len(trail)
    for i, tp in enumerate(trail):
        pr = cam.project(tp)
        if pr is None:
            continue
        k = (i + 1) / n
        r = BALL_R * FOCAL / pr[2] * (0.3 + 0.45 * k)
        draw.ellipse([pr[0] - r, pr[1] - r, pr[0] + r, pr[1] + r],
                     fill=(*[int(c) for c in BALL_COLOR], int(60 * k)))
    p = cam.project(pos)
    if p is None:
        return
    r = BALL_R * FOCAL / p[2]
    col = tuple(int(c) for c in BALL_COLOR)
    for rr, aa in [(r * 2.1, 42), (r * 1.45, 85)]:
        draw.ellipse([p[0] - rr, p[1] - rr, p[0] + rr, p[1] + rr], fill=(*col, aa))
    draw.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=col)
    hr = r * 0.42
    draw.ellipse([p[0] - hr - r * 0.3, p[1] - hr - r * 0.3,
                  p[0] - r * 0.3, p[1] - r * 0.3], fill=(255, 255, 255, 190))


# ---------------------------------------------------------------- メイン

def render(pts, audio: Path, output: Path, end_time: float):
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

    cam = Camera(ball_pos(pts, 0.0))
    trail = []
    for f in range(n_frames):
        t = f / FPS
        bp = ball_pos(pts, t)
        cam.update(bp)

        frame = bg.copy()
        draw = ImageDraw.Draw(frame, "RGBA")

        # カメラ前方の足場のみ、遠い順に描画(painter's algorithm)
        visible = []
        for (pt, x, y, z, pitch) in pts:
            d = z - cam.pos[2]
            if -1.0 < d < FOG_END + 2.0:
                visible.append((d, pt, x, y, z, pitch))
        for d, pt, x, y, z, pitch in sorted(visible, reverse=True):
            draw_platform(draw, cam, x, y, z, pitch, t, pt)

        trail.append(bp)
        if len(trail) > 20:
            trail.pop(0)
        draw_ball(draw, cam, bp, trail[:-1])

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
    parser = argparse.ArgumentParser(description="メロディ同期3Dバウンドボール動画を生成")
    parser.add_argument("midi", type=Path, help="メロディMIDI")
    parser.add_argument("--track", type=int, default=None, help="使用トラック番号")
    parser.add_argument("--audio", type=Path, required=True, help="重ねる音声(MP3)")
    parser.add_argument("-o", "--output", type=Path, required=True, help="出力MP4")
    parser.add_argument("--duration", type=float, default=None,
                        help="先頭からこの秒数だけ描画(プレビュー用)")
    args = parser.parse_args()

    bounces = load_bounces(args.midi, args.track)
    end_time = bounces[-1][0]
    if args.duration:
        end_time = min(end_time, args.duration)
        bounces = [b for b in bounces if b[0] <= end_time]
    pts = build_points(bounces)
    print(f"バウンド数: {len(pts)}, 長さ: {end_time:.1f}s")
    render(pts, args.audio, args.output, end_time)
    print(f"完了: {args.output}")


if __name__ == "__main__":
    main()
