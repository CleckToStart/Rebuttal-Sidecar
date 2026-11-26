"""Generate multi-resolution Rebuttal Sidecar icons (assets/icon.png & assets/icon.ico)."""

from __future__ import annotations

import os
import struct
from pathlib import Path

try:
    import clr
except Exception:
    os.environ["PYTHONNET_RUNTIME"] = "coreclr"
    import clr

clr.AddReference("System.Drawing")

from System.Drawing import (  # noqa: E402
    Bitmap,
    Color,
    Drawing2D,
    Graphics,
    Imaging,
    Pen,
    PointF,
    RectangleF,
    SolidBrush,
)
from System.IO import MemoryStream  # noqa: E402


def _rounded_rect(rect: RectangleF, radius: float) -> Drawing2D.GraphicsPath:
    path = Drawing2D.GraphicsPath()
    d = radius * 2.0
    path.AddArc(rect.X, rect.Y, d, d, 180, 90)
    path.AddArc(rect.Right - d, rect.Y, d, d, 270, 90)
    path.AddArc(rect.Right - d, rect.Bottom - d, d, d, 0, 90)
    path.AddArc(rect.X, rect.Bottom - d, d, d, 90, 90)
    path.CloseFigure()
    return path


def _draw_master_icon(size: int = 512) -> Bitmap:
    bmp = Bitmap(size, size, Imaging.PixelFormat.Format32bppArgb)
    g = Graphics.FromImage(bmp)
    g.SmoothingMode = Drawing2D.SmoothingMode.AntiAlias
    g.PixelOffsetMode = Drawing2D.PixelOffsetMode.HighQuality
    g.Clear(Color.Transparent)

    s = size / 256.0

    # Deep indigo squircle background
    bg_rect = RectangleF(12 * s, 12 * s, 232 * s, 232 * s)
    bg_path = _rounded_rect(bg_rect, 54 * s)
    bg_brush = Drawing2D.LinearGradientBrush(
        PointF(12 * s, 12 * s),
        PointF(244 * s, 244 * s),
        Color.FromArgb(255, 43, 38, 84),
        Color.FromArgb(255, 23, 20, 46),
    )
    g.FillPath(bg_brush, bg_path)

    # White manuscript sheet with folded corner
    sheet_rect = RectangleF(56 * s, 50 * s, 130 * s, 156 * s)
    sheet_path = _rounded_rect(sheet_rect, 14 * s)
    g.FillPath(SolidBrush(Color.White), sheet_path)

    # Fold corner overlay on top-right of sheet
    fold_cut = Drawing2D.GraphicsPath()
    fold_cut.AddPolygon([PointF(148 * s, 49 * s), PointF(187 * s, 49 * s), PointF(187 * s, 88 * s)])
    g.FillPath(bg_brush, fold_cut)

    fold_flap = Drawing2D.GraphicsPath()
    fold_flap.AddPolygon([PointF(148 * s, 50 * s), PointF(148 * s, 88 * s), PointF(186 * s, 88 * s)])
    g.FillPath(SolidBrush(Color.FromArgb(255, 203, 213, 225)), fold_flap)

    # Neutral header line
    g.FillPath(SolidBrush(Color.FromArgb(255, 148, 163, 184)), _rounded_rect(RectangleF(78 * s, 92 * s, 54 * s, 10 * s), 5 * s))

    # Coral deletion bar + strike line
    g.FillPath(SolidBrush(Color.FromArgb(255, 252, 165, 165)), _rounded_rect(RectangleF(78 * s, 116 * s, 84 * s, 10 * s), 5 * s))
    g.FillPath(SolidBrush(Color.FromArgb(255, 220, 38, 38)), _rounded_rect(RectangleF(74 * s, 120 * s, 92 * s, 2.8 * s), 1.4 * s))

    # Mint addition highlight bar
    mint_brush = Drawing2D.LinearGradientBrush(
        PointF(78 * s, 140 * s),
        PointF(190 * s, 210 * s),
        Color.FromArgb(255, 99, 220, 201),
        Color.FromArgb(255, 20, 184, 166),
    )
    g.FillPath(mint_brush, _rounded_rect(RectangleF(78 * s, 140 * s, 76 * s, 11 * s), 5.5 * s))

    # Sidecar verification badge at bottom-right
    g.FillEllipse(SolidBrush(Color.FromArgb(255, 23, 20, 46)), RectangleF(134 * s, 134 * s, 88 * s, 88 * s))
    g.FillEllipse(mint_brush, RectangleF(140 * s, 140 * s, 76 * s, 76 * s))

    check_pen = Pen(Color.White, 9.5 * s)
    check_pen.StartCap = Drawing2D.LineCap.Round
    check_pen.EndCap = Drawing2D.LineCap.Round
    check_pen.LineJoin = Drawing2D.LineJoin.Round
    g.DrawLines(
        check_pen,
        [PointF(161 * s, 178 * s), PointF(173 * s, 190 * s), PointF(197 * s, 164 * s)],
    )

    g.Dispose()
    return bmp


def _resize_to_png_bytes(master: Bitmap, size: int) -> bytes:
    target = Bitmap(size, size, Imaging.PixelFormat.Format32bppArgb)
    g = Graphics.FromImage(target)
    g.InterpolationMode = Drawing2D.InterpolationMode.HighQualityBicubic
    g.SmoothingMode = Drawing2D.SmoothingMode.AntiAlias
    g.PixelOffsetMode = Drawing2D.PixelOffsetMode.HighQuality
    g.DrawImage(master, 0, 0, size, size)
    g.Dispose()

    stream = MemoryStream()
    target.Save(stream, Imaging.ImageFormat.Png)
    data = bytes(stream.ToArray())
    stream.Dispose()
    target.Dispose()
    return data


def _pack_ico(png_frames: list[tuple[int, bytes]]) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(png_frames))
    offset = 6 + 16 * len(png_frames)
    entries = []
    payloads = []
    for size, data in png_frames:
        dim = 0 if size >= 256 else size
        entries.append(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset))
        payloads.append(data)
        offset += len(data)
    return header + b"".join(entries) + b"".join(payloads)


def generate_icons(repo_root: Path) -> tuple[Path, Path]:
    assets_dir = repo_root / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    master = _draw_master_icon(512)
    try:
        sizes = [256, 128, 64, 48, 32, 24, 16]
        frames = [(sz, _resize_to_png_bytes(master, sz)) for sz in sizes]
    finally:
        master.Dispose()

    png_path = assets_dir / "icon.png"
    ico_path = assets_dir / "icon.ico"
    png_path.write_bytes(frames[0][1])
    ico_path.write_bytes(_pack_ico(frames))
    return png_path, ico_path


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    png_out, ico_out = generate_icons(root)
    print(f"Generated {png_out} ({png_out.stat().st_size} bytes) and {ico_out} ({ico_out.stat().st_size} bytes)")
