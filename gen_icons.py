#!/usr/bin/env python3
"""
Regenerate all menu bar / app icons (PNGs) from SVG via rsvg-convert.

Requires: brew install librsvg   (provides rsvg-convert)
Usage:    python3 gen_icons.py

Produces in icons/:
  running.png / paused.png / off.png  — static states
  AppIcon.icns                        — Finder/Dock app icon
  roll_c{1..9}_NN.png, roll_c9p_NN.png — rolling robot animation (count baked in)
"""
import os
import math
import subprocess

ICONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
os.makedirs(ICONS, exist_ok=True)
RSVG = os.getenv("RSVG", "/opt/homebrew/bin/rsvg-convert")

# rounded-square robot head (r=8) + capsule eyes (raised), as a compound evenodd path
HEAD = "M14,6 h4 a8,8 0 0 1 8,8 v4 a8,8 0 0 1 -8,8 h-4 a8,8 0 0 1 -8,-8 v-4 a8,8 0 0 1 8,-8 z"
EYE1 = "M11.2,11.6 L11.2,15.4 a1.3,1.3 0 0 0 2.6,0 L13.8,11.6 a1.3,1.3 0 0 0 -2.6,0 z"
EYE2 = "M18.2,11.6 L18.2,15.4 a1.3,1.3 0 0 0 2.6,0 L20.8,11.6 a1.3,1.3 0 0 0 -2.6,0 z"
FACE = f"{HEAD} {EYE1} {EYE2}"


def render(svg, out, height=44):
    p = os.path.join(ICONS, out)
    subprocess.run([RSVG, "-h", str(height), "/dev/stdin", "-o", p],
                   input=svg.encode(), check=True)


def svg_wrap(viewbox, w, h, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox}" '
            f'width="{w}" height="{h}">{body}</svg>')


# ── static state icons ──────────────────────────────────────────────
def static_icons():
    head = ('<defs><mask id="f"><rect x="0" y="0" width="24" height="24" fill="white"/>'
            '<rect x="7.1" y="8.5" width="2.8" height="7.0" rx="1.4" fill="black"/>'
            '<rect x="14.1" y="8.5" width="2.8" height="7.0" rx="1.4" fill="black"/></mask></defs>'
            '<rect x="0.7" y="0.7" width="22.6" height="22.6" rx="8" fill="#000000" mask="url(#f)" {extra}/>')
    render(svg_wrap("0 0 24 24", 24, 24, head.format(extra='')), "running.png", 44)
    render(svg_wrap("0 0 24 24", 24, 24, head.format(extra='opacity="0.32"')), "off.png", 44)
    # paused = bot + crescent moon badge bottom-right
    paused = ('<defs>'
              '<mask id="f"><rect x="0" y="0" width="24" height="24" fill="white"/>'
              '<rect x="7.1" y="8.5" width="2.8" height="7.0" rx="1.4" fill="black"/>'
              '<rect x="14.1" y="8.5" width="2.8" height="7.0" rx="1.4" fill="black"/>'
              '<circle cx="17" cy="17" r="7.1" fill="black"/></mask>'
              '<mask id="moon"><rect x="0" y="0" width="24" height="24" fill="black"/>'
              '<circle cx="17" cy="17" r="6.0" fill="white"/>'
              '<circle cx="19.7" cy="14.3" r="5.6" fill="black"/></mask></defs>'
              '<rect x="0.7" y="0.7" width="22.6" height="22.6" rx="8" fill="#000000" mask="url(#f)"/>'
              '<rect x="10" y="10" width="14" height="14" fill="#000000" mask="url(#moon)"/>')
    render(svg_wrap("0 0 24 24", 24, 24, paused), "paused.png", 44)


# ── app icon (.icns) ────────────────────────────────────────────────
def app_icon():
    body = ('<defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0" stop-color="#7B78FF"/><stop offset="1" stop-color="#4A3CCB"/>'
            '</linearGradient></defs>'
            '<rect x="0" y="0" width="24" height="24" rx="5.4" fill="url(#bg)"/>'
            '<rect x="4.6" y="4.6" width="14.8" height="14.8" rx="5" fill="#ffffff"/>'
            '<rect x="9.0" y="9.3" width="2.2" height="5.4" rx="1.1" fill="#4A3CCB"/>'
            '<rect x="12.8" y="9.3" width="2.2" height="5.4" rx="1.1" fill="#4A3CCB"/>')
    iconset = os.path.join(ICONS, "AppIcon.iconset")
    os.makedirs(iconset, exist_ok=True)
    sizes = [(16, "16x16"), (32, "16x16@2x"), (32, "32x32"), (64, "32x32@2x"),
             (128, "128x128"), (256, "128x128@2x"), (256, "256x256"),
             (512, "256x256@2x"), (512, "512x512"), (1024, "512x512@2x")]
    svg = svg_wrap("0 0 24 24", 1024, 1024, body)
    for px, name in sizes:
        subprocess.run([RSVG, "-w", str(px), "-h", str(px), "/dev/stdin",
                        "-o", os.path.join(iconset, f"icon_{name}.png")],
                       input=svg.encode(), check=True)
    subprocess.run(["iconutil", "-c", "icns", iconset,
                    "-o", os.path.join(ICONS, "AppIcon.icns")], check=True)


# ── rolling animation (count baked in) ──────────────────────────────
N = 24
ANGLES = [-15 * i for i in range(N)]   # counter-clockwise, 15° steps
VB = "5 5.1 46 21.8"; VBW, VBH = 46, 21.8
NUM_X, NUM_BASE = 37, 24.5
L = 12


def _pos(kind, s):
    if s < 0 or s >= L:
        return None
    rf = max(0.0, 1.0 - s / L)
    if kind == "high":   return 23 + 2.0 * s, 23 - 3.6 * s + 0.30 * s * s, rf
    if kind == "high2":  return 23 + 2.6 * s, 22 - 4.2 * s + 0.34 * s * s, rf
    if kind == "low":    return 23 + 3.3 * s, 24 - 1.0 * s + 0.14 * s * s, rf
    if kind == "pop":    return 22 + 0.5 * s, 23 - 3.2 * s + 0.44 * s * s, rf
    if kind == "upleft": return 21 - 0.9 * s, 22 - 3.0 * s + 0.40 * s * s, rf
    if kind == "bounce":
        vx, vy, g = 2.2, 0.8, 0.28; ximp = 32.0; simp = (ximp - 23) / vx
        if s <= simp:
            return 23 + vx * s, 22 - vy * s + g * s * s, rf
        t = s - simp; yimp = 22 - vy * simp + g * simp * simp
        return ximp - 1.5 * t, yimp - 2.6 * t + g * t * t, rf
    return None


PARTS = [("high", 0, 1.05), ("low", 2, 0.9), ("bounce", 4, 1.0), ("pop", 6, 0.8),
         ("high2", 8, 0.85), ("upleft", 10, 0.8), ("bounce", 1, 0.7)]


def _dust(i):
    out = []
    for kind, off, r0 in PARTS:
        p = _pos(kind, (i + off) % L)
        if p:
            x, y, rf = p; r = r0 * rf
            if r > 0.28 and 4 < x < VBW + 6 and 3 < y < 28:
                out.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{r:.2f}" fill="#000000"/>')
    return "".join(out)


def roll_set(label, numtext, numx, numsize):
    for i, a in enumerate(ANGLES):
        num = (f'<text x="{numx}" y="{NUM_BASE}" font-family="Helvetica" font-weight="700" '
               f'font-size="{numsize}" text-anchor="middle" fill="#000000">{numtext}</text>')
        body = (f'<path fill-rule="evenodd" fill="#000000" transform="rotate({a} 16 16)" d="{FACE}"/>'
                f'{_dust(i)}{num}')
        render(svg_wrap(VB, VBW, VBH, body), f"roll_{label}_{i:02d}.png", 44)


if __name__ == "__main__":
    static_icons()
    app_icon()
    for c in range(1, 10):
        roll_set(f"c{c}", str(c), NUM_X, 15)
    roll_set("c9p", "9+", NUM_X - 1, 12)
    print("icons regenerated")
