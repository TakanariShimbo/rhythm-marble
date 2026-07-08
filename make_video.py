#!/usr/bin/env python3
"""メロディMIDIに同期した3D落下ボール動画(9:16)を生成する。

物理: 重力+空気抵抗(終端速度DRAG_VT)の下でボールをシミュレーションする。
ノートの発音時刻のボール位置に斜めの板を置き、反発係数RESTITUTION(一定)の
反射で跳ね返す。設計変数は板の傾きだけで、エネルギーが増えることはない。

長い休符(GAP_MAX超)では、音の鳴らないグレーのレールを敷く。レールとの
衝突は反発係数ゼロ(法線方向の速度が消え、接線方向だけ残る)なので、
ボールはレールの上を転がって降りる。転がり速度は斜面の終端速度で頭打ちに
なるため、休符中にボールが速くなりすぎない。

板の配置は上から順の決定的な探索(バックトラック付き)で、
「軌道が板をすり抜けない」「板同士がめり込まない」角度だけを選ぶ。
find_collisions()が最終軌道を機械検証する(--checkで検証のみ実行)。

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

from convert import extract_melody, process_long_notes, trim_leading_silence

W, H = 1080, 1920            # 9:16 リール解像度
TITLE = "ポケモンセンター"     # 壁に表示する曲名
FPS = 30
FOCAL = H * 0.62             # 透視投影の焦点距離(px)

G = 9.8                      # 重力加速度 (m/s^2)
DRAG_VT = 7.5                # 終端速度 (m/s)。空気抵抗で落下速度はここで頭打ち
RESTITUTION = 0.35           # 鳴る板の反発係数(一定)
VX_TARGET = 1.5              # 跳ね返り後の目標横速度 (m/s)。板の傾き選びに使う
VY_CAP = 2.5                 # 跳ね上げ速度の上限目安 (m/s)。超過分は横に逃がす
X_LIMIT = 1.3                # 中心からこの距離を超えたら内側へ向かわせる (m)
MAX_TILT = 40.0              # 板の最大傾き (度)
BALL_R = 0.09                # ボール半径 (m)。経路の間隔に対して小さく保つ
PLAT_W = 0.18                # 板の横幅 (m)。全板で固定
PLAT_H = 0.03                # 板の厚さ (m)。極薄
PLAT_LIFE = 1.2              # 板が着地後に消えるまでの時間 (s)
PLAT_LEAD = 1.2              # 板が着地の何秒前に現れるか (s)
# ループ演出「振り子の頂点」: 終わりは上りレールを登り切って速度ゼロ、
# 始まりは同じ(見た目の)位置から転がり落ちる。継ぎ目は折り返しの一瞬
# なので、位置も速度(ゼロ)も構図も完全に一致する。
ENTRY_V = 1.4                # 入場レール離脱時の速度 (m/s)
ENTRY_TF = 0.75              # レール離脱→最初の板への飛行時間 (s)
EXIT_TF = 0.35               # 最後の板→出口レールへの飛行時間 (s)

GAP_MAX = 0.75               # ノート間隔がこれを超えたらレールで転がす (s)
RAIL_SLOPE = np.radians(12)  # レールの傾斜角
RAIL_H = 0.035               # レールの厚さ (m)
RAIL_COLOR = np.array([150.0, 155.0, 170.0])   # 無音レールの色(無彩色)

FOG_START, FOG_END = 5.0, 12.0
BG_TOP = (10, 13, 30)
BG_BOTTOM = (28, 20, 52)
BALL_COLOR = np.array([255, 236, 190], dtype=float)
LIGHT_DIR = np.array([0.35, 0.85, -0.4])
LIGHT_DIR = LIGHT_DIR / np.linalg.norm(LIGHT_DIR)


# ---------------------------------------------------------------- 物理

def load_bounces(midi_path: Path, track, long_note="keep", long_note_len=0.5,
                 min_pitch=55):
    import pretty_midi
    pm = pretty_midi.PrettyMIDI(str(midi_path))
    if track is not None:
        idx = [int(x) for x in str(track).split(",")]
        pm.instruments = [pm.instruments[i] for i in idx]
    # convert.py と同じmin_pitchで抽出しないと音源とバウンドがズレる
    pm = extract_melody(pm, min_pitch=min_pitch)
    pm = process_long_notes(pm, long_note, long_note_len)
    pm = trim_leading_silence(pm)
    notes = sorted(pm.instruments[0].notes, key=lambda n: n.start)
    return [(n.start, n.pitch) for n in notes]


def fly(p0, v0, s):
    """重力+線形空気抵抗下の弾道。時刻s後の(位置, 速度)を返す。

    終端速度DRAG_VTの線形抵抗モデル(k=G/VT)の閉形式解。sは配列でも可。
    """
    k = G / DRAG_VT
    s = np.asarray(s, dtype=float)
    ek = np.exp(-k * s)
    shape = s.shape + (3,)
    pos = np.zeros(shape)
    vel = np.zeros(shape)
    for c in (0, 2):  # 水平: 指数減衰
        pos[..., c] = p0[c] + v0[c] / k * (1.0 - ek)
        vel[..., c] = v0[c] * ek
    pos[..., 1] = p0[1] + (v0[1] + DRAG_VT) / k * (1.0 - ek) - DRAG_VT * s
    vel[..., 1] = (v0[1] + DRAG_VT) * ek - DRAG_VT
    return pos, vel


def roll_state(v0, s):
    """レール上の転がり(斜面の重力成分+空気抵抗)。(移動距離, 速度)を返す。"""
    k = G / DRAG_VT
    vt = G * np.sin(RAIL_SLOPE) / k      # 転がりの終端速度
    s = np.asarray(s, dtype=float)
    ek = np.exp(-k * s)
    vel = vt + (v0 - vt) * ek
    dist = vt * s + (v0 - vt) * (1.0 - ek) / k
    return dist, vel


def roll_up(v0, s):
    """上り勾配EXIT_SLOPEのレールを転がり減速する。(距離, 速度)。

    dv/dt = -g sinα - k v の閉形式解。停止後は頂点に留まる(そこが継ぎ目)。
    """
    k = G / DRAG_VT
    va = G * np.sin(RAIL_SLOPE) / k
    t_stop = np.log((v0 + va) / va) / k
    s = np.minimum(np.asarray(s, dtype=float), t_stop)
    ek = np.exp(-k * s)
    vel = (v0 + va) * ek - va
    dist = ((v0 + va) / k) * (1.0 - ek) - va * s
    return dist, vel


def roll_up_stop(v0):
    """上り転がりが停止するまでの(時間, 距離)。"""
    k = G / DRAG_VT
    va = G * np.sin(RAIL_SLOPE) / k
    t_stop = float(np.log((v0 + va) / va) / k)
    d, _ = roll_up(v0, t_stop)
    return t_stop, float(d)


def piece_pos(piece, t):
    """区間(飛行 or 転がり)内の時刻tのボール位置。"""
    if piece[0] == "fly":
        _, t0, t1, p0, v0 = piece
        return fly(p0, v0, t - t0)[0]
    if piece[0] == "roll":
        _, t0, t1, p0, u, v0s = piece
        d, _ = roll_state(v0s, t - t0)
        return p0 + u * float(d)
    _, t0, t1, p0, u, v0s = piece          # "rollu": 上り減速
    d, _ = roll_up(v0s, t - t0)
    return p0 + u * float(d)


def path_pos(pieces, t):
    for pc in pieces:
        if pc[1] <= t <= pc[2]:
            return piece_pos(pc, t)
    return piece_pos(pieces[-1], t)


def rail_frame(u):
    """レールの接線uから上向きの法線を作る(xy平面内)。"""
    n = np.cross(np.array([0.0, 0.0, 1.0]), u)
    if n[1] < 0:
        n = -n
    return n / np.linalg.norm(n)


def gap_plan(t, p, v_out, dt_next, sx):
    """休符区間の複合経路: 短い飛行 → レール転がり → 落下して次の板へ。

    レール接触は反発係数ゼロ(法線速度が消え接線速度のみ残る)なので、
    ボールはレール上を転がる。返り値: (区間リスト, レール, 終端位置, 終端速度)
    """
    ta = min(0.35, 0.30 * dt_next)
    tf = min(0.22, 0.22 * dt_next)
    tr = dt_next - ta - tf
    p1, v1 = fly(p, v_out, ta)
    u = np.array([sx * np.cos(RAIL_SLOPE), -np.sin(RAIL_SLOPE), 0.0])
    v0s = max(0.05, float(v1 @ u))       # e=0: 接線成分だけが残る
    d, vds = roll_state(v0s, tr)
    d, vds = float(d), float(vds)
    p2 = p1 + u * d
    v2 = u * vds
    p3, v3 = fly(p2, v2, tf)
    pieces = [("fly", t, t + ta, p.copy(), v_out.copy()),
              ("roll", t + ta, t + ta + tr, p1, u, v0s),
              ("fly", t + ta + tr, t + dt_next, p2, v2)]
    # レールは着地点で始まり離脱点で終わる(余白はごく僅か)
    a_ext = p1 - u * 0.05
    b_ext = p2 + u * 0.05
    rail = (t + ta, t + ta + tr, a_ext, b_ext, u, d + 0.10)
    return pieces, rail, p3, v3


# ---------------------------------------------------------------- 経路探索

def build_path(bounces):
    """物理シミュレーション+バックトラック探索で経路を作る。

    返り値:
      pts: [(t, pos(3,), pitch)] 鳴る板
      pieces: 飛行/転がり区間のリスト(ball_posと検証器が使う)
      normals, sizes: 各板の法線と横幅
      rails: [(t0, t1, a(3,), b(3,), u(3,), length)] 無音レール
    """
    e = RESTITUTION
    # 最初の板への入射速度 = 入場レール離脱(ENTRY_V)後にENTRY_TF飛行した速度
    u_in0 = np.array([-np.cos(RAIL_SLOPE), -np.sin(RAIL_SLOPE), 0.0])
    v = fly(np.zeros(3), u_in0 * ENTRY_V, ENTRY_TF)[1]
    p = np.zeros(3)
    tilts = np.radians(np.linspace(-MAX_TILT, MAX_TILT, 81))
    cand_normals = np.stack([np.sin(tilts), np.cos(tilts), np.zeros_like(tilts)],
                            axis=1)
    FEASIBLE = 500.0
    MAX_BRANCH = 10
    N = len(bounces)

    def plate_w(idx):
        """板の横幅は全て固定(見た目の分かりやすさ優先)。"""
        return PLAT_W

    def eval_step(i, t, p, v, sign, placed, hist_t, hist_p):
        dt_next = (bounces[i + 1][0] - t) if i < N - 1 else 1.0
        gap = dt_next > GAP_MAX and i < N - 1
        fast = dt_next < 0.35
        if i == N - 1:
            # 最後のバウンド: 出口レール(+x方向)に乗る向きへ跳ねさせる
            sign = 1.0
            fast = False
        if fast:
            # 速い連打: 方向転換の余地がないので流れに沿わせ、
            # 目標横速度も現在速度から達成可能な範囲に抑える
            cur = v[0] if abs(v[0]) > 0.05 else sign * 0.1
            if np.sign(cur) != sign:
                vx_goal = sign * min(VX_TARGET, abs(cur) * 0.5 + 0.15)
            else:
                vx_goal = sign * min(VX_TARGET, abs(cur) * 0.9 + 0.25)
        else:
            vx_goal = sign * VX_TARGET

        # ボールが上昇中にノートが来たら、板を天井(下向き)にして叩き落とす
        cands = -cand_normals if v[1] > 0 else cand_normals
        outs = v - (1.0 + e) * (cands @ v)[:, None] * cands
        score = np.abs(outs[:, 0] - vx_goal)
        # 跳ね上げすぎる候補は避け、余剰エネルギーは横方向へ逃がす
        score += np.maximum(0.0, outs[:, 1] - VY_CAP) * 100.0
        # 板の表側から当たらない候補(すり抜け)は不可
        score += ((cands @ v) >= -1e-6) * 1e4

        w_plat = plate_w(i)
        w_next = plate_w(i + 1) if i < N - 1 else w_plat
        ts = np.linspace(0.06 * dt_next, dt_next * 0.97, 24)

        # 各候補の弾道サンプルと着地状態(=次の板の位置と入射方向)
        rails_c = None
        if gap:
            K = len(cands)
            q = np.empty((K, len(ts), 3))
            landing = np.empty((K, 3))
            land_v = np.empty((K, 3))
            rails_c = []
            for k1, vo in enumerate(outs):
                pcs, rail, p3, v3 = gap_plan(t, p, vo, dt_next, sign)
                q[k1] = np.stack([path_pos(pcs, t + s) for s in ts])
                landing[k1], land_v[k1] = p3, v3
                rails_c.append(rail)
        else:
            q = np.stack([fly(p, vo, ts)[0] for vo in outs])
            land_pv = [fly(p, vo, dt_next) for vo in outs]
            landing = np.stack([pv[0] for pv in land_pv])
            land_v = np.stack([pv[1] for pv in land_pv])
        land_vy = land_v[:, 1]

        for hit_t, c_top, pu, pd, pn, pw in placed:
            if hit_t + PLAT_LIFE < t:
                continue
            # (1) ボールの軌道が板をすり抜ける角度は不可
            rel = q - c_top
            inside = ((np.abs(rel @ pu) < pw / 2 + BALL_R) &
                      (np.abs(rel @ pd) < pw * 0.85 / 2 + BALL_R) &
                      ((rel @ pn) < BALL_R * 0.8) &
                      ((rel @ pn) > -PLAT_H - BALL_R))
            score += inside.any(axis=1) * 1009  # (1)
            # (2) 次の板が既存の板とめり込む角度も不可
            # 既存の板は薄いので線分として扱い、次の板(向き未定)は
            # 着地点中心の円で近似して、点と線分の距離で判定する
            if hit_t + PLAT_LIFE > t + dt_next - PLAT_LEAD:
                c_old = c_top - pn * PLAT_H / 2
                a = c_old - pu * (pw / 2)
                ab = pu * pw
                c_new = landing.copy()
                c_new[:, 1] -= np.sign(-land_vy) * (BALL_R + PLAT_H / 2)
                tt = np.clip((c_new - a) @ ab / (ab @ ab), 0.0, 1.0)
                near = a + tt[:, None] * ab[None, :]
                d_land = np.linalg.norm(c_new - near, axis=1)
                score += (d_land < w_next / 2 + PLAT_H + 0.06) * 503  # (2)

        # (2b) いま跳ねている板(まだplaced未登録)と次の板の位置もめり込み不可
        u_self0 = np.stack([cands[:, 1], -cands[:, 0], np.zeros(len(cands))], axis=1)
        cen_self = p[None, :] - cands * (BALL_R + PLAT_H / 2)
        a_s = cen_self - u_self0 * (w_plat / 2)
        ab_s = u_self0 * w_plat
        c_new2 = landing.copy()
        c_new2[:, 1] -= np.sign(-land_vy) * (BALL_R + PLAT_H / 2)
        tt_s = np.clip(np.einsum("kc,kc->k", c_new2 - a_s, ab_s) /
                       np.einsum("kc,kc->k", ab_s, ab_s), 0.0, 1.0)
        near_s = a_s + tt_s[:, None] * ab_s
        d_s = np.linalg.norm(c_new2 - near_s, axis=1)
        score += (d_s < w_next / 2 + PLAT_H + 0.06) * 509  # (2b)

        # (3) 自分が今跳ねた板に再突入する角度は不可
        # 「板に近づく向きに動いている」サンプルだけを再突入とみなす
        c_self = p[None, :] - cands * BALL_R
        rel_s = q - c_self[:, None, :]
        pu_s = np.einsum("ktc,kc->kt", rel_s, u_self0)
        pn_s = np.einsum("ktc,kc->kt", rel_s, cands)
        approaching = np.concatenate(
            [np.zeros((len(cands), 1), bool), np.diff(pn_s, axis=1) < 0], axis=1)
        # マージンは検証器(0.6R)と同基準にする(厳しすぎると探索が破綻する)
        self_in = ((np.abs(pu_s) < w_plat / 2 + BALL_R * 0.6) &
                   (np.abs(rel_s[:, :, 2]) < w_plat * 0.85 / 2 + BALL_R * 0.6) &
                   (pn_s < BALL_R * 0.65) & (pn_s > -PLAT_H - BALL_R) &
                   approaching)
        score += self_in.any(axis=1) * 1013  # (3)

        # (4) 次の板は、それが表示される時間帯の確定済み軌道と重なってはいけない
        if hist_t:
            HT = np.concatenate(hist_t)
            HP = np.concatenate(hist_p)
            m = HT >= (t + dt_next) - PLAT_LEAD
            if m.any():
                dd = np.linalg.norm(landing[:, None, :] - HP[m][None, :, :], axis=2)
                score += (dd.min(axis=1) < w_next / 2 + BALL_R * 0.9) * 521  # (4)
            # (5) 今置く板が、直前の進入経路(確定済み)に刺さる傾きは不可
            # 除外は距離ベース: バウンド点の近傍は正当な最終アプローチ
            m_in = ((HT >= t - 0.7) & (HT <= t - 0.02) &
                    (np.linalg.norm(HP - p[None, :], axis=1) > BALL_R * 2.2))
            if m_in.any():
                rel_i = HP[m_in][None, :, :] - c_self[:, None, :]
                pu_i = np.einsum("kmc,kc->km", rel_i, u_self0)
                pn_i = np.einsum("kmc,kc->km", rel_i, cands)
                in_i = ((np.abs(pu_i) < w_plat / 2 + BALL_R * 0.9) &
                        (np.abs(rel_i[:, :, 2]) < w_plat * 0.85 / 2 + BALL_R * 0.9) &
                        (pn_i < BALL_R * 0.9) & (pn_i > -PLAT_H - BALL_R * 0.5))
                score += in_i.any(axis=1) * 1019  # (5)

        # (6) 着地点に置かれる次の板を、飛行中に突き抜ける軌道は不可
        # (着地時に上昇中なら次の板は天井=箱は上、下降中なら床=下)
        early = ts < 0.9 * dt_next
        box_cy = landing[:, 1] - np.sign(-land_vy) * (BALL_R + PLAT_H / 2)
        in_box = ((np.abs(q[:, :, 0] - landing[:, 0, None]) < w_next / 2 + BALL_R * 0.9) &
                  (np.abs(q[:, :, 1] - box_cy[:, None]) < PLAT_H / 2 + BALL_R * 0.9))
        score += (in_box & early[None, :]).any(axis=1) * 523  # (6)

        return score, cands, outs, q, ts, w_plat, dt_next, rails_c, landing, land_v

    # ---- バックトラック付き深さ優先探索 ----
    # 上から順に角度を決めていき、実行可能(制約違反ゼロ)な角度がない
    # バウンドに達したら、1つ前のバウンドへ戻って別の角度を試す。
    states = [None] * (N + 1)
    states[0] = (p.copy(), v.copy(), 1.0, [], [], [])
    tried = [None] * N
    chosen = [None] * N
    compromises = []
    visits = 0
    max_visits = 120 * N
    i = 0
    while i < N:
        t, pitch = bounces[i]
        p_i, v_i, sign, placed, hist_t, hist_p = states[i]
        if tried[i] is None:
            if abs(p_i[0]) > X_LIMIT:
                sign = -np.sign(p_i[0])
            res = eval_step(i, t, p_i, v_i, sign, placed, hist_t, hist_p)
            order = np.argsort(res[0])[:MAX_BRANCH]
            tried[i] = [order, 0, res, sign]
        order, ptr, res, sign = tried[i]
        score, cands, outs, q, ts, w_plat, dt_next, rails_c, landing, land_v = res
        visits += 1

        # 候補が尽きた/残りが全て制約違反 → 1つ前のバウンドへ戻ってやり直す
        exhausted = ptr >= len(order)
        infeasible = (not exhausted) and score[order[ptr]] >= FEASIBLE
        if (exhausted or infeasible) and i > 0 and visits < max_visits:
            tried[i] = None
            i -= 1
            tried[i][1] += 1
            continue

        best = int(order[min(ptr, len(order) - 1)])
        if score[best] >= FEASIBLE:
            compromises.append(i)
            print(f"    [妥協詳細] バウンド{i}: 最良スコア={score[best]:.0f} "
                  f"上位5候補={[int(x) for x in np.sort(score)[:5]]}")
        n = cands[best]
        v_out = outs[best]
        chosen[i] = (t, p_i.copy(), pitch, n, v_out.copy(), w_plat, sign,
                     rails_c is not None)

        u0 = np.array([n[1], -n[0], 0.0])
        new_placed = placed + [(t, p_i - n * BALL_R, u0,
                                np.array([0.0, 0.0, 1.0]), n, w_plat)]
        new_placed = new_placed[-12:]
        new_ht = hist_t + [t + ts]
        new_hp = hist_p + [q[best]]
        while new_ht and new_ht[0][-1] < t - PLAT_LEAD - 1.5:
            new_ht.pop(0)
            new_hp.pop(0)
        if i < N - 1:
            states[i + 1] = (landing[best].copy(), land_v[best].copy(), sign,
                             new_placed, new_ht, new_hp)
        i += 1

    if compromises:
        print(f"  探索妥協: {len(set(compromises))}バウンド {sorted(set(compromises))[:10]}")
    print(f"  探索ステップ数: {visits} (バウンド数 {N})")

    # 確定した選択から出力を組み立てる
    pts, pieces, normals, sizes, rails = [], [], [], [], []

    # ---- 入場: レールの頂点(静止)から左下へ転がり落ちる(振り子の出発) ----
    t0_note = bounces[0][0]
    u_in = np.array([-np.cos(RAIL_SLOPE), -np.sin(RAIL_SLOPE), 0.0])
    v_exit = u_in * ENTRY_V
    disp = fly(np.zeros(3), v_exit, ENTRY_TF)[0]
    E = chosen[0][1] - disp                      # レール離脱点
    k_ = G / DRAG_VT
    vt_roll = DRAG_VT * np.sin(RAIL_SLOPE)
    t_roll = float(-np.log(1.0 - ENTRY_V / vt_roll) / k_)
    d_roll = float(roll_state(0.0, t_roll)[0])
    S = E - u_in * d_roll                        # 頂点(最初のフレーム, v=0)
    t_exit = t0_note - ENTRY_TF
    pieces.append(("roll", t_exit - t_roll, t_exit, S, u_in, 0.0))
    pieces.append(("fly", t_exit, t0_note, E, v_exit))
    rails.append((t_exit - t_roll - 2.0, t_exit,
                  S - u_in * 0.45, E + u_in * 0.05, u_in, d_roll + 0.5))

    for i, (t, p_i, pitch, n, v_out, w_plat, sgn, is_gap) in enumerate(chosen):
        pts.append((t, p_i, pitch))
        normals.append(n)
        sizes.append(w_plat)
        if i < N - 1:
            dt = bounces[i + 1][0] - t
            if is_gap:
                pcs, rail, _, _ = gap_plan(t, p_i, v_out, dt, sgn)
                pieces.extend(pcs)
                rails.append(rail)
            else:
                pieces.append(("fly", t, bounces[i + 1][0], p_i, v_out))
        else:
            # ---- 出口: 落下→右上がりレールに乗り、減速して頂点で静止(継ぎ目) ----
            pieces.append(("fly", t, t + EXIT_TF, p_i, v_out))
            R0, v_arr = fly(p_i, v_out, EXIT_TF)
            u_up = np.array([np.cos(RAIL_SLOPE), np.sin(RAIL_SLOPE), 0.0])
            v0s = max(0.35, float(v_arr @ u_up))
            t_stop, d_stop = roll_up_stop(v0s)
            pieces.append(("rollu", t + EXIT_TF, t + EXIT_TF + t_stop + 1.0,
                           R0, u_up, v0s))
            # レールは着地点の下にも延長して、入場側と同じ見た目にする
            rails.append((t + EXIT_TF - 0.6, t + EXIT_TF + t_stop + 1.0,
                          R0 - u_up * 1.7, R0 + u_up * (d_stop + 0.45),
                          u_up, d_stop + 2.15))
    return pts, pieces, normals, sizes, rails


def ball_pos(pts, pieces, t):
    if t <= pieces[0][1]:
        return pieces[0][3].copy()     # 開始前: 入場レール上で静止(暗闇の中)
    return path_pos(pieces, t)


# ---------------------------------------------------------------- 検証器

def find_collisions(pts, pieces, normals, sizes, rails):
    """最終軌道と全ての板・レールのめり込みを検出する(デバッグ用)。

    軌道を240Hzでサンプリングし、表示時間帯にボールが板・レールへ
    食い込んでいる区間、および板同士・レールとの箱交差(SAT)を列挙する。
    """
    end_t = pieces[-1][2]
    ts_all, ps_all = [], []
    for pc in pieces:
        t0, t1 = pc[1], min(pc[2], end_t)
        if t1 <= t0:
            continue
        n = max(4, int((t1 - t0) * 240))
        ss = np.linspace(t0, t1, n, endpoint=False)
        ts_all.append(ss)
        ps_all.append(np.stack([piece_pos(pc, s) for s in ss]))
    T = np.concatenate(ts_all)
    P = np.concatenate(ps_all)

    events = []
    margin = BALL_R * 0.6
    for j, ((hit_t, pos, _), nrm, w) in enumerate(zip(pts, normals, sizes)):
        c_top = pos - nrm * BALL_R
        u = np.array([nrm[1], -nrm[0], 0.0])
        rel = P - c_top
        inside = ((np.abs(rel @ u) < w / 2 + margin) &
                  (np.abs(rel[:, 2]) < w * 0.85 / 2 + margin) &
                  ((rel @ nrm) < margin) &
                  ((rel @ nrm) > -PLAT_H - margin) &
                  (T > hit_t - PLAT_LEAD) & (T < hit_t + PLAT_LIFE) &
                  (np.abs(T - hit_t) > 0.1))
        if inside.any():
            tt = T[inside]
            depth = float(np.max(-(rel[inside] @ nrm)))
            kind = "未来板" if tt.min() < hit_t else "過去板"
            events.append((j, kind, float(tt.min()), float(tt.max()), depth))

    # ボール vs レール(自分が転がっている間の接触は正当なので除外)
    for ridx, (r0, r1, a, b, u, length) in enumerate(rails):
        nr = rail_frame(u)
        center = (a + b) / 2 - nr * BALL_R
        rel = P - center
        inside = ((np.abs(rel @ u) < length / 2 + margin) &
                  (np.abs(rel[:, 2]) < 0.14 + margin) &
                  ((rel @ nr) < margin) &
                  ((rel @ nr) > -RAIL_H - margin) &
                  (T > r0 - PLAT_LEAD) & (T < r1 + PLAT_LIFE) &
                  ((T < r0 - 0.3) | (T > r1 + 0.6)))   # 進入/離脱の自レールかすりは正当
        if inside.any():
            tt = T[inside]
            events.append((ridx, "レール", float(tt.min()), float(tt.max()), 0.0))

    # 板・レール同士の交差(x-y平面の分離軸判定)
    def rect_axes(nrm2, w, h):
        u2 = np.array([nrm2[1], -nrm2[0]])
        return [(u2, w / 2), (np.asarray(nrm2), h / 2)]

    def sat_overlap(c1, ax1, c2, ax2):
        for ax, _ in ax1 + ax2:
            r1 = sum(abs(a @ ax) * hh for a, hh in ax1)
            r2 = sum(abs(a @ ax) * hh for a, hh in ax2)
            if abs((c2 - c1) @ ax) > r1 + r2:
                return False
        return True

    boxes = []
    for j in range(len(pts)):
        tj, pj, _ = pts[j]
        boxes.append((tj - PLAT_LEAD, tj + PLAT_LIFE,
                      (pj - normals[j] * (BALL_R + PLAT_H / 2))[:2],
                      rect_axes(normals[j][:2], sizes[j], PLAT_H), f"板{j}"))
    for ridx, (r0, r1, a, b, u, length) in enumerate(rails):
        nr = rail_frame(u)
        boxes.append((r0 - PLAT_LEAD, r1 + PLAT_LIFE,
                      ((a + b) / 2 - nr * (BALL_R + RAIL_H / 2))[:2],
                      rect_axes(nr[:2], length, RAIL_H), f"レール{ridx}"))
    for j in range(len(boxes)):
        f1, e1, c1, ax1, l1 = boxes[j]
        for k in range(j + 1, len(boxes)):
            f2, e2, c2, ax2, l2 = boxes[k]
            if f2 > e1 or f1 > e2:
                continue   # 表示時間帯が重ならない
            if sat_overlap(c1, ax1, c2, ax2):
                events.append((j, f"{l1}と{l2}が交差", max(f1, f2), min(e1, e2), 0.0))
    return events


# ---------------------------------------------------------------- 描画

def pitch_color(pitch):
    if pitch is None:
        return RAIL_COLOR.copy()
    hue = (pitch % 12) / 12.0
    r, g, b = colorsys.hsv_to_rgb(hue, 0.5, 1.0)
    return np.array([r * 255, g * 255, b * 255])


def make_stars(pts):
    rng = np.random.default_rng(7)
    y_top = pts[0][1][1] + 6.0
    y_bot = pts[-1][1][1] - 6.0
    n = int((y_top - y_bot) * 14)
    stars = np.stack([
        rng.uniform(-9, 9, n),
        rng.uniform(y_bot, y_top, n),
        rng.uniform(1.5, 14.0, n),
    ], axis=1)
    return stars


class Camera:
    """速度を先読みし、速いほど引く追従カメラ。

    - 進行方向に注視点をずらす(落下中はボールが画面上部に来て
      落ちていく先が見える)
    - 速度に応じてカメラ距離が変わる(遠近感とスピード感)
    - ボールが画面から出ない緩い制約(中心固定はしない)
    """

    OFFSET_DIR = np.array([0.0, 0.40, -1.0])
    LOOK_BASE = np.array([0.0, -0.30, 1.2])
    CLAMP_LO = np.array([-0.60, -0.28, -1.0])   # カメラがボールの上に取り残されない
    CLAMP_HI = np.array([0.60, 1.15, 1.0])      # ボールが画面上部に居るのは許す

    def __init__(self, ball):
        self.smooth = np.array(ball, dtype=float)
        self.vel = np.zeros(3)
        self.last = np.array(ball, dtype=float)
        self.dist = 4.4
        self.update(ball)

    def update(self, ball):
        ball = np.asarray(ball, dtype=float)
        v = (ball - self.last) * FPS
        self.last = ball.copy()
        self.vel += (v - self.vel) * 0.10           # 速度の平滑化
        speed = float(np.linalg.norm(self.vel))

        # 進行方向への先読み(下方向を重めに)
        ahead = np.clip(self.vel * 0.28,
                        [-0.45, -1.3, -0.2], [0.45, 0.35, 0.2])
        target = ball + ahead
        self.smooth += (target - self.smooth) * np.array([0.09, 0.13, 0.09])
        # ボールが画面から出ない保険(中心には固定しない)
        self.smooth = ball - np.clip(ball - self.smooth,
                                     self.CLAMP_LO, self.CLAMP_HI)

        # 速いほど引く(ゆっくり変化)
        want = float(np.clip(4.2 + speed * 0.32, 4.2, 6.6))
        self.dist += (want - self.dist) * 0.10

        off = self.OFFSET_DIR / np.linalg.norm(self.OFFSET_DIR)
        self.pos = self.smooth + off * self.dist
        # 速度に応じて視線も進行方向へ振る(落下中は下を見る)
        look_shift = np.clip(self.vel * 0.07, [-0.2, -0.5, -0.2], [0.2, 0.2, 0.2])
        look = self.smooth + self.LOOK_BASE + look_shift
        fwd = look - self.pos
        fwd /= np.linalg.norm(fwd)
        right = np.cross(fwd, np.array([0.0, 1.0, 0.0]))
        right /= np.linalg.norm(right)
        up = np.cross(right, fwd)
        self.mat = np.stack([right, up, fwd])
        self.look = look

    def project(self, p):
        v = self.mat @ (np.asarray(p, dtype=float) - self.pos)
        if v[2] < 0.05:
            return None
        sx = W / 2 + v[0] * FOCAL / v[2]
        sy = H * 0.46 - v[1] * FOCAL / v[2]
        return sx, sy, v[2]


def fog(depth):
    return float(np.clip((FOG_END - depth) / (FOG_END - FOG_START), 0.0, 1.0))


def shade(base, normal, k=1.0):
    lam = 0.5 + 0.5 * max(0.0, float(normal @ LIGHT_DIR))
    c = np.clip(base * lam * k, 0, 255)
    return tuple(int(v) for v in c)


def draw_box(draw, cam, center, u, n, half_u, half_d, thick, base, alpha_k):
    """uを横方向、nを上面法線とする薄い箱を描く。fog係数を返す(不可視ならNone)。"""
    d = np.array([0.0, 0.0, 1.0])
    corners = {}
    for ui in (-1, 1):
        for di in (-1, 1):
            top = center + u * (ui * half_u) + d * (di * half_d)
            corners[(ui, di, 1)] = cam.project(top)
            corners[(ui, di, 0)] = cam.project(top - n * thick)
    if any(pp is None for pp in corners.values()):
        return None
    depth = np.mean([pp[2] for pp in corners.values()])
    f = fog(depth)
    if f <= 0.01:
        return None
    alpha = int(235 * f * alpha_k)
    if alpha <= 2:
        return None

    def quad(keys, normal, k=1.0):
        draw.polygon([(corners[key][0], corners[key][1]) for key in keys],
                     fill=(*shade(base, normal, k), alpha))

    quad([(-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)], n)
    quad([(-1, -1, 1), (1, -1, 1), (1, -1, 0), (-1, -1, 0)],
         np.array([0.0, 0.0, -1.0]), 0.7)
    side = -1 if cam.pos[0] < center[0] else 1
    quad([(side, -1, 1), (side, 1, 1), (side, 1, 0), (side, -1, 0)],
         u * side, 0.55)
    return f


def draw_platform(draw, cam, center, n, pitch, t, hit_t, w_plat):
    glow = max(0.0, 1.0 - abs(t - hit_t) * 5.0)
    # 着地のPLAT_LEAD秒前にフェードイン、着地後PLAT_LIFE秒でフェードアウト
    fade_in = float(np.clip((t - (hit_t - PLAT_LEAD)) / 0.4, 0.0, 1.0))
    fade_out = float(np.clip(1.0 - (t - hit_t - (PLAT_LIFE - 0.8)) / 0.8, 0.0, 1.0))
    fade = fade_in * fade_out
    if fade <= 0.01:
        return
    base = pitch_color(pitch) * (1.0 + 0.6 * glow)
    u = np.array([n[1], -n[0], 0.0])
    c_top = np.asarray(center) - n * BALL_R
    f = draw_box(draw, cam, c_top, u, n, w_plat / 2, w_plat * 0.85 / 2,
                 PLAT_H, base, fade)
    if f and glow > 0.02:
        pp = cam.project(center)
        if pp:
            rr = (0.5 + 0.5 * glow) * FOCAL * w_plat / pp[2]
            col = tuple(int(v) for v in np.clip(base, 0, 255))
            draw.ellipse([pp[0] - rr, pp[1] - rr * 0.4, pp[0] + rr, pp[1] + rr * 0.4],
                         fill=(*col, int(70 * glow * f)))


def draw_rail(draw, cam, rail, t):
    """無音レール(グレー、発光なし)を描く。"""
    r0, r1, a, b, u, length = rail
    fade_in = float(np.clip((t - (r0 - PLAT_LEAD)) / 0.4, 0.0, 1.0))
    fade_out = float(np.clip(1.0 - (t - r1 - (PLAT_LIFE - 0.8)) / 0.8, 0.0, 1.0))
    fade = fade_in * fade_out
    if fade <= 0.01:
        return
    nr = rail_frame(u)
    center = (a + b) / 2 - nr * BALL_R
    draw_box(draw, cam, center, u, nr, length / 2 + 0.05, 0.14,
             RAIL_H, RAIL_COLOR, fade)


def draw_stars(draw, cam, stars):
    rel_y = np.abs(stars[:, 1] - cam.pos[1])
    for (x, y, z) in stars[rel_y < 12.0]:
        p = cam.project((x, y, z))
        if p is None:
            continue
        r = max(1.0, 2.6 / p[2] * 4.0)
        a = int(110 * fog(p[2] * 0.55))
        if a > 8:
            draw.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r],
                         fill=(255, 255, 255, a))


def draw_ball(draw, cam, pos, trail):
    n = max(len(trail), 1)
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


def make_background():
    grad = np.linspace(0, 1, H)[:, None] * np.ones((1, W))
    img = np.zeros((H, W, 3), dtype=np.uint8)
    for c in range(3):
        img[:, :, c] = (BG_TOP[c] + (BG_BOTTOM[c] - BG_TOP[c]) * grad).astype(np.uint8)
    return Image.fromarray(img)


# ---------------------------------------------------------------- メイン

def render(pts, pieces, normals, sizes, rails, audio: Path, output: Path,
           end_time: float):
    # 継ぎ目: 開始=入場レールの頂点(v=0)、終了=出口レールの頂点(v=0)
    t_start = pieces[0][1]
    total = pieces[-1][2] - 1.0 + 0.70   # 頂点で0.7秒の溜め(カメラも収束)
    n_frames = int((total - t_start) * FPS)
    delay_ms = int(round(max(0.0, -t_start) * 1000))
    bg = make_background()
    stars = make_stars(pts)

    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
         "-i", "-", "-i", str(audio),
         "-af", f"adelay={delay_ms}:all=1,apad",
         "-t", f"{n_frames / FPS:.3f}",
         "-c:v", "libx264", "-preset", "fast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
         str(output)],
        stdin=subprocess.PIPE,
    )

    cam = Camera(ball_pos(pts, pieces, t_start))
    for _ in range(150):
        cam.update(ball_pos(pts, pieces, t_start))   # 暖機: 収束状態から開始
    trail = []
    settle_t = pieces[-1][2] - 1.0        # 頂点到達時刻(以降は溜め)
    for f in range(n_frames):
        t = t_start + f / FPS
        bp = ball_pos(pts, pieces, t)
        cam.update(bp)
        if t >= settle_t:                 # 溜め中はカメラを素早く据わらせる
            for _ in range(3):
                cam.update(bp)

        frame = bg.copy()
        draw = ImageDraw.Draw(frame, "RGBA")
        draw_stars(draw, cam, stars)

        # レール(板より奥に描く)
        for rail in rails:
            mid_y = (rail[2][1] + rail[3][1]) / 2
            if rail[0] - PLAT_LEAD < t < rail[1] + PLAT_LIFE and \
               abs(mid_y - cam.smooth[1]) < 9.0:
                draw_rail(draw, cam, rail, t)

        visible = []
        for (pt, pos, pitch), nrm, w_plat in zip(pts, normals, sizes):
            if abs(pos[1] - cam.smooth[1]) > 9.0:
                continue
            pr = cam.project(pos)
            if pr is None:
                continue
            visible.append((pr[2], pt, pos, nrm, pitch, w_plat))
        for depth, pt, pos, nrm, pitch, w_plat in sorted(visible, key=lambda v: -v[0]):
            draw_platform(draw, cam, pos, nrm, pitch, t, pt, w_plat)

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


def export_scene(pts, pieces, normals, sizes, rails, output: Path,
                 end_time: float):
    """Blenderレンダラー用にシーンデータをJSONへ書き出す。

    ボール位置とカメラ注視点はフレームごとにサンプル済みの値を渡すので、
    Blender側は物理を知らなくてよい。
    """
    import json
    t_start = pieces[0][1]
    total = pieces[-1][2] - 1.0 + 0.70
    n_frames = int((total - t_start) * FPS)
    cam = Camera(ball_pos(pts, pieces, t_start))
    for _ in range(150):
        cam.update(ball_pos(pts, pieces, t_start))   # 暖機: 収束状態から開始
    ball_track, cam_track, spin_track = [], [], []
    theta = 0.0
    omega = 0.0
    prev = None
    settle_t = pieces[-1][2] - 1.0
    for f in range(n_frames):
        t = t_start + f / FPS
        bp = ball_pos(pts, pieces, t)
        cam.update(bp)
        if t >= settle_t:
            for _ in range(3):
                cam.update(bp)
        ball_track.append([float(x) for x in bp])
        cam_track.append({"pos": [float(x) for x in cam.pos],
                          "look": [float(x) for x in cam.look]})
        # 回転は慣性を持つ: 空中では保存、板ヒットで3割だけ変化(ガラスの
        # 低摩擦)、レール上では転がり条件 ω=v/r に強く収束する
        if prev is not None:
            vx = (bp[0] - prev[0]) * FPS
            omega_target = vx / BALL_R
            on_rail = any(r0 <= t <= r1 for (r0, r1, *_) in
                          [(r[0], r[1]) for r in rails])
            hit_now = any(abs(t - pt[0]) < 0.5 / FPS for pt in pts)
            if on_rail:
                omega += (omega_target - omega) * 0.35   # 転がりに収束
            elif hit_now:
                omega += (omega_target - omega) * 0.3    # 接触の摩擦で少し変化
            # 空中: omega保存(空気抵抗でわずかに減衰)
            omega *= 0.999
            theta += omega / FPS
        spin_track.append(theta)
        prev = bp
    start_pos = pieces[0][3]                     # 入場の頂点(=最初のフレーム)
    end_piece = pieces[-1]                        # 出口レールの登り
    _, d_stop_ = roll_up_stop(end_piece[5])
    seam_pos = end_piece[3] + end_piece[4] * d_stop_   # 出口の頂点(=最終フレーム)
    data = {
        "fps": FPS,
        "title": TITLE,
        "start_pos": [float(x) for x in start_pos],
        "end_pos": [float(x) for x in seam_pos],
        "start_anchor": [float(x) for x in start_pos],
        "end_anchor": [float(x) for x in seam_pos],
        "t_start": t_start,
        "n_frames": n_frames,
        "audio_delay_ms": int(round(max(0.0, -t_start) * 1000)),
        "duration_s": round(n_frames / FPS, 3),
        "ball_r": BALL_R,
        "plat_h": PLAT_H,
        "rail_h": RAIL_H,
        "plat_lead": PLAT_LEAD,
        "plat_life": PLAT_LIFE,
        "ball": ball_track,
        "spin": spin_track,
        "cam": cam_track,
        "plates": [
            {"t": float(t), "pos": [float(x) for x in pos],
             "normal": [float(x) for x in n], "pitch": int(pitch), "w": float(w)}
            for (t, pos, pitch), n, w in zip(pts, normals, sizes)
        ],
        "rails": [
            {"t0": float(r0), "t1": float(r1),
             "a": [float(x) for x in a], "b": [float(x) for x in b],
             "u": [float(x) for x in u], "length": float(ln)}
            for (r0, r1, a, b, u, ln) in rails
        ],
    }
    output.write_text(json.dumps(data))
    print(f"シーン書き出し: {output} ({n_frames}フレーム)")


def main():
    parser = argparse.ArgumentParser(description="メロディ同期3D落下ボール動画を生成")
    parser.add_argument("midi", type=Path, help="メロディMIDI")
    parser.add_argument("--track", type=str, default=None,
                        help="使用トラック番号(カンマ区切りで複数可)")
    parser.add_argument("--audio", type=Path, required=True, help="重ねる音声(MP3)")
    parser.add_argument("-o", "--output", type=Path, required=True, help="出力MP4")
    parser.add_argument("--min-pitch", type=int, default=55,
                        help="これ未満の低音を捨てる(convert.pyと同値にすること)")
    parser.add_argument("--skip", type=float, default=0,
                        help="先頭N秒を捨てて詰める(convert.pyと同値にすること)")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="テンポ倍率(convert.pyと同値にすること)")
    parser.add_argument("--duration", type=float, default=None,
                        help="先頭からこの秒数だけ描画(プレビュー用)")
    parser.add_argument("--long-note", choices=["keep", "cut", "split"],
                        default="keep",
                        help="長い音の扱い(音源と同じ設定にすること)")
    parser.add_argument("--long-note-len", type=float, default=0.5,
                        help="長音の閾値秒数(音源と同じ設定にすること)")
    parser.add_argument("--check", action="store_true",
                        help="レンダリングせず衝突検証だけ実行する")
    parser.add_argument("--export", type=Path, default=None,
                        help="Blender用シーンJSONを書き出して終了する")
    args = parser.parse_args()

    bounces = load_bounces(args.midi, args.track,
                           args.long_note, args.long_note_len,
                           args.min_pitch)
    end_time = bounces[-1][0]
    if args.duration:
        end_time = min(end_time, args.duration)
        bounces = [b for b in bounces if b[0] <= end_time]
    if args.skip > 0:
        # convert.pyと同じ順序: duration切り出し → 先頭スキップ → テンポ縮尺
        bounces = [(t - args.skip, p) for t, p in bounces if t >= args.skip]
        end_time -= args.skip
    if args.speed != 1.0:
        bounces = [(t / args.speed, p) for t, p in bounces]
        end_time /= args.speed
    pts, pieces, normals, sizes, rails = build_path(bounces)
    print(f"バウンド数: {len(pts)}, レール数: {len(rails)}, 長さ: {end_time:.1f}s")

    events = find_collisions(pts, pieces, normals, sizes, rails)
    if events:
        print(f"⚠ めり込み検出: {len(events)}件")
        for j, kind, t0, t1, depth in events:
            print(f"  {j:3d} ({kind}) t={t0:6.2f}-{t1:6.2f}s 深さ{depth*100:.1f}cm")
    else:
        print("めり込みなし ✓")
    if args.check:
        return
    if args.export:
        export_scene(pts, pieces, normals, sizes, rails, args.export, end_time)
        return
    render(pts, pieces, normals, sizes, rails, args.audio, args.output, end_time)
    print(f"完了: {args.output}")


if __name__ == "__main__":
    main()
