"""앱 아이콘(.icns)을 만든다. 디자인을 바꾸고 싶을 때만 다시 돌리면 된다.

    ./venv/bin/python tools/아이콘만들기.py
"""
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024
ROOT = Path(__file__).resolve().parent.parent


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1], radius, fill=255)
    return m


def make_png():
    # 배경: 위에서 아래로 가는 파란 그라데이션 (화면 버튼 색 #4a6cf7 계열)
    bg = Image.new("RGB", (SIZE, SIZE))
    d = ImageDraw.Draw(bg)
    top, bottom = (0x5B, 0x7C, 0xFA), (0x33, 0x46, 0xC8)
    for y in range(SIZE):
        t = y / SIZE
        d.line([(0, y), (SIZE, y)],
               fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))

    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    img.paste(bg, (0, 0), rounded_mask(SIZE, int(SIZE * 0.225)))
    d = ImageDraw.Draw(img)

    # 필름 구멍 (영상이라는 걸 알려주는 힌트) — 양쪽 가장자리에 세로로
    hole_w, hole_h = int(SIZE * 0.052), int(SIZE * 0.072)
    for i in range(5):
        y = int(SIZE * 0.16) + i * int(SIZE * 0.172)
        for x in (int(SIZE * 0.055), SIZE - int(SIZE * 0.055) - hole_w):
            d.rounded_rectangle([x, y, x + hole_w, y + hole_h],
                                int(hole_w * 0.3), fill=(255, 255, 255, 60))

    # 아래로 향하는 굵은 화살표 = 다운로드
    cx = SIZE // 2
    shaft_w = int(SIZE * 0.13)
    d.rounded_rectangle([cx - shaft_w // 2, int(SIZE * 0.235),
                         cx + shaft_w // 2, int(SIZE * 0.60)],
                        shaft_w // 2, fill="white")
    d.polygon([(cx - int(SIZE * 0.175), int(SIZE * 0.545)),
               (cx + int(SIZE * 0.175), int(SIZE * 0.545)),
               (cx, int(SIZE * 0.745))], fill="white")
    # 받침대
    bar_w, bar_h = int(SIZE * 0.40), int(SIZE * 0.062)
    d.rounded_rectangle([cx - bar_w // 2, int(SIZE * 0.795),
                         cx + bar_w // 2, int(SIZE * 0.795) + bar_h],
                        bar_h // 2, fill="white")
    return img


def main():
    png = ROOT / "tools" / "icon.png"
    make_png().save(png)

    iconset = ROOT / "tools" / "icon.iconset"
    subprocess.run(["rm", "-rf", str(iconset)], check=True)
    iconset.mkdir()
    for s in (16, 32, 128, 256, 512):
        for scale, suffix in ((1, ""), (2, "@2x")):
            out = iconset / f"icon_{s}x{s}{suffix}.png"
            subprocess.run(["sips", "-z", str(s * scale), str(s * scale),
                            str(png), "--out", str(out)],
                           capture_output=True, check=True)
    icns = ROOT / "tools" / "앱아이콘.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True)
    subprocess.run(["rm", "-rf", str(iconset)], check=True)
    print("만들었어요:", icns)


if __name__ == "__main__":
    main()
