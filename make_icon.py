#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 SVN Manager 应用图标"""
from PIL import Image, ImageDraw

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

# 蓝色渐变底
grad = Image.new("RGB", (S, S))
gd = ImageDraw.Draw(grad)
top, bot = (58, 155, 255), (0, 84, 214)
for y in range(S):
    t = y / (S - 1)
    gd.line([(0, y), (S, y)], fill=(
        int(top[0] + (bot[0] - top[0]) * t),
        int(top[1] + (bot[1] - top[1]) * t),
        int(top[2] + (bot[2] - top[2]) * t),
    ))

mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([100, 100, 924, 924], radius=190, fill=255)
img.paste(grad, (0, 0), mask)

d = ImageDraw.Draw(img)
W = 40  # 线宽
nodes = []

# 主干
d.line([(380, 310), (380, 714)], fill="white", width=W, joint="curve")
nodes += [(380, 310), (380, 512), (380, 714)]
# 分支
d.line([(380, 410), (640, 360)], fill="white", width=W)
d.line([(380, 614), (640, 664)], fill="white", width=W)
nodes += [(640, 360), (640, 664)]

for x, y in nodes:
    r = 52
    d.ellipse([x - r, y - r, x + r, y + r], fill="white")

img.save("/tmp/svnicon_1024.png")
print("ok")
