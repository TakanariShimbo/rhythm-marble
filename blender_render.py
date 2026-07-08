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


def hex_rgb(h):
    """"#RRGGBB" → (r, g, b) 0-1"""
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


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


def make_marble(r, band_colors=None, glow=1.1):
    """ガラスのビー玉: 外殻はガラス、内部に色の渦。

    band_colors: 渦の色4つ [(r,g,b,1)×4]。未指定なら既定の配色。
    glow: 渦の発光強度(wall.jsonのmarble_glow)。
    """
    if band_colors is None:
        band_colors = [(0.95, 0.55, 0.10, 1), (0.15, 0.65, 0.35, 1),
                       (0.15, 0.45, 0.85, 1), (0.9, 0.9, 0.95, 1)]
    bpy.ops.mesh.primitive_uv_sphere_add(radius=r, segments=48, ring_count=24)
    ball = bpy.context.object
    ball.name = "marble"
    bpy.ops.object.shade_smooth()
    glass = make_material("glass", (1, 1, 1, 1), rough=0.02, transmission=1.0)
    ball.data.materials.append(glass)

    # 内部の色の渦: 2本のツイストした帯(本物のビー玉の作り)
    for k, (rot, cols) in enumerate([
        (0.0, band_colors[0:2]),
        (math.radians(90), band_colors[2:4]),
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
        bsdf.inputs["Emission Strength"].default_value = glow   # 渦が内側から光る
        bsdf.inputs["Roughness"].default_value = 0.3
        core.data.materials.append(mat)
        core.parent = ball
    return ball


WALL_Y = 0.25   # 壁のBlender Y位置(奥行き)


def add_picture_spot(center, energy=120):
    """額縁を上前方から照らす美術館風スポット。壁に光のプールを作る。"""
    tgt = bpy.data.objects.new("spot_target", None)
    bpy.context.collection.objects.link(tgt)
    tgt.location = center
    bpy.ops.object.light_add(
        type="SPOT", location=(center[0], center[1] - 1.6, center[2] + 1.3))
    sp = bpy.context.object
    sp.data.energy = energy
    sp.data.spot_size = math.radians(55)
    sp.data.spot_blend = 0.6                  # 縁を柔らかく
    sp.data.color = (1.0, 0.93, 0.80)         # 温かいギャラリー光
    sp.data.shadow_soft_size = 0.15
    con = sp.constraints.new("TRACK_TO")
    con.target = tgt


def setup_stylize(scene, bloom=0.35, vignette=0.30):
    """コンポジタ仕上げ: ブルーム(発光のにじみ)+ビネット(周辺減光)。

    Blender 5.0の新コンポジタAPI(compositing_node_group)前提。
    """
    nt = bpy.data.node_groups.new("stylize", "CompositorNodeTree")
    nt.interface.new_socket("Image", in_out="OUTPUT",
                            socket_type="NodeSocketColor")
    scene.compositing_node_group = nt
    scene.render.use_compositing = True
    rl = nt.nodes.new("CompositorNodeRLayers")
    out = nt.nodes.new("NodeGroupOutput")
    src = rl.outputs["Image"]
    if bloom > 0:
        glare = nt.nodes.new("CompositorNodeGlare")
        glare.inputs["Type"].default_value = "Bloom"
        glare.inputs["Threshold"].default_value = 1.0
        glare.inputs["Strength"].default_value = bloom
        glare.inputs["Size"].default_value = 0.6
        nt.links.new(src, glare.inputs["Image"])
        src = glare.outputs["Image"]
    if vignette > 0:
        mask = nt.nodes.new("CompositorNodeEllipseMask")
        mask.inputs["Size"].default_value = (1.6, 1.6)
        blur = nt.nodes.new("CompositorNodeBlur")
        blur.inputs["Size"].default_value = (420, 420)
        nt.links.new(mask.outputs["Mask"], blur.inputs["Image"])
        mix = nt.nodes.new("ShaderNodeMix")
        mix.data_type = "RGBA"
        mix.blend_type = "MULTIPLY"
        mix.inputs[0].default_value = vignette          # Factor
        nt.links.new(src, mix.inputs[6])                # A: 画
        nt.links.new(blur.outputs["Image"], mix.inputs[7])  # B: マスク
        src = mix.outputs[2] if len(mix.outputs) > 2 else mix.outputs[0]
        # RGBA出力ソケットを名前で選ぶ(インデックスはバージョン依存)
        for o in mix.outputs:
            if o.type == "RGBA":
                src = o
                break
    nt.links.new(src, out.inputs["Image"])


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


def add_wall_image(path, location, width, opacity=0.85, desaturate=0.35):
    """画像を壁に「印刷/ペイントした風」に貼る。

    背景透過PNG推奨。マットな質感+彩度控えめ+半透明で壁の色が透け、
    貼り紙ではなく壁面に描かれたように見せる。
    """
    img = bpy.data.images.load(str(path))
    aspect = img.size[1] / img.size[0]
    bpy.ops.mesh.primitive_plane_add(size=1)
    ob = bpy.context.object
    ob.name = f"wallimg_{path.stem}"
    ob.scale = (width, width * aspect, 1)
    ob.rotation_euler = (math.radians(90), 0, 0)
    ob.location = location

    mat = bpy.data.materials.new(f"wallimg_{path.stem}")
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    hsv = nt.nodes.new("ShaderNodeHueSaturation")
    hsv.inputs["Saturation"].default_value = 1.0 - desaturate
    nt.links.new(tex.outputs["Color"], hsv.inputs["Color"])
    nt.links.new(hsv.outputs["Color"], bsdf.inputs["Base Color"])
    math_n = nt.nodes.new("ShaderNodeMath")
    math_n.operation = "MULTIPLY"
    math_n.inputs[1].default_value = opacity
    nt.links.new(tex.outputs["Alpha"], math_n.inputs[0])
    nt.links.new(math_n.outputs["Value"], bsdf.inputs["Alpha"])
    bsdf.inputs["Roughness"].default_value = 0.9
    if hasattr(mat, "blend_method"):
        mat.blend_method = "BLEND"
    ob.data.materials.append(mat)
    return ob


def add_framed_picture(path, location, width, title, font, plaque_mat,
                       frame_mat, text_mat):
    """額縁に入れて壁に飾った絵+下の銘板(美術館スタイル)。

    どんな画像でもそのまま使える(切り抜き・透過不要)。
    """
    img = bpy.data.images.load(str(path))
    aspect = img.size[1] / img.size[0]
    ih = width * aspect                  # 画像の高さ
    mat_m = width * 0.07                 # マット(台紙)の余白
    fw = width + mat_m * 2               # 額の内寸(=マット寸)
    fh = ih + mat_m * 2
    bar = 0.035                          # 額縁の枠の太さ
    depth = 0.03                         # 壁からの出っ張り
    x, y, z = location

    # マット(白い台紙)
    white = bpy.data.materials.new("mat_white")
    white.use_nodes = True
    wb = white.node_tree.nodes["Principled BSDF"]
    wb.inputs["Base Color"].default_value = (0.92, 0.91, 0.88, 1)
    wb.inputs["Roughness"].default_value = 0.85
    bpy.ops.mesh.primitive_plane_add(size=1)
    matp = bpy.context.object
    matp.scale = (fw, fh, 1)
    matp.rotation_euler = (math.radians(90), 0, 0)
    matp.location = (x, y - depth * 0.5, z)
    matp.data.materials.append(white)

    # 画像(マットの上に少し浮かせる)
    pic_mat = bpy.data.materials.new(f"framed_{path.stem}")
    pic_mat.use_nodes = True
    nt = pic_mat.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.65
    bpy.ops.mesh.primitive_plane_add(size=1)
    pic = bpy.context.object
    pic.scale = (width, ih, 1)
    pic.rotation_euler = (math.radians(90), 0, 0)
    pic.location = (x, y - depth * 0.5 - 0.003, z)
    pic.data.materials.append(pic_mat)

    # 額縁の枠(4辺の細い棒)
    for bx, bz, sx, sz in [(0, fh / 2 + bar / 2, fw + bar * 2, bar),
                           (0, -fh / 2 - bar / 2, fw + bar * 2, bar),
                           (-fw / 2 - bar / 2, 0, bar, fh),
                           (fw / 2 + bar / 2, 0, bar, fh)]:
        bpy.ops.mesh.primitive_cube_add(size=1)
        b = bpy.context.object
        b.scale = (sx, depth, sz)
        b.location = (x + bx, y - depth / 2, z + bz)
        b.data.materials.append(frame_mat)

    # 銘板(額の下の真鍮プレート+刻印タイトル)
    if title:
        ph = 0.16
        pw = max(0.62, 0.062 * max(len(l) for l in title.split("\n")))
        pz = z - fh / 2 - bar - 0.14
        bpy.ops.mesh.primitive_cube_add(size=1)
        pl = bpy.context.object
        pl.scale = (pw, 0.012, ph)
        pl.location = (x, y - 0.006, pz)
        bev = pl.modifiers.new("bevel", "BEVEL")
        bev.width = 0.006
        bev.segments = 2
        pl.data.materials.append(plaque_mat)
        add_wall_text(title, (x, y - 0.014, pz), 0.045, text_mat, font,
                      extrude=0.002)


def add_framed_picture(path, location, width, title, font, plaque_mat,
                       frame_mat, text_mat):
    """額縁に入れて壁に飾った絵+下の銘板(美術館スタイル)。

    どんな画像でもそのまま使える(切り抜き・透過不要)。
    """
    img = bpy.data.images.load(str(path))
    aspect = img.size[1] / img.size[0]
    ih = width * aspect
    mat_m = width * 0.07                 # マット(台紙)の余白
    fw = width + mat_m * 2
    fh = ih + mat_m * 2
    bar = 0.035
    depth = 0.03
    x, y, z = location

    white = bpy.data.materials.new("mat_white")
    white.use_nodes = True
    wb = white.node_tree.nodes["Principled BSDF"]
    wb.inputs["Base Color"].default_value = (0.92, 0.91, 0.88, 1)
    wb.inputs["Roughness"].default_value = 0.85
    bpy.ops.mesh.primitive_plane_add(size=1)
    matp = bpy.context.object
    matp.scale = (fw, fh, 1)
    matp.rotation_euler = (math.radians(90), 0, 0)
    matp.location = (x, y - depth * 0.5, z)
    matp.data.materials.append(white)

    pic_mat = bpy.data.materials.new(f"framed_{path.stem}")
    pic_mat.use_nodes = True
    nt = pic_mat.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.65
    bpy.ops.mesh.primitive_plane_add(size=1)
    pic = bpy.context.object
    pic.scale = (width, ih, 1)
    pic.rotation_euler = (math.radians(90), 0, 0)
    pic.location = (x, y - depth * 0.5 - 0.003, z)
    pic.data.materials.append(pic_mat)

    for bx, bz, sx, sz in [(0, fh / 2 + bar / 2, fw + bar * 2, bar),
                           (0, -fh / 2 - bar / 2, fw + bar * 2, bar),
                           (-fw / 2 - bar / 2, 0, bar, fh),
                           (fw / 2 + bar / 2, 0, bar, fh)]:
        bpy.ops.mesh.primitive_cube_add(size=1)
        b = bpy.context.object
        b.scale = (sx, depth, sz)
        b.location = (x + bx, y - depth / 2, z + bz)
        b.data.materials.append(frame_mat)

    if title:
        # 銘板は行数と最長行に合わせて動的にサイズを決める
        lines = title.split("\n")
        longest = max(len(l) for l in lines)
        ts = 0.075                                   # 基本の文字サイズ
        max_w = max(fw * 0.95, 0.7)                  # 額の幅に収める
        if ts * longest * 0.98 > max_w:
            ts = max(0.048, max_w / (longest * 0.98))
        line_h = ts * 1.45
        ph = line_h * len(lines) + 0.07
        pw = ts * longest * 0.98 + 0.14
        pz = z - fh / 2 - bar - 0.10 - ph / 2
        bpy.ops.mesh.primitive_cube_add(size=1)
        pl = bpy.context.object
        pl.scale = (pw, 0.012, ph)
        pl.location = (x, y - 0.006, pz)
        bev = pl.modifiers.new("bevel", "BEVEL")
        bev.width = 0.006
        bev.segments = 2
        pl.data.materials.append(plaque_mat)
        txt = add_wall_text(title, (x, y - 0.014, pz), ts, text_mat, font,
                            extrude=0.002)
        txt.data.space_line = 1.3


def add_wall_hatch(location, ball_r, wall_mat, fps):
    """何もない壁が左右にパカッと割れてボールが出てくるハッチ。

    パネルは壁と同じマテリアル(スクリーン座標グラデーション)なので
    閉じている間は壁に完全に溶け込み、動いた瞬間に割れ目が現れる。
    """
    x, y, z = location
    ow, oh = ball_r * 2.9, ball_r * 3.1     # 開口部のサイズ
    # 暗い奥(穴の見た目)
    dark = bpy.data.materials.new("hatch_dark")
    dark.use_nodes = True
    db = dark.node_tree.nodes["Principled BSDF"]
    db.inputs["Base Color"].default_value = (0.012, 0.012, 0.016, 1)
    db.inputs["Roughness"].default_value = 0.95
    bpy.ops.mesh.primitive_plane_add(size=1)
    hole = bpy.context.object
    hole.scale = (ow, oh, 1)
    hole.rotation_euler = (math.radians(90), 0, 0)
    hole.location = (x, y - 0.002, z)
    hole.data.materials.append(dark)
    # 左右のパネル(壁と同じマテリアル=閉じていれば見えない)
    pw = ow / 2 + 0.02
    f0 = int(0.12 * fps)
    f1 = int(0.50 * fps)
    for side in (-1, 1):
        bpy.ops.mesh.primitive_cube_add(size=1)
        pnl = bpy.context.object
        pnl.scale = (pw, 0.002, oh + 0.04)
        base_x = x + side * pw / 2
        pnl.location = (base_x, y - 0.003, z)    # 壁とほぼ面一(枠が見えないように)
        pnl.data.materials.append(wall_mat)
        pnl.keyframe_insert("location", frame=f0)
        # 引き戸のように横へ滑りつつ壁の奥へ沈んで消える
        pnl.location = (base_x + side * (pw * 0.95), y + 0.03, z)
        pnl.keyframe_insert("location", frame=f1)


def add_tunnel(a, b, u, ball_r):
    """レールの端を包む暗いトンネル。ボールが闇に溶けて消える(現れる)。

    a=奥(閉じた側), b=口(開いた側)。上下・背面・奥端は閉じ、
    口側と手前(カメラ側)の口元だけ開けて、奥は前面も覆う。
    """
    dark = bpy.data.materials.new("tunnel")
    dark.use_nodes = True
    db = dark.node_tree.nodes["Principled BSDF"]
    db.inputs["Base Color"].default_value = (0.008, 0.008, 0.012, 1)
    db.inputs["Roughness"].default_value = 0.95
    ax, ay = a[0], a[1]
    bx, by = b[0], b[1]
    cx, cy = (ax + bx) / 2, (ay + by) / 2
    L = math.hypot(bx - ax, by - ay)
    ang = math.atan2(-(by - ay) / L if L else 0, (bx - ax) / L if L else 1)
    hh = ball_r + 0.10          # 半分の高さ
    hd = ball_r + 0.08          # 奥行き(Blender Y)の半分
    th = 0.012                  # 板厚

    def slab(local_x, local_z, sx, sz, y_off, sy=hd * 2):
        bpy.ops.mesh.primitive_cube_add(size=1)
        ob = bpy.context.object
        ob.scale = (sx, sy, sz)
        ob.rotation_euler = (0, ang, 0)
        ca, sa = math.cos(ang), math.sin(ang)
        wx = cx + local_x * ca + local_z * sa
        wz = (cy) + (-local_x * sa + local_z * ca)
        ob.location = (wx, y_off, wz)
        ob.data.materials.append(dark)

    slab(0, hh, L + 0.1, th, 0.02)              # 天井
    slab(0, -hh, L + 0.1, th, 0.02)             # 床
    slab(-L / 2 - 0.05, 0, th, hh * 2, 0.02)    # 奥端の蓋
    # 背面(壁側)
    bpy.ops.mesh.primitive_cube_add(size=1)
    ob = bpy.context.object
    ob.scale = (L + 0.1, th, hh * 2)
    ob.rotation_euler = (0, ang, 0)
    ob.location = (cx, 0.02 + hd, cy)
    ob.data.materials.append(dark)
    # 手前(カメラ側)は奥60%だけ覆う: 口元は見える
    bpy.ops.mesh.primitive_cube_add(size=1)
    ob = bpy.context.object
    ob.scale = (L * 0.6, th, hh * 2)
    ob.rotation_euler = (0, ang, 0)
    ca, sa = math.cos(ang), math.sin(ang)
    off = -L * 0.2
    ob.location = (cx + off * ca, 0.02 - hd, cy - off * sa)
    ob.data.materials.append(dark)


def keyframe_visibility(ob, f_in, f_out, fps, n_frames=None):
    """スケールで出現/消滅を表現する(0.25秒でポップイン/アウト)。

    f_inが負(動画開始前から存在)なら最初から表示。
    n_framesを渡すと、消滅は動画が終わる前に完了するよう前倒しされる
    (ループの最終フレームに残骸が残らないように)。
    """
    pop = max(2, int(0.25 * fps))
    if n_frames is not None and f_out + pop > n_frames - int(0.2 * fps):
        f_out = n_frames - int(0.2 * fps) - pop
    if f_in + pop <= 0:
        ob.scale = (1, 1, 1)
        ob.keyframe_insert("scale", frame=0)
    else:
        ob.scale = (0, 0, 0)
        ob.keyframe_insert("scale", frame=max(0, f_in - 1))
        ob.scale = (1, 1, 1)
        ob.keyframe_insert("scale", frame=max(1, f_in + pop))
    if f_out > 0:
        ob.scale = (1, 1, 1)
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
    key.data.energy = 360
    key.data.size = 1.2
    key.rotation_euler = (math.radians(55), 0, math.radians(-30))
    bpy.ops.object.light_add(type="AREA", location=(2.5, -3.5, -1.0))
    fill = bpy.context.object
    fill.data.energy = 70
    fill.data.size = 4.0
    fill.rotation_euler = (math.radians(95), 0, math.radians(35))

    # ---- 壁のコンテンツ(wall.jsonで自由指定、無ければタイトルのみ) ----
    font = None
    try:
        font = bpy.data.fonts.load(JP_FONT)
    except Exception:
        pass
    text_mat = make_material("walltext", (0.035, 0.035, 0.045, 1),
                             metallic=0.85, rough=0.35)   # メタリックブラック

    def resolve_at(at, dy=0.0):
        """配置指定: "start"=壁の穴の位置 / "first_plate"=最初の板 / [x, y]"""
        if at == "start":
            b0 = sc.get("start_anchor", sc["ball"][0])
            return (b0[0], WALL_Y - 0.012, b0[1] + dy)
        if at == "end":
            b0 = sc.get("end_anchor", sc["ball"][-1])
            return (b0[0], WALL_Y - 0.012, b0[1] + dy)
        if at == "first_plate":
            p0 = sc["plates"][0]["pos"]
            return (p0[0], WALL_Y - 0.012, p0[1] + 0.72 + dy)
        return (at[0], WALL_Y - 0.012, at[1] + dy)

    wall_cfg = sc.get("wall")
    if not wall_cfg:
        title = sc.get("title")
        wall_cfg = {"texts": ([{"text": title, "at": "start", "dy": -0.55},
                               {"text": title, "at": "first_plate"}]
                              if title else [])}
    for tx in wall_cfg.get("texts", []):
        add_wall_text(tx["text"], resolve_at(tx["at"], tx.get("dy", 0.0)),
                      tx.get("size", 0.30), text_mat, font)
    wall_dir = Path(wall_cfg.get("_dir", "."))

    def wall_file(f):
        q = Path(f)
        return q if q.is_absolute() or q.exists() else wall_dir / q

    for im in wall_cfg.get("images", []):
        add_wall_image(wall_file(im["file"]),
                       resolve_at(im["at"], im.get("dy", 0.0)),
                       im.get("width", 0.8), im.get("opacity", 0.85),
                       im.get("desaturate", 0.35))
    # 入口・出口の暗いトンネル(ループの継ぎ目を隠す)
    for tn in sc.get("tunnels", []):
        add_tunnel(tn["a"], tn["b"], tn["u"], sc["ball_r"])
    if wall_cfg.get("frames"):
        plaque_mat = make_material("plaque", (0.55, 0.45, 0.25, 1),
                                   metallic=1.0, rough=0.35)
        frame_mat = make_material("frame", (0.06, 0.06, 0.07, 1),
                                  metallic=0.85, rough=0.35)
        plate_text_mat = make_material("plaquetext", (0.16, 0.13, 0.08, 1),
                                       rough=0.6)
        spot_energy = float(wall_cfg.get("frame_spot_energy", 120))
        for fr in wall_cfg["frames"]:
            pos = resolve_at(fr["at"], fr.get("dy", 0.0))
            add_framed_picture(wall_file(fr["file"]), pos,
                               fr.get("width", 0.9), fr.get("title", ""),
                               font, plaque_mat, frame_mat, plate_text_mat)
            if spot_energy > 0:
                add_picture_spot(pos, energy=spot_energy)
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

    # ---- 経路沿いの物理ライト(降下に沿って点在、色はプロジェクト設定) ----
    light_cols = [hex_rgb(c) for c in wall_cfg.get("lights", [])] or \
                 [(1.0, 0.85, 0.65), (0.65, 0.8, 1.0)]
    light_energy = float(wall_cfg.get("lights_energy", 55))
    ys = [b[1] for b in sc["ball"]]
    y_top, y_bot = max(ys) + 1.0, min(ys) - 1.0
    dark_ys = [sc.get("start_pos", [0, 9e9])[1], sc.get("end_pos", [0, -9e9])[1]]
    i_l = 0
    y = y_top
    while y > y_bot:
        if any(abs(y - dy_) < 2.6 for dy_ in dark_ys):
            y -= 3.4
            continue                     # 入口・出口の暗闇ゾーンには置かない
        side = 1 if i_l % 2 == 0 else -1
        # 経路から少し離して置く(近すぎるとパーツが白飛びする)
        bpy.ops.object.light_add(type="POINT",
                                 location=(side * 2.2, -0.85, y))
        lp = bpy.context.object
        lp.data.energy = light_energy
        lp.data.shadow_soft_size = 0.3
        lp.data.color = light_cols[i_l % len(light_cols)]
        i_l += 1
        y -= 3.4

    # ---- ビー玉(渦の色はプロジェクト設定、未指定ならライト色から導出) ----
    mc = wall_cfg.get("marble_colors")
    if mc:
        band = [(*hex_rgb(c), 1.0) for c in (mc * 4)[:4]]
    elif wall_cfg.get("lights"):
        # ライト色から彩度を強めた濃い渦色を導出(淡色ライトでも映える)
        base = [hex_rgb(c) for c in wall_cfg["lights"]]
        band = []
        for i in range(4):
            h, sv, v = colorsys.rgb_to_hsv(*base[i % len(base)])
            sv = min(1.0, sv * 2.6 + 0.15)
            v = 0.78 if i < 2 else 0.5          # 2本目の帯は暗めでコントラスト
            band.append((*colorsys.hsv_to_rgb(h, sv, v), 1.0))
    else:
        band = None
    ball = make_marble(sc["ball_r"], band,
                       glow=float(wall_cfg.get("marble_glow", 1.1)))
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
        keyframe_visibility(ob, f_in, f_out, fps, n_frames)
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
        # 入口・出口レールは常時表示(消えない)
        if f_in + int(0.25 * fps) <= 0 or f_out >= n_frames:
            f_out = -1
            keyframe_visibility(ob, f_in, f_out, fps)
        else:
            keyframe_visibility(ob, f_in, f_out, fps, n_frames)

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

    # ---- 仕上げ(コンポジタ): ブルーム+ビネット ----
    setup_stylize(scene,
                  bloom=float(wall_cfg.get("bloom", 0.35)),
                  vignette=float(wall_cfg.get("vignette", 0.30)))

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
    ap.add_argument("--wall", type=Path, default=None,
                    help="壁コンテンツwall.jsonのパス(省略時: scene.jsonと同じ場所)")
    args = ap.parse_args()

    sc = json.loads(args.scene.read_text())
    wall_path = args.wall or (args.scene.parent / "wall.json")
    if wall_path.exists():
        sc["wall"] = json.loads(wall_path.read_text())
        sc["wall"]["_dir"] = str(wall_path.parent)
        print(f"壁コンテンツ: {wall_path}")
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
