#!/usr/bin/env python3
"""シーンJSON(make_video.py --export)をBlenderでフォトリアルにレンダリングする。

参考動画の見た目を再現する:
  - ガラスのビー玉(内部に色の渦)
  - 金属の小さな板(鳴る板は音程ごとの色、レールは銀)
  - すりガラス風の壁に落ちる柔らかい影
  - 浅い被写界深度

座標系: 計算側は x=右, y=上, z=奥。Blenderは z=上 なので (x, z, y) に写像する。

実行(専用venv):
  vendor/bpy-venv/bin/python blender_render.py scene.json out_dir \
      [--frame N] [--start A --end B] [--scale 0.5] [--samples 32]
"""

import argparse
import colorsys
import json
import math
import sys
from pathlib import Path

import bpy


def to_b(v):
    """計算座標(x,y=上,z=奥) → Blender座標(x, y=奥, z=上)"""
    return (v[0], v[2], v[1])


def pitch_rgb(pitch):
    hue = (pitch % 12) / 12.0
    r, g, b = colorsys.hsv_to_rgb(hue, 0.55, 0.75)
    return (r, g, b, 1.0)


def clear_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def make_material(name, color, metallic=0.0, rough=0.4, transmission=0.0,
                  emission=None):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = color
    bsdf.inputs["Metallic"].default_value = metallic
    bsdf.inputs["Roughness"].default_value = rough
    if transmission:
        bsdf.inputs["Transmission Weight"].default_value = transmission
        bsdf.inputs["IOR"].default_value = 1.45
        for attr in ("use_raytrace_refraction", "use_screen_refraction"):
            if hasattr(mat, attr):
                setattr(mat, attr, True)
    if emission:
        bsdf.inputs["Emission Color"].default_value = emission
        bsdf.inputs["Emission Strength"].default_value = 1.5
    return mat


def make_marble(r):
    """ガラスのビー玉: 外殻はガラス、内部に色の渦。"""
    bpy.ops.mesh.primitive_uv_sphere_add(radius=r, segments=48, ring_count=24)
    ball = bpy.context.object
    ball.name = "marble"
    bpy.ops.object.shade_smooth()
    glass = make_material("glass", (1, 1, 1, 1), rough=0.02, transmission=1.0)
    ball.data.materials.append(glass)

    # 内部の色の渦: 2本のツイストした帯(本物のビー玉の作り)
    for k, (rot, cols) in enumerate([
        (0.0, [(0.95, 0.55, 0.10, 1), (0.15, 0.65, 0.35, 1)]),
        (math.radians(90), [(0.15, 0.45, 0.85, 1), (0.9, 0.9, 0.95, 1)]),
    ]):
        bpy.ops.mesh.primitive_torus_add(major_radius=r * 0.34,
                                         minor_radius=r * 0.10,
                                         major_segments=64, minor_segments=12)
        core = bpy.context.object
        core.name = f"marble_core{k}"
        core.rotation_euler = (rot, rot * 0.5, 0)
        bpy.ops.object.shade_smooth()
        mod = core.modifiers.new("twist", "SIMPLE_DEFORM")
        mod.deform_method = "TWIST"
        mod.angle = math.radians(300)
        mat = bpy.data.materials.new(f"core{k}")
        mat.use_nodes = True
        nt = mat.node_tree
        bsdf = nt.nodes["Principled BSDF"]
        ramp = nt.nodes.new("ShaderNodeValToRGB")
        ramp.color_ramp.elements[0].color = cols[0]
        ramp.color_ramp.elements[1].color = cols[1]
        noise = nt.nodes.new("ShaderNodeTexNoise")
        noise.inputs["Scale"].default_value = 5.0
        nt.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
        nt.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
        nt.links.new(ramp.outputs["Color"], bsdf.inputs["Emission Color"])
        bsdf.inputs["Emission Strength"].default_value = 1.1   # 渦が内側から光る
        bsdf.inputs["Roughness"].default_value = 0.3
        core.data.materials.append(mat)
        core.parent = ball
    return ball


WALL_Y = 0.25   # 壁のBlender Y位置(奥行き)


def add_wall_pin(parent, mat, at_local=(0.0, 0.0, 0.0)):
    """パーツから壁まで伸びる金属の支柱を付ける(壁マウント感)。"""
    world = parent.matrix_world @ __import__("mathutils").Vector(at_local)
    length = max(0.05, WALL_Y - world.y)
    bpy.ops.mesh.primitive_cylinder_add(radius=0.011, depth=length, vertices=16)
    pin = bpy.context.object
    pin.rotation_euler = (math.radians(90), 0, 0)
    pin.location = (world.x, world.y + length / 2, world.z)
    pin.data.materials.append(mat)
    pin.parent = parent
    pin.matrix_parent_inverse = parent.matrix_world.inverted()
    # 壁側の台座
    bpy.ops.mesh.primitive_cylinder_add(radius=0.028, depth=0.015, vertices=20)
    base = bpy.context.object
    base.rotation_euler = (math.radians(90), 0, 0)
    base.location = (world.x, WALL_Y - 0.008, world.z)
    base.data.materials.append(mat)
    base.parent = parent
    base.matrix_parent_inverse = parent.matrix_world.inverted()


def make_plate(name, w, h_thick, depth, mat):
    bpy.ops.mesh.primitive_cube_add(size=1)   # size=1は一辺1mの立方体
    ob = bpy.context.object
    ob.name = name
    ob.scale = (w, depth, h_thick)
    bpy.ops.object.transform_apply(scale=True)
    # 角を少し丸める
    bev = ob.modifiers.new("bevel", "BEVEL")
    bev.width = min(0.01, h_thick / 2.5)
    bev.segments = 3
    ob.data.materials.append(mat)
    return ob


def _child(ob, parent, loc, rot=(0, 0, 0)):
    """objをparentの子にしてローカル座標で配置する。"""
    ob.parent = parent
    ob.location = loc
    ob.rotation_euler = rot
    return ob


def add_plate_details(plate, w, h, pin_mat):
    """板の背面→壁の支柱と台座、前面のビス(参考画像のマウント構造)。"""
    depth = w * 0.85
    stem_len = WALL_Y - depth / 2 - 0.01
    bpy.ops.mesh.primitive_cylinder_add(radius=0.012, depth=stem_len, vertices=16)
    _child(bpy.context.object, plate,
           (0, depth / 2 + stem_len / 2, 0), (math.radians(90), 0, 0))
    bpy.context.object.data.materials.append(pin_mat)
    bpy.ops.mesh.primitive_cylinder_add(radius=0.026, depth=0.012, vertices=20)
    _child(bpy.context.object, plate,
           (0, depth / 2 + stem_len, 0), (math.radians(90), 0, 0))
    bpy.context.object.data.materials.append(pin_mat)
    # 前面中央の小さなビス
    bpy.ops.mesh.primitive_cylinder_add(radius=0.013, depth=0.008, vertices=16)
    _child(bpy.context.object, plate,
           (0, -depth / 2 - 0.003, 0), (math.radians(90), 0, 0))
    bpy.context.object.data.materials.append(pin_mat)


def make_rail_assembly(length, ball_r, rail_mat, pin_mat):
    """2本の平行な丸棒+横桟+壁支柱のレール(本物のビー玉レール構造)。

    ルート(空オブジェクト)の原点はボール中心線からball_r下の接触面。
    棒の間にボールが沈む分(δ)は棒を持ち上げて補正する。
    """
    sep = 0.035                     # 棒の中心の横(奥行き)距離の半分
    bar_r = 0.013
    delta = ball_r - math.sqrt(ball_r ** 2 - sep ** 2)
    bpy.ops.object.empty_add()
    root = bpy.context.object

    for sy in (-sep, sep):
        bpy.ops.mesh.primitive_cylinder_add(radius=bar_r, depth=length + 0.02,
                                            vertices=20)
        bar = _child(bpy.context.object, root,
                     (0, sy, delta - bar_r), (0, math.radians(90), 0))
        bar.data.materials.append(rail_mat)

    n_rung = max(2, int(length / 0.45))
    for i in range(n_rung):
        x = -length / 2 + (i + 0.5) * length / n_rung
        bpy.ops.mesh.primitive_cylinder_add(radius=0.007, depth=sep * 2 + 0.02,
                                            vertices=12)
        rung = _child(bpy.context.object, root,
                      (x, 0, delta - bar_r * 2), (math.radians(90), 0, 0))
        rung.data.materials.append(rail_mat)

    for fx in (-0.38, 0.38):
        stem_len = WALL_Y - sep - 0.02
        bpy.ops.mesh.primitive_cylinder_add(radius=0.011, depth=stem_len,
                                            vertices=16)
        stem = _child(bpy.context.object, root,
                      (fx * length, sep + stem_len / 2, delta - bar_r),
                      (math.radians(90), 0, 0))
        stem.data.materials.append(pin_mat)
        bpy.ops.mesh.primitive_cylinder_add(radius=0.024, depth=0.012, vertices=20)
        cap = _child(bpy.context.object, root,
                     (fx * length, sep + stem_len, delta - bar_r),
                     (math.radians(90), 0, 0))
        cap.data.materials.append(pin_mat)
    return root


JP_FONT = "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc"


def add_wall_text(text, location, size, mat, font=None, extrude=0.004, tilt=0.0):
    """壁面に貼り付くエンボス文字。locationはBlender座標(x, y=奥行き, z=高さ)。"""
    bpy.ops.object.text_add()
    ob = bpy.context.object
    ob.data.body = text
    if font is not None:
        ob.data.font = font
    ob.data.size = size
    ob.data.extrude = extrude
    ob.data.align_x = "CENTER"
    ob.data.align_y = "CENTER"
    ob.rotation_euler = (math.radians(90), tilt, 0)
    ob.location = location
    ob.data.materials.append(mat)
    return ob


def keyframe_visibility(ob, f_in, f_out, fps):
    """スケールで出現/消滅を表現する(0.25秒でポップイン/アウト)。"""
    pop = max(2, int(0.25 * fps))
    ob.scale = (0, 0, 0)
    ob.keyframe_insert("scale", frame=max(0, f_in - 1))
    ob.scale = (1, 1, 1)
    ob.keyframe_insert("scale", frame=f_in + pop)
    ob.keyframe_insert("scale", frame=f_out)
    ob.scale = (0, 0, 0)
    ob.keyframe_insert("scale", frame=f_out + pop)


def build_scene(sc, engine="eevee", samples=48, scale=1.0):
    """scene.jsonからBlenderシーンを構築する。デバッグからも呼べる。"""
    fps = sc["fps"]
    n_frames = sc["n_frames"]
    clear_scene()
    scene = bpy.context.scene
    if engine == "cycles":
        scene.render.engine = "CYCLES"
        scene.cycles.samples = samples
        scene.cycles.use_denoising = True
        prefs = bpy.context.preferences.addons["cycles"].preferences
        prefs.compute_device_type = "OPTIX"
        prefs.get_devices()
        for d in prefs.devices:
            d.use = True
        scene.cycles.device = "GPU"
    else:
        scene.render.engine = "BLENDER_EEVEE"
        scene.eevee.taa_render_samples = max(32, samples)
        for attr, val in [("use_raytracing", True), ("use_gtao", True),
                          ("use_bloom", False)]:
            if hasattr(scene.eevee, attr):
                setattr(scene.eevee, attr, val)
    scene.render.resolution_x = int(1080 * scale)
    scene.render.resolution_y = int(1920 * scale)
    scene.render.fps = fps
    scene.frame_start = 0
    scene.frame_end = n_frames - 1
    scene.render.image_settings.file_format = "PNG"

    # ---- ワールド(淡い環境光) ----
    world = bpy.data.worlds.new("world")
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    bg.inputs["Color"].default_value = (0.35, 0.42, 0.5, 1)
    bg.inputs["Strength"].default_value = 0.25

    # ---- 壁(すりガラス風の背景、影を受ける) ----
    wall_mat = bpy.data.materials.new("wall")
    wall_mat.use_nodes = True
    nt = wall_mat.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    bsdf.inputs["Roughness"].default_value = 0.9
    grad = nt.nodes.new("ShaderNodeTexGradient")
    grad.gradient_type = "DIAGONAL"
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = (0.13, 0.22, 0.28, 1)   # 深い青緑
    ramp.color_ramp.elements[1].color = (0.42, 0.36, 0.27, 1)   # 温かいグレー金
    coord = nt.nodes.new("ShaderNodeTexCoord")
    # スクリーン座標に固定: カメラがどこへ動いても画面内の色味が一定
    nt.links.new(coord.outputs["Window"], grad.inputs["Vector"])
    nt.links.new(grad.outputs["Color"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])

    bpy.ops.mesh.primitive_plane_add(size=400)
    wall = bpy.context.object
    wall.name = "wall"
    wall.rotation_euler = (math.radians(90), 0, 0)   # 垂直の壁
    wall.location = (0, WALL_Y, 0)                    # ボールのすぐ奥(マウント感)
    wall.data.materials.append(wall_mat)

    # ---- ライト(左上前方から: 壁に柔らかい影を落とす) ----
    # カメラに親子付けし、降下してもシーンの明るさが変わらないようにする
    bpy.ops.object.light_add(type="AREA", location=(-2.0, -4.5, 2.5))
    key = bpy.context.object
    key.data.energy = 260
    key.data.size = 1.2
    key.rotation_euler = (math.radians(55), 0, math.radians(-30))
    bpy.ops.object.light_add(type="AREA", location=(2.5, -3.5, -1.0))
    fill = bpy.context.object
    fill.data.energy = 55
    fill.data.size = 4.0
    fill.rotation_euler = (math.radians(95), 0, math.radians(35))

    # ---- 壁のタイトルと音符の装飾 ----
    title = sc.get("title")
    font = None
    try:
        font = bpy.data.fonts.load(JP_FONT)
    except Exception:
        pass
    text_mat = make_material("walltext", (0.30, 0.33, 0.40, 1), rough=0.75)
    if title:
        # 最初の板の少し上: 導入の落下〜最初の音の間、画面に留まる位置
        p0 = sc["plates"][0]["pos"]
        add_wall_text(title, (p0[0], WALL_Y - 0.012, p0[1] + 0.72),
                      0.30, text_mat, font)
    faint_mat = make_material("wallnote", (0.47, 0.50, 0.56, 1), rough=0.85)
    glyphs = ["\u266a", "\u266b", "\u2669"]
    ys2 = [b[1] for b in sc["ball"]]
    yy = max(ys2) - 3.0
    gi = 0
    while yy > min(ys2) - 2.0:
        gx = (0.95 + 0.35 * ((gi * 7) % 3) / 2) * (1 if gi % 2 == 0 else -1)
        add_wall_text(glyphs[gi % 3], (gx, WALL_Y - 0.012, yy),
                      0.15, faint_mat, font, extrude=0.002,
                      tilt=math.radians(-12 + (gi * 11) % 24))
        gi += 1
        yy -= 3.8

    # ---- 経路沿いの物理ライト(降下に沿って交互に点在、画面内に約2個) ----
    ys = [b[1] for b in sc["ball"]]
    y_top, y_bot = max(ys) + 1.0, min(ys) - 1.0
    i_l = 0
    y = y_top
    while y > y_bot:
        side = 1 if i_l % 2 == 0 else -1
        warm = i_l % 2 == 0
        bpy.ops.object.light_add(type="POINT",
                                 location=(side * 1.7, -0.55, y))
        lp = bpy.context.object
        lp.data.energy = 90
        lp.data.shadow_soft_size = 0.25
        lp.data.color = (1.0, 0.85, 0.65) if warm else (0.65, 0.8, 1.0)
        i_l += 1
        y -= 3.4

    # ---- ビー玉 ----
    ball = make_marble(sc["ball_r"])
    spin = sc.get("spin", [0.0] * len(sc["ball"]))
    for f, p in enumerate(sc["ball"]):
        ball.location = to_b(p)
        ball.keyframe_insert("location", frame=f)
        # 物理的な転がり回転(計算側で積分済み)。回転軸は奥行き=BlenderのY
        ball.rotation_euler = (0.0, spin[f], 0.0)
        ball.keyframe_insert("rotation_euler", frame=f)

    # ---- 鳴る板 ----
    t_start = sc["t_start"]
    pin_mat = make_material("pin", (0.62, 0.63, 0.66, 1), metallic=1.0, rough=0.3)
    plate_mats = {}
    for i, pl in enumerate(sc["plates"]):
        pc = pl["pitch"] % 12
        if pc not in plate_mats:
            plate_mats[pc] = make_material(
                f"plate{pc}", pitch_rgb(pl["pitch"]), metallic=0.85, rough=0.24)
            bsdf = plate_mats[pc].node_tree.nodes["Principled BSDF"]
            if "Coat Weight" in bsdf.inputs:
                bsdf.inputs["Coat Weight"].default_value = 0.6
        ob = make_plate(f"p{i}", pl["w"], sc["plat_h"], pl["w"] * 0.85,
                        plate_mats[pc])
        n = pl["normal"]
        pos = pl["pos"]
        c = [pos[0] - n[0] * (sc["ball_r"] + sc["plat_h"] / 2),
             pos[1] - n[1] * (sc["ball_r"] + sc["plat_h"] / 2), pos[2]]
        ob.location = to_b(c)
        ob.rotation_euler = (0, math.atan2(n[0], n[1]), 0)  # y軸(奥行き)回り
        add_plate_details(ob, pl["w"], sc["plat_h"], pin_mat)
        f_in = int((pl["t"] - sc["plat_lead"] - t_start) * fps)
        f_out = int((pl["t"] + sc["plat_life"] - t_start) * fps)
        keyframe_visibility(ob, f_in, f_out, fps)
        # 着弾の瞬間: 板が法線方向に沈んで跳ね返る+一瞬膨らむ(触れてる感)
        f_hit = int((pl["t"] - t_start) * fps)
        nb = to_b(n)
        base = ob.location.copy()
        ob.keyframe_insert("location", frame=f_hit)
        ob.location = (base.x - nb[0] * 0.03, base.y - nb[1] * 0.03,
                       base.z - nb[2] * 0.03)
        ob.keyframe_insert("location", frame=f_hit + 2)
        ob.location = base
        ob.keyframe_insert("location", frame=f_hit + 8)
        ob.scale = (1, 1, 1)
        ob.keyframe_insert("scale", frame=f_hit)
        ob.scale = (1.15, 1.15, 1.15)
        ob.keyframe_insert("scale", frame=f_hit + 2)
        ob.scale = (1, 1, 1)
        ob.keyframe_insert("scale", frame=f_hit + 7)

    # ---- レール(銀の金属) ----
    # レールは「音が鳴らない」素材感: 黒に近いマットなゴム
    rail_mat = make_material("rail", (0.085, 0.085, 0.095, 1), metallic=0.0, rough=0.88)
    for i, r in enumerate(sc["rails"]):
        a, b, u = r["a"], r["b"], r["u"]
        ob = make_rail_assembly(r["length"], sc["ball_r"], rail_mat, pin_mat)
        ob.name = f"rail{i}"
        nr = [-u[1], u[0], 0.0]
        if nr[1] < 0:
            nr = [-nr[0], -nr[1], 0.0]
        # ルート原点 = ボール接触面(中心線からball_r下)
        c = [(a[0] + b[0]) / 2 - nr[0] * sc["ball_r"],
             (a[1] + b[1]) / 2 - nr[1] * sc["ball_r"],
             (a[2] + b[2]) / 2]
        ob.location = to_b(c)
        ob.rotation_euler = (0, math.atan2(-u[1], u[0]), 0)
        f_in = int((r["t0"] - sc["plat_lead"] - t_start) * fps)
        f_out = int((r["t1"] + sc["plat_life"] - t_start) * fps)
        keyframe_visibility(ob, f_in, f_out, fps)

    # ---- カメラ(注視点トラックを追従、浅い被写界深度) ----
    target = bpy.data.objects.new("target", None)
    bpy.context.collection.objects.link(target)
    bpy.ops.object.camera_add()
    cam = bpy.context.object
    scene.camera = cam
    cam.data.lens = 42
    cam.data.dof.use_dof = True
    cam.data.dof.focus_object = ball
    cam.data.dof.aperture_fstop = 1.4
    tr = cam.constraints.new("TRACK_TO")
    tr.target = target
    # ライトをカメラ追従にする(相対位置を保ったまま)
    for light in (key, fill):
        base = light.location.copy()
        light.parent = cam
        light.matrix_parent_inverse = cam.matrix_world.inverted()
        light.location = base
    for f, cs in enumerate(sc["cam"]):
        cam.location = to_b(cs["pos"])
        target.location = to_b(cs["look"])
        cam.keyframe_insert("location", frame=f)
        target.keyframe_insert("location", frame=f)

    return {"ball": ball, "cam": cam, "n_frames": n_frames}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scene", type=Path)
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--frame", type=int, default=None, help="1フレームだけ描く")
    ap.add_argument("--start", type=int, default=None)
    ap.add_argument("--end", type=int, default=None)
    ap.add_argument("--scale", type=float, default=1.0, help="解像度倍率")
    ap.add_argument("--samples", type=int, default=48)
    ap.add_argument("--engine", choices=["eevee", "cycles"], default="eevee",
                    help="eevee=高速(既定) / cycles=最高品質")
    args = ap.parse_args()

    sc = json.loads(args.scene.read_text())
    args.outdir.mkdir(parents=True, exist_ok=True)
    objs = build_scene(sc, args.engine, args.samples, args.scale)
    n_frames = objs["n_frames"]
    scene = bpy.context.scene

    # ---- レンダリング ----
    if args.frame is not None:
        scene.frame_set(args.frame)
        scene.render.filepath = str(args.outdir / f"frame_{args.frame:05d}.png")
        bpy.ops.render.render(write_still=True)
        print(f"1フレーム描画: {scene.render.filepath}")
    else:
        f0 = args.start if args.start is not None else 0
        f1 = args.end if args.end is not None else n_frames - 1
        scene.frame_start = f0
        scene.frame_end = f1
        scene.render.filepath = str(args.outdir) + "/f_"
        bpy.ops.render.render(animation=True)
        print(f"連番描画完了: {args.outdir}")


if __name__ == "__main__":
    main()
