#!/usr/bin/env python
"""レンダリング済みフレームへのポスト処理の試作ラボ。

代表フレームを自動選定し、複数プリセットで加工して比較シートを作る。
気に入ったプリセットが決まったら --apply で全フレームに一括適用する。

使い方:
  uv run python postfx_lab.py data/<proj>/output/frames -o <出力dir>          # 試作
  uv run python postfx_lab.py <frames> -o <出力dir> --apply E_refined         # 全適用
"""
import argparse
import functools
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# ---------------------------------------------------------------- プリセット
# 各値は「効果ゼロ=0」の弱さ基準。既にBlender側でブルーム+ビネットが
# 焼き込まれている前提なので、ここでは足しすぎない。
PRESETS = {
    # グレーディング+コントラスト+シャープのみ(ベースライン)
    "A_clean": dict(shadow_teal=0.05, highlight_warm=0.05, contrast=0.10,
                    spotlight=0.0, bloom=0.0, vignette=0.0, sharpen=0.6),
    # 推奨: A+情報量スポット+ごく弱いブルーム/ビネット
    "E_refined": dict(shadow_teal=0.06, highlight_warm=0.07, contrast=0.14,
                      spotlight=0.12, bloom=0.06, vignette=0.10, sharpen=0.8),
    # 推奨より暖色寄り・少し強め(シネマティック方向に半歩)
    "E_warm": dict(shadow_teal=0.09, highlight_warm=0.12, contrast=0.18,
                   spotlight=0.12, bloom=0.08, vignette=0.14, sharpen=0.8),
    # 推奨+全体を少し持ち上げ(暗すぎ対策)
    "E_bright": dict(shadow_teal=0.06, highlight_warm=0.07, contrast=0.12,
                     spotlight=0.15, bloom=0.06, vignette=0.08, sharpen=0.8,
                     lift=0.05),
}


def luminance(a):
    return a[..., 0] * 0.2126 + a[..., 1] * 0.7152 + a[..., 2] * 0.0722


def gaussian(a, radius):
    """numpy配列(0-1 float)をPIL経由でガウスぼかし。"""
    im = Image.fromarray((np.clip(a, 0, 1) * 255).astype(np.uint8))
    im = im.filter(ImageFilter.GaussianBlur(radius))
    return np.asarray(im).astype(np.float32) / 255.0


def busyness_map(a):
    """情報量マップ: 輝度勾配の大きさを強くぼかしたもの(0-1)。

    速度のため1/4解像度で計算して元サイズへ戻す(マップは低周波なので同等)。
    """
    h, w = a.shape[:2]
    small = Image.fromarray((np.clip(a, 0, 1) * 255).astype(np.uint8))
    small = np.asarray(small.resize((w // 4, h // 4))).astype(np.float32) / 255
    lum = luminance(small)
    gy, gx = np.gradient(lum)
    g = np.sqrt(gx * gx + gy * gy)
    g = gaussian(g * 8.0, radius=max(small.shape[:2]) // 24)
    peak = g.max()
    g = g / peak if peak > 1e-6 else g
    return np.asarray(Image.fromarray((g * 255).astype(np.uint8))
                      .resize((w, h))).astype(np.float32) / 255


def process(img: Image.Image, p: dict) -> Image.Image:
    a = np.asarray(img.convert("RGB")).astype(np.float32) / 255.0
    lum = luminance(a)[..., None]

    # 1. カラーグレーディング: 影を青緑へ、ハイライトを暖色へ
    shadow_w = (1.0 - lum) ** 2
    highlight_w = lum ** 2
    teal = np.array([-1.0, 0.35, 0.65], np.float32)     # R↓ G↑ B↑
    warm = np.array([1.0, 0.45, -0.55], np.float32)     # R↑ G↑ B↓
    a = a + p["shadow_teal"] * 0.35 * shadow_w * teal \
          + p["highlight_warm"] * 0.35 * highlight_w * warm

    # 2. コントラスト(0.5中心のリニアS)
    a = 0.5 + (a - 0.5) * (1.0 + p["contrast"])

    # 全体リフト(オプション)
    if p.get("lift"):
        a = a + p["lift"] * (1.0 - a)

    # 3. 情報量スポット: ビー玉・ノーツ周辺だけ少し明るく
    if p["spotlight"] > 0:
        w = busyness_map(a)[..., None]
        a = a * (1.0 + p["spotlight"] * w)

    # 4. 弱いブルーム: ハイライトのみ滲ませてスクリーン合成
    if p["bloom"] > 0:
        hi = np.clip((luminance(a) - 0.75) / 0.25, 0, 1)[..., None] * a
        glow = gaussian(hi, radius=max(a.shape[:2]) // 60)
        a = 1.0 - (1.0 - a) * (1.0 - p["bloom"] * glow)

    # 5. ビネット
    if p["vignette"] > 0:
        h, w_ = a.shape[:2]
        yy, xx = np.mgrid[0:h, 0:w_]
        r = np.sqrt(((xx / w_ - 0.5) * 2) ** 2 + ((yy / h - 0.5) * 2) ** 2)
        mask = 1.0 - p["vignette"] * np.clip(r - 0.55, 0, 1) ** 2 * 2.2
        a = a * mask[..., None]

    out = Image.fromarray((np.clip(a, 0, 1) * 255).astype(np.uint8))

    # 6. 軽いシャープ(アンシャープマスク)
    if p["sharpen"] > 0:
        out = out.filter(ImageFilter.UnsharpMask(
            radius=2, percent=int(60 * p["sharpen"]), threshold=2))
    return out


# ---------------------------------------------------------------- フレーム選定

def pick_frames(files, n=6):
    """代表フレーム: 先頭 / 動き出し / 情報量最大 / 最小 / 中間 / ラスト付近。"""
    idxs = np.linspace(0, len(files) - 1, min(48, len(files))).astype(int)
    scores = {}
    base = None
    for i in idxs:
        a = np.asarray(Image.open(files[i]).convert("L").resize((135, 240)),
                       np.float32) / 255.0
        if base is None:
            base = a
        gy, gx = np.gradient(a)
        scores[i] = (float(np.abs(a - base).mean()),          # 先頭との差
                     float(np.sqrt(gx**2 + gy**2).mean()))    # 情報量
    moving = [i for i in idxs if scores[i][0] > 0.015]
    inner = [i for i in idxs if 0 < i < len(files) - 1]
    picks = [0,
             moving[0] if moving else idxs[len(idxs) // 8],
             max(inner, key=lambda i: scores[i][1]),
             min(inner, key=lambda i: scores[i][1]),
             idxs[len(idxs) // 2],
             int(len(files) * 0.92)]
    seen, out = set(), []
    for i in picks:
        if i not in seen:
            seen.add(i)
            out.append(int(i))
    return sorted(out)[:n]


def contact_sheet(rows, col_labels, out_path, cell_w=340):
    """rows: [(行ラベル, [PIL画像,...]), ...] を1枚のシートに。"""
    ch = None
    grid = []
    for label, imgs in rows:
        cells = []
        for im in imgs:
            r = cell_w / im.width
            cells.append(im.resize((cell_w, int(im.height * r))))
        ch = cells[0].height
        grid.append((label, cells))
    pad, head = 6, 34
    W = (cell_w + pad) * len(col_labels) + pad
    H = (ch + head + pad) * len(grid) + pad
    sheet = Image.new("RGB", (W, H), (16, 18, 20))
    d = ImageDraw.Draw(sheet)
    for r, (label, cells) in enumerate(grid):
        y = pad + r * (ch + head + pad)
        for c, im in enumerate(cells):
            x = pad + c * (cell_w + pad)
            d.text((x + 2, y + 8), f"{col_labels[c]}  [{label}]",
                   fill=(220, 220, 210))
            sheet.paste(im, (x, y + head))
    sheet.save(out_path)


def _apply_one(f: Path, out: Path, preset: dict):
    process(Image.open(f), preset).save(out / f.name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("frames", type=Path, help="フレームdir (f_*.png)")
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--pick", type=str, default=None,
                    help="使うフレーム番号をカンマ区切りで(省略時は自動選定)")
    ap.add_argument("--apply", type=str, default=None,
                    help="このプリセットを全フレームに適用(試作をスキップ)")
    ap.add_argument("--workers", type=int,
                    default=max(1, (os.cpu_count() or 4) - 4))
    args = ap.parse_args()
    files = sorted(args.frames.glob("f_*.png"))
    if not files:
        raise SystemExit(f"フレームが見つからない: {args.frames}")
    args.out.mkdir(parents=True, exist_ok=True)

    if args.apply:
        p = PRESETS[args.apply]
        # 再開可能: 出力済みはスキップ(中断対策で最新1枚は作り直す)。
        # ただし入力フレームの方が新しい場合は再レンダリング後なので作り直す
        done = sorted(args.out.glob("f_*.png"))
        if done:
            done[-1].unlink()
        have = {q.name: q.stat().st_mtime for q in args.out.glob("f_*.png")}
        todo = [f for f in files
                if f.name not in have or f.stat().st_mtime > have[f.name]]
        print(f"処理済み{len(have)}件スキップ / 残り{len(todo)}件 "
              f"(workers={args.workers})", flush=True)
        work = functools.partial(_apply_one, out=args.out, preset=p)
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            for i, _ in enumerate(ex.map(work, todo, chunksize=8)):
                if i % 200 == 0:
                    print(f"{i}/{len(todo)}", flush=True)
        print(f"全{len(files)}フレームに {args.apply} を適用: {args.out}")
        return

    if args.pick:
        picks = [int(x) for x in args.pick.split(",")]
    else:
        picks = pick_frames(files)
    print("代表フレーム:", picks)
    names = ["original"] + list(PRESETS)
    rows = []
    for i in picks:
        img = Image.open(files[i])
        variants = [img] + [process(img, PRESETS[k]) for k in PRESETS]
        for name, v in zip(names, variants):
            v.save(args.out / f"f{i:04d}_{name}.png")
        rows.append((f"f_{i:04d}", variants))
    contact_sheet(rows, names, args.out / "contact_sheet.png")
    print(f"比較シート: {args.out / 'contact_sheet.png'}")


if __name__ == "__main__":
    main()
