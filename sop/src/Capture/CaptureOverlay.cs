using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Drawing.Text;
using System.IO;
using System.Linq;
using System.Windows.Forms;
using MySOP.Core;

namespace MySOP.Capture
{
    public enum CaptureMode { Normal, Ocr }

    enum Tool { None, Rect, Ellipse, Arrow, Pen, Mosaic, Text }

    class Shape
    {
        public Tool Kind;
        public Color Color;
        public float Width;
        public List<Point> Pts = new List<Point>();
        public string Text;
        public Font Font;
    }

    class ToolButton
    {
        public Rectangle Bounds;
        public string Glyph, GlyphFont, Tip;
        public Tool Tool;
        public Action Click;
        public bool Separator;
        public Color? Swatch;
        public int WidthLevel = -1;
    }

    /// <summary>
    /// 全屏截图遮罩（仿 Snipaste）：
    /// 鼠标悬停自动识别窗口、单击选中窗口、拖拽框选、8 个手柄调整、放大镜取色、
    /// 标注（矩形/椭圆/箭头/画笔/马赛克/文字）、复制/保存/钉图/OCR。
    /// </summary>
    public class CaptureOverlay : Form
    {
        // 快捷键说明（也显示在工具栏提示里）
        //   Enter / Ctrl+C / 双击 = 复制    Ctrl+S = 保存    Ctrl+T / F3 = 钉在屏幕
        //   Ctrl+Z = 撤销    方向键 = 微调选区    Ctrl+A = 选中当前显示器    C = 复制光标处颜色
        //   右键 = 重选 / 退出    Esc = 退出

        readonly CaptureMode mode;
        readonly Bitmap screen, dim;
        Bitmap mosaicSmall;
        readonly Rectangle vbounds;           // 虚拟屏幕（屏幕坐标）
        readonly List<Rectangle> winRects;     // 客户区坐标
        readonly List<Rectangle> monitors;     // 客户区坐标
        readonly float scale;
        readonly Font uiFont, smallFont, glyphFont, glyphFontText;

        Rectangle sel;
        bool hasSel;
        Rectangle? hover;
        enum DragKind { None, Creating, Moving, Resizing, Drawing }
        DragKind drag;
        int resizeHandle = -1;
        Point dragStart, mouse;
        Rectangle selAtDragStart;
        bool movedSinceDown;

        Tool tool = Tool.None;
        static Color lastColor = Color.FromArgb(255, 59, 48);
        static int lastWidthLevel = 1;
        Color color = lastColor;
        int widthLevel = lastWidthLevel;
        readonly List<Shape> shapes = new List<Shape>();
        Shape current;
        TextBox textBox;
        Point textPos;

        readonly List<ToolButton> buttons = new List<ToolButton>();
        Rectangle toolbarRect, subbarRect;
        ToolButton hoverButton;
        Rectangle lastDirty;

        static readonly Color[] Palette =
        {
            Color.FromArgb(255, 59, 48), Color.FromArgb(255, 204, 0), Color.FromArgb(52, 199, 89),
            Color.FromArgb(0, 122, 255), Color.FromArgb(175, 82, 222), Color.Black, Color.White
        };

        public CaptureOverlay(CaptureMode mode)
        {
            this.mode = mode;
            winRects = Host.Config.Section("capture").Bool("windowDetect", true) ? ScreenGrab.WindowRects() : new List<Rectangle>();
            screen = ScreenGrab.CaptureVirtualScreen(out vbounds);
            for (int i = 0; i < winRects.Count; i++) winRects[i] = ToClient(winRects[i]);
            monitors = Screen.AllScreens.Select(s => ToClient(s.Bounds)).ToList();
            if (!Host.Config.Section("capture").Bool("windowDetect", true)) winRects.AddRange(monitors);

            dim = new Bitmap(screen.Width, screen.Height, PixelFormat.Format32bppPArgb);
            using (var g = Graphics.FromImage(dim))
            {
                g.DrawImageUnscaled(screen, 0, 0);
                using (var b = new SolidBrush(Color.FromArgb(110, 0, 0, 0))) g.FillRectangle(b, 0, 0, dim.Width, dim.Height);
            }

            scale = Native.ScaleAt(Cursor.Position);
            uiFont = new Font(Theme.FontName, 12 * scale, GraphicsUnit.Pixel);
            smallFont = new Font(Theme.FontName, 11 * scale, GraphicsUnit.Pixel);
            glyphFont = new Font("Segoe MDL2 Assets", 15 * scale, GraphicsUnit.Pixel);
            glyphFontText = new Font(Theme.FontName, 15 * scale, FontStyle.Bold, GraphicsUnit.Pixel);

            FormBorderStyle = FormBorderStyle.None;
            StartPosition = FormStartPosition.Manual;
            ShowInTaskbar = false;
            TopMost = true;
            KeyPreview = true;
            DoubleBuffered = true;
            Cursor = Cursors.Cross;
            Bounds = vbounds;
            mouse = PointToClientSafe(Cursor.Position);
            UpdateHover();
        }

        // ---------- 坐标 ----------
        Rectangle ToClient(Rectangle r) { r.Offset(-vbounds.X, -vbounds.Y); return r; }
        Point PointToClientSafe(Point p) { return new Point(p.X - vbounds.X, p.Y - vbounds.Y); }
        Rectangle ToScreenRect(Rectangle r) { r.Offset(vbounds.X, vbounds.Y); return r; }

        protected override void OnShown(EventArgs e)
        {
            base.OnShown(e);
            Native.SetWindowPos(Handle, Native.HWND_TOPMOST, vbounds.X, vbounds.Y, vbounds.Width, vbounds.Height, Native.SWP_SHOWWINDOW);
            Activate();
            Native.SetForegroundWindow(Handle);
        }

        protected override void WndProc(ref Message m)
        {
            if (m.Msg == 0x02E0) return; // WM_DPICHANGED：不要让系统缩放这个跨屏窗口
            base.WndProc(ref m);
        }

        // ---------- 绘制 ----------
        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            var clip = e.ClipRectangle;
            g.CompositingMode = CompositingMode.SourceCopy;
            g.InterpolationMode = InterpolationMode.NearestNeighbor;
            g.DrawImage(dim, clip, clip, GraphicsUnit.Pixel);

            Rectangle bright = hasSel || drag == DragKind.Creating ? sel : (hover.HasValue ? hover.Value : Rectangle.Empty);
            var bc = Rectangle.Intersect(bright, clip);
            if (bc.Width > 0 && bc.Height > 0) g.DrawImage(screen, bc, bc, GraphicsUnit.Pixel);
            g.CompositingMode = CompositingMode.SourceOver;
            g.SmoothingMode = SmoothingMode.AntiAlias;

            if (bright.Width > 0)
            {
                // 标注
                var state = g.Save();
                g.SetClip(bright);
                foreach (var s in shapes) DrawShape(g, s);
                if (current != null) DrawShape(g, current);
                g.Restore(state);

                using (var pen = new Pen(Theme.Accent, Math.Max(1, (int)(1.5f * scale))))
                    g.DrawRectangle(pen, bright.X, bright.Y, bright.Width - 1, bright.Height - 1);
                if (hasSel && mode == CaptureMode.Normal)
                    foreach (var h in Handles()) { g.FillRectangle(Brushes.White, h); using (var p = new Pen(Theme.Accent)) g.DrawRectangle(p, h); }
                DrawSizeLabel(g, bright);
            }

            if (hasSel && mode == CaptureMode.Normal && drag != DragKind.Creating && drag != DragKind.Resizing && drag != DragKind.Moving) DrawToolbar(g);
            if (ShowMagnifier()) DrawMagnifier(g);
            if (mode == CaptureMode.Ocr) DrawHint(g, "框选要识别文字的区域（单击可选中窗口）· Esc 取消");
        }

        bool ShowMagnifier()
        {
            return Host.Config.Section("capture").Bool("magnifier", true) &&
                   (!hasSel || drag == DragKind.Creating || drag == DragKind.Resizing);
        }

        void DrawHint(Graphics g, string text)
        {
            var mon = monitors.FirstOrDefault(m => m.Contains(mouse));
            if (mon.IsEmpty) mon = monitors[0];
            var sz = g.MeasureString(text, uiFont);
            var r = new RectangleF(mon.X + (mon.Width - sz.Width) / 2 - 12 * scale, mon.Y + 24 * scale, sz.Width + 24 * scale, sz.Height + 12 * scale);
            using (var p = Theme.RoundRect(r, r.Height / 2)) using (var b = new SolidBrush(Color.FromArgb(220, 30, 30, 32))) g.FillPath(b, p);
            g.DrawString(text, uiFont, Brushes.White, r.X + 12 * scale, r.Y + 6 * scale);
        }

        Rectangle HintRect()
        {
            var mon = monitors.FirstOrDefault(m => m.Contains(mouse));
            if (mon.IsEmpty) mon = monitors[0];
            return new Rectangle(mon.X, mon.Y, mon.Width, (int)(80 * scale));
        }

        void DrawSizeLabel(Graphics g, Rectangle r)
        {
            string t = r.Width + " × " + r.Height;
            var sz = g.MeasureString(t, smallFont);
            float x = r.X, y = r.Y - sz.Height - 8 * scale;
            if (y < 0) y = r.Y + 4 * scale;
            var box = new RectangleF(x, y, sz.Width + 10 * scale, sz.Height + 4 * scale);
            using (var p = Theme.RoundRect(box, 4 * scale)) using (var b = new SolidBrush(Color.FromArgb(200, 0, 0, 0))) g.FillPath(b, p);
            g.DrawString(t, smallFont, Brushes.White, x + 5 * scale, y + 2 * scale);
        }

        Rectangle SizeLabelRect(Rectangle r)
        {
            int h = (int)(30 * scale);
            return new Rectangle(r.X - 2, r.Y - h - 4, (int)(160 * scale), h * 2 + 8);
        }

        Rectangle[] Handles()
        {
            int s = (int)(6 * scale) | 1, hs = s / 2;
            int l = sel.Left, t = sel.Top, r = sel.Right - 1, b = sel.Bottom - 1, cx = (l + r) / 2, cy = (t + b) / 2;
            return new[]
            {
                new Rectangle(l - hs, t - hs, s, s), new Rectangle(cx - hs, t - hs, s, s), new Rectangle(r - hs, t - hs, s, s),
                new Rectangle(r - hs, cy - hs, s, s), new Rectangle(r - hs, b - hs, s, s), new Rectangle(cx - hs, b - hs, s, s),
                new Rectangle(l - hs, b - hs, s, s), new Rectangle(l - hs, cy - hs, s, s)
            };
        }

        int HitHandle(Point p)
        {
            var hs = Handles();
            for (int i = 0; i < hs.Length; i++) { var r = hs[i]; r.Inflate((int)(4 * scale), (int)(4 * scale)); if (r.Contains(p)) return i; }
            // 边缘也可以拖
            int tol = (int)(4 * scale);
            if (Math.Abs(p.X - sel.Left) <= tol && p.Y > sel.Top && p.Y < sel.Bottom) return 7;
            if (Math.Abs(p.X - sel.Right) <= tol && p.Y > sel.Top && p.Y < sel.Bottom) return 3;
            if (Math.Abs(p.Y - sel.Top) <= tol && p.X > sel.Left && p.X < sel.Right) return 1;
            if (Math.Abs(p.Y - sel.Bottom) <= tol && p.X > sel.Left && p.X < sel.Right) return 5;
            return -1;
        }

        static readonly Cursor[] HandleCursors =
        {
            Cursors.SizeNWSE, Cursors.SizeNS, Cursors.SizeNESW, Cursors.SizeWE,
            Cursors.SizeNWSE, Cursors.SizeNS, Cursors.SizeNESW, Cursors.SizeWE
        };

        // ---------- 放大镜 ----------
        Rectangle MagnifierRect()
        {
            int cells = 17, cell = (int)(7 * scale);
            int w = cells * cell, h = w + (int)(44 * scale);
            int x = mouse.X + (int)(20 * scale), y = mouse.Y + (int)(20 * scale);
            var mon = monitors.FirstOrDefault(m => m.Contains(mouse));
            if (mon.IsEmpty) mon = new Rectangle(0, 0, Width, Height);
            if (x + w > mon.Right) x = mouse.X - w - (int)(20 * scale);
            if (y + h > mon.Bottom) y = mouse.Y - h - (int)(20 * scale);
            return new Rectangle(x, y, w, h);
        }

        void DrawMagnifier(Graphics g)
        {
            var r = MagnifierRect();
            int cells = 17, cell = r.Width / cells, half = cells / 2;
            var zoomR = new Rectangle(r.X, r.Y, r.Width, r.Width);
            var st = g.Save();
            g.InterpolationMode = InterpolationMode.NearestNeighbor;
            g.PixelOffsetMode = PixelOffsetMode.Half;
            g.SmoothingMode = SmoothingMode.None;
            g.FillRectangle(Brushes.Black, zoomR);
            g.DrawImage(screen, zoomR, new Rectangle(mouse.X - half, mouse.Y - half, cells, cells), GraphicsUnit.Pixel);
            g.Restore(st);
            using (var b = new SolidBrush(Color.FromArgb(110, Theme.Accent)))
            {
                g.FillRectangle(b, r.X, r.Y + half * cell, r.Width, cell);
                g.FillRectangle(b, r.X + half * cell, r.Y, cell, r.Width);
            }
            using (var p = new Pen(Color.White, 1)) g.DrawRectangle(p, r.X + half * cell, r.Y + half * cell, cell, cell);
            var info = new Rectangle(r.X, zoomR.Bottom, r.Width, r.Height - r.Width);
            using (var b = new SolidBrush(Color.FromArgb(230, 20, 20, 22))) g.FillRectangle(b, info);
            Color c = PixelAt(mouse);
            var screenPt = new Point(mouse.X + vbounds.X, mouse.Y + vbounds.Y);
            g.DrawString("(" + screenPt.X + ", " + screenPt.Y + ")", smallFont, Brushes.White, info.X + 6 * scale, info.Y + 4 * scale);
            using (var b = new SolidBrush(c)) g.FillRectangle(b, info.X + 6 * scale, info.Y + 24 * scale, 12 * scale, 12 * scale);
            g.DrawString(ColorHex(c) + "  按 C 复制", smallFont, Brushes.White, info.X + 22 * scale, info.Y + 22 * scale);
            using (var p = new Pen(Color.FromArgb(160, 255, 255, 255))) g.DrawRectangle(p, r.X, r.Y, r.Width - 1, r.Height - 1);
        }

        Color PixelAt(Point p)
        {
            if (p.X < 0 || p.Y < 0 || p.X >= screen.Width || p.Y >= screen.Height) return Color.Black;
            return screen.GetPixel(p.X, p.Y);
        }

        static string ColorHex(Color c) { return "#" + c.R.ToString("X2") + c.G.ToString("X2") + c.B.ToString("X2"); }

        // ---------- 工具栏 ----------
        void LayoutToolbar()
        {
            buttons.Clear();
            int bs = (int)(34 * scale), gap = (int)(2 * scale), sep = (int)(9 * scale), pad = (int)(5 * scale);
            const string mdl = "Segoe MDL2 Assets";
            var defs = new List<ToolButton>
            {
                new ToolButton { Glyph = "", GlyphFont = mdl, Tip = "矩形", Tool = Tool.Rect },
                new ToolButton { Glyph = "", GlyphFont = mdl, Tip = "椭圆", Tool = Tool.Ellipse },
                new ToolButton { Glyph = "", GlyphFont = mdl, Tip = "箭头", Tool = Tool.Arrow },
                new ToolButton { Glyph = "", GlyphFont = mdl, Tip = "画笔", Tool = Tool.Pen },
                new ToolButton { Glyph = "mosaic", Tip = "马赛克", Tool = Tool.Mosaic },
                new ToolButton { Glyph = "A", Tip = "文字", Tool = Tool.Text },
                new ToolButton { Separator = true },
                new ToolButton { Glyph = "", GlyphFont = mdl, Tip = "撤销  Ctrl+Z", Click = Undo },
                new ToolButton { Separator = true },
                new ToolButton { Glyph = "AI", Tip = "问 AI：把截图发给 AI Bot", Click = () => Finish(FinishAction.AskAi) },
                new ToolButton { Glyph = "文", Tip = "识别文字 OCR", Click = () => Finish(FinishAction.Ocr) },
                new ToolButton { Glyph = "", GlyphFont = mdl, Tip = "钉在屏幕  Ctrl+T / F3", Click = () => Finish(FinishAction.Pin) },
                new ToolButton { Glyph = "", GlyphFont = mdl, Tip = "保存  Ctrl+S", Click = () => Finish(FinishAction.Save) },
                new ToolButton { Glyph = "", GlyphFont = mdl, Tip = "退出  Esc", Click = Close },
                new ToolButton { Glyph = "", GlyphFont = mdl, Tip = "复制  Enter / Ctrl+C / 双击", Click = () => Finish(FinishAction.Copy) },
            };
            int w = pad * 2, h = bs + pad * 2;
            foreach (var d in defs) w += (d.Separator ? sep : bs) + gap;
            w -= gap;

            var mon = monitors.FirstOrDefault(m => m.IntersectsWith(sel) && m.Contains(new Point(sel.Right - 1, sel.Bottom - 1)));
            if (mon.IsEmpty) mon = monitors.OrderByDescending(m => { var i = Rectangle.Intersect(m, sel); return i.Width * i.Height; }).First();
            int x = sel.Right - w, y = sel.Bottom + (int)(8 * scale);
            bool below = true;
            int subH = bs + pad * 2 - (int)(6 * scale);
            if (y + h + subH > mon.Bottom) { y = sel.Top - h - (int)(8 * scale); below = false; }
            if (y - subH < mon.Top && !below) { y = sel.Bottom - h - (int)(8 * scale); below = true; }
            x = Math.Max(mon.Left + 4, Math.Min(x, mon.Right - w - 4));
            toolbarRect = new Rectangle(x, y, w, h);

            int cx = x + pad;
            foreach (var d in defs)
            {
                int bw = d.Separator ? sep : bs;
                d.Bounds = new Rectangle(cx, y + pad, bw, bs);
                cx += bw + gap;
                buttons.Add(d);
            }

            subbarRect = Rectangle.Empty;
            if (tool != Tool.None)
            {
                int sw = (int)(26 * scale);
                int n = Palette.Length + 3;
                int sbw = pad * 2 + n * (sw + gap) + sep;
                int sy = below ? toolbarRect.Bottom + (int)(4 * scale) : toolbarRect.Top - (int)(4 * scale) - (sw + pad * 2);
                subbarRect = new Rectangle(toolbarRect.Left, sy, sbw, sw + pad * 2);
                int sx = subbarRect.X + pad;
                for (int i = 0; i < 3; i++)
                {
                    buttons.Add(new ToolButton { Bounds = new Rectangle(sx, sy + pad, sw, sw), WidthLevel = i, Tip = new[] { "细", "中", "粗" }[i] });
                    sx += sw + gap;
                }
                sx += sep;
                foreach (var c in Palette)
                {
                    buttons.Add(new ToolButton { Bounds = new Rectangle(sx, sy + pad, sw, sw), Swatch = c, Tip = "颜色" });
                    sx += sw + gap;
                }
            }
        }

        void DrawToolbar(Graphics g)
        {
            using (var p = Theme.RoundRect(toolbarRect, 8 * scale))
            using (var b = new SolidBrush(Color.FromArgb(248, 250, 250, 252)))
            {
                g.FillPath(b, p);
                using (var pen = new Pen(Color.FromArgb(60, 0, 0, 0))) g.DrawPath(pen, p);
            }
            if (!subbarRect.IsEmpty)
                using (var p = Theme.RoundRect(subbarRect, 8 * scale))
                using (var b = new SolidBrush(Color.FromArgb(248, 250, 250, 252)))
                {
                    g.FillPath(b, p);
                    using (var pen = new Pen(Color.FromArgb(60, 0, 0, 0))) g.DrawPath(pen, p);
                }

            var sf = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
            foreach (var bt in buttons)
            {
                var r = bt.Bounds;
                if (bt.Separator)
                {
                    using (var pen = new Pen(Color.FromArgb(50, 0, 0, 0))) g.DrawLine(pen, r.X + r.Width / 2, r.Y + 6 * scale, r.X + r.Width / 2, r.Bottom - 6 * scale);
                    continue;
                }
                bool active = (bt.Tool != Tool.None && bt.Tool == tool) || (bt.WidthLevel >= 0 && bt.WidthLevel == widthLevel) ||
                              (bt.Swatch.HasValue && bt.Swatch.Value.ToArgb() == color.ToArgb());
                if (active || bt == hoverButton)
                    using (var p = Theme.RoundRect(r, 6 * scale))
                    using (var b = new SolidBrush(active ? Color.FromArgb(40, Theme.Accent) : Color.FromArgb(18, 0, 0, 0)))
                        g.FillPath(b, p);

                if (bt.Swatch.HasValue)
                {
                    var sr = RectangleF.Inflate(r, -6 * scale, -6 * scale);
                    using (var b = new SolidBrush(bt.Swatch.Value)) g.FillEllipse(b, sr);
                    using (var pen = new Pen(Color.FromArgb(80, 0, 0, 0))) g.DrawEllipse(pen, sr);
                    continue;
                }
                if (bt.WidthLevel >= 0)
                {
                    float d = (new[] { 4f, 7f, 11f })[bt.WidthLevel] * scale;
                    using (var b = new SolidBrush(Theme.Text)) g.FillEllipse(b, r.X + (r.Width - d) / 2, r.Y + (r.Height - d) / 2, d, d);
                    continue;
                }
                Color fg = active ? Theme.Accent : Theme.Text;
                if (bt.Glyph == "mosaic")
                {
                    float cs = 4 * scale, ox = r.X + (r.Width - cs * 4) / 2, oy = r.Y + (r.Height - cs * 4) / 2;
                    using (var b = new SolidBrush(fg))
                        for (int i = 0; i < 4; i++) for (int j = 0; j < 4; j++) if ((i + j) % 2 == 0) g.FillRectangle(b, ox + i * cs, oy + j * cs, cs, cs);
                    using (var pen = new Pen(fg)) g.DrawRectangle(pen, ox, oy, cs * 4, cs * 4);
                    continue;
                }
                var font = bt.GlyphFont != null ? glyphFont : glyphFontText;
                if (bt.Tip.StartsWith("复制")) fg = Theme.Accent;
                using (var b = new SolidBrush(fg)) g.DrawString(bt.Glyph, font, b, r, sf);
            }

            if (hoverButton != null && hoverButton.Tip != null)
            {
                var sz = g.MeasureString(hoverButton.Tip, smallFont);
                bool up = subbarRect.IsEmpty || subbarRect.Top > toolbarRect.Top;
                float tx = Math.Max(toolbarRect.Left, hoverButton.Bounds.X + hoverButton.Bounds.Width / 2 - sz.Width / 2 - 6 * scale);
                float ty = up ? toolbarRect.Top - sz.Height - 14 * scale : toolbarRect.Bottom + 6 * scale;
                if (!up && !subbarRect.IsEmpty) ty = subbarRect.Top - sz.Height - 14 * scale;
                var tr = new RectangleF(tx, ty, sz.Width + 12 * scale, sz.Height + 6 * scale);
                using (var p = Theme.RoundRect(tr, 4 * scale)) using (var b = new SolidBrush(Color.FromArgb(230, 30, 30, 32))) g.FillPath(b, p);
                g.DrawString(hoverButton.Tip, smallFont, Brushes.White, tr.X + 6 * scale, tr.Y + 3 * scale);
            }
        }

        Rectangle ToolbarDirty()
        {
            if (toolbarRect.IsEmpty) return Rectangle.Empty;
            var r = subbarRect.IsEmpty ? toolbarRect : Rectangle.Union(toolbarRect, subbarRect);
            r.Inflate((int)(60 * scale), (int)(40 * scale));
            return r;
        }

        // ---------- 标注绘制 ----------
        float PenWidth() { return (new[] { 2f, 4f, 7f })[widthLevel] * scale; }
        float MosaicWidth() { return (new[] { 14f, 24f, 40f })[widthLevel] * scale; }
        float FontPx() { return (new[] { 16f, 22f, 32f })[widthLevel] * scale; }

        void EnsureMosaic()
        {
            if (mosaicSmall != null) return;
            int cell = Math.Max(4, (int)(8 * scale));
            mosaicSmall = new Bitmap(Math.Max(1, screen.Width / cell), Math.Max(1, screen.Height / cell));
            using (var g = Graphics.FromImage(mosaicSmall))
            {
                g.InterpolationMode = InterpolationMode.HighQualityBilinear;
                g.DrawImage(screen, new Rectangle(0, 0, mosaicSmall.Width, mosaicSmall.Height));
            }
        }

        void DrawShape(Graphics g, Shape s)
        {
            if (s.Pts.Count == 0) return;
            Point a = s.Pts[0], b = s.Pts[s.Pts.Count - 1];
            var r = Rectangle.FromLTRB(Math.Min(a.X, b.X), Math.Min(a.Y, b.Y), Math.Max(a.X, b.X), Math.Max(a.Y, b.Y));
            using (var pen = new Pen(s.Color, s.Width) { LineJoin = LineJoin.Round, StartCap = LineCap.Round, EndCap = LineCap.Round })
            {
                switch (s.Kind)
                {
                    case Tool.Rect: g.DrawRectangle(pen, r); break;
                    case Tool.Ellipse: g.DrawEllipse(pen, r); break;
                    case Tool.Arrow:
                        if (a == b) break;
                        float len = (float)Math.Sqrt((b.X - a.X) * (b.X - a.X) + (b.Y - a.Y) * (b.Y - a.Y));
                        float head = Math.Min(len * 0.6f, s.Width * 4 + 10 * scale);
                        float ux = (b.X - a.X) / len, uy = (b.Y - a.Y) / len;
                        var tip = new PointF(b.X, b.Y);
                        var baseC = new PointF(b.X - ux * head, b.Y - uy * head);
                        float hw = head * 0.45f;
                        var p1 = new PointF(baseC.X - uy * hw, baseC.Y + ux * hw);
                        var p2 = new PointF(baseC.X + uy * hw, baseC.Y - ux * hw);
                        g.DrawLine(pen, a, new PointF(b.X - ux * head * 0.8f, b.Y - uy * head * 0.8f));
                        using (var br = new SolidBrush(s.Color)) g.FillPolygon(br, new[] { tip, p1, p2 });
                        break;
                    case Tool.Pen:
                        if (s.Pts.Count == 1) using (var br = new SolidBrush(s.Color)) g.FillEllipse(br, a.X - s.Width / 2, a.Y - s.Width / 2, s.Width, s.Width);
                        else g.DrawLines(pen, s.Pts.ToArray());
                        break;
                    case Tool.Mosaic:
                        EnsureMosaic();
                        using (var path = new GraphicsPath())
                        {
                            if (s.Pts.Count == 1) path.AddEllipse(a.X - s.Width / 2, a.Y - s.Width / 2, s.Width, s.Width);
                            else { path.AddLines(s.Pts.ToArray()); path.Widen(pen); }
                            var st = g.Save();
                            g.SetClip(path, CombineMode.Intersect);
                            g.InterpolationMode = InterpolationMode.NearestNeighbor;
                            g.PixelOffsetMode = PixelOffsetMode.Half;
                            g.DrawImage(mosaicSmall, new Rectangle(0, 0, screen.Width, screen.Height));
                            g.Restore(st);
                        }
                        break;
                    case Tool.Text:
                        if (!string.IsNullOrEmpty(s.Text))
                            using (var br = new SolidBrush(s.Color))
                            {
                                g.TextRenderingHint = TextRenderingHint.AntiAlias;
                                g.DrawString(s.Text, s.Font, br, a); // DrawString 会跟随坐标变换（导出时要平移）
                            }
                        break;
                }
            }
        }

        static Rectangle ShapeBounds(Shape s, float scale)
        {
            if (s.Pts.Count == 0) return Rectangle.Empty;
            int x1 = s.Pts.Min(p => p.X), y1 = s.Pts.Min(p => p.Y), x2 = s.Pts.Max(p => p.X), y2 = s.Pts.Max(p => p.Y);
            var r = Rectangle.FromLTRB(x1, y1, x2, y2);
            int inf = (int)(s.Width * 5 + 20 * scale);
            r.Inflate(inf, inf);
            return r;
        }

        // ---------- 鼠标 ----------
        void UpdateHover()
        {
            hover = null;
            if (hasSel) return;
            foreach (var r in winRects) if (r.Contains(mouse)) { hover = r; break; }
        }

        protected override void OnMouseDown(MouseEventArgs e)
        {
            base.OnMouseDown(e);
            mouse = e.Location;
            movedSinceDown = false;
            if (e.Button == MouseButtons.Right) { OnRightClick(); return; }
            if (e.Button != MouseButtons.Left) return;
            CommitText();

            if (hasSel && mode == CaptureMode.Normal)
            {
                var bt = buttons.FirstOrDefault(b => !b.Separator && b.Bounds.Contains(e.Location));
                if (bt != null) { OnButton(bt); return; }
                if (toolbarRect.Contains(e.Location) || subbarRect.Contains(e.Location)) return;

                if (tool != Tool.None)
                {
                    if (!sel.Contains(e.Location)) return;
                    if (tool == Tool.Text) { BeginText(e.Location); return; }
                    current = new Shape { Kind = tool, Color = color, Width = tool == Tool.Mosaic ? MosaicWidth() : PenWidth() };
                    current.Pts.Add(e.Location);
                    drag = DragKind.Drawing;
                    InvalidateDirty();
                    return;
                }
                int h = HitHandle(e.Location);
                if (h >= 0) { drag = DragKind.Resizing; resizeHandle = h; selAtDragStart = sel; dragStart = e.Location; Invalidate(ToolbarDirty()); return; }
                if (sel.Contains(e.Location)) { drag = DragKind.Moving; selAtDragStart = sel; dragStart = e.Location; Invalidate(ToolbarDirty()); return; }
                if (shapes.Count > 0) return; // 已有标注时不允许重新框选，避免误操作
            }
            // 开始新的框选
            hasSel = false;
            drag = DragKind.Creating;
            dragStart = e.Location;
            sel = new Rectangle(e.Location, Size.Empty);
            InvalidateDirty();
        }

        protected override void OnMouseMove(MouseEventArgs e)
        {
            base.OnMouseMove(e);
            var prev = mouse;
            mouse = e.Location;
            if (Math.Abs(mouse.X - dragStart.X) > 3 || Math.Abs(mouse.Y - dragStart.Y) > 3) movedSinceDown = true;
            switch (drag)
            {
                case DragKind.Creating:
                    if (movedSinceDown)
                    {
                        sel = Rectangle.FromLTRB(Math.Min(dragStart.X, mouse.X), Math.Min(dragStart.Y, mouse.Y),
                                                 Math.Max(dragStart.X, mouse.X) + 1, Math.Max(dragStart.Y, mouse.Y) + 1);
                        sel.Intersect(ClientRectangle);
                    }
                    break;
                case DragKind.Moving:
                    var r = selAtDragStart;
                    r.Offset(mouse.X - dragStart.X, mouse.Y - dragStart.Y);
                    r.X = Math.Max(0, Math.Min(r.X, Width - r.Width));
                    r.Y = Math.Max(0, Math.Min(r.Y, Height - r.Height));
                    MoveShapes(r.X - sel.X, r.Y - sel.Y); // 标注跟着选区走
                    sel = r;
                    break;
                case DragKind.Resizing:
                    sel = ResizeRect(selAtDragStart, resizeHandle, mouse.X - dragStart.X, mouse.Y - dragStart.Y);
                    break;
                case DragKind.Drawing:
                    var p = new Point(Math.Max(sel.Left, Math.Min(mouse.X, sel.Right - 1)), Math.Max(sel.Top, Math.Min(mouse.Y, sel.Bottom - 1)));
                    if (current.Kind == Tool.Pen || current.Kind == Tool.Mosaic) current.Pts.Add(p);
                    else { if (current.Pts.Count > 1) current.Pts[1] = p; else current.Pts.Add(p); }
                    break;
                default:
                    var oldHover = hover;
                    UpdateHover();
                    var hb = hasSel ? buttons.FirstOrDefault(b => !b.Separator && b.Bounds.Contains(mouse)) : null;
                    if (hb != hoverButton) { hoverButton = hb; Invalidate(ToolbarDirty()); }
                    UpdateCursor();
                    if (!hasSel && (oldHover != hover)) { Invalidate(); lastDirty = Rectangle.Empty; }
                    break;
            }
            if (ShowMagnifier() || drag != DragKind.None) InvalidateDirty();
        }

        void UpdateCursor()
        {
            if (!hasSel || mode == CaptureMode.Ocr) { Cursor = Cursors.Cross; return; }
            if (toolbarRect.Contains(mouse) || subbarRect.Contains(mouse)) { Cursor = Cursors.Hand; return; }
            if (tool != Tool.None) { Cursor = sel.Contains(mouse) ? (tool == Tool.Text ? Cursors.IBeam : Cursors.Cross) : Cursors.Default; return; }
            int h = HitHandle(mouse);
            Cursor = h >= 0 ? HandleCursors[h] : sel.Contains(mouse) ? Cursors.SizeAll : Cursors.Default;
        }

        static Rectangle ResizeRect(Rectangle r, int h, int dx, int dy)
        {
            int l = r.Left, t = r.Top, rt = r.Right, b = r.Bottom;
            if (h == 0 || h == 6 || h == 7) l += dx;
            if (h == 2 || h == 3 || h == 4) rt += dx;
            if (h == 0 || h == 1 || h == 2) t += dy;
            if (h == 4 || h == 5 || h == 6) b += dy;
            return Rectangle.FromLTRB(Math.Min(l, rt), Math.Min(t, b), Math.Max(Math.Max(l, rt), Math.Min(l, rt) + 1), Math.Max(Math.Max(t, b), Math.Min(t, b) + 1));
        }

        protected override void OnMouseUp(MouseEventArgs e)
        {
            base.OnMouseUp(e);
            if (e.Button != MouseButtons.Left) return;
            var d = drag;
            drag = DragKind.None;
            if (d == DragKind.Creating)
            {
                if (!movedSinceDown || sel.Width < 3 || sel.Height < 3)
                {
                    if (hover.HasValue) sel = hover.Value; // 单击 = 选中鼠标下的窗口
                    else { Invalidate(); return; }
                }
                hasSel = true;
                hover = null;
                if (mode == CaptureMode.Ocr) { Finish(FinishAction.Ocr); return; }
                LayoutToolbar();
                Invalidate();
                lastDirty = Rectangle.Empty;
            }
            else if (d == DragKind.Drawing)
            {
                if (current != null && (current.Pts.Count > 1 || current.Kind == Tool.Pen || current.Kind == Tool.Mosaic)) shapes.Add(current);
                current = null;
                InvalidateDirty();
            }
            else if (d == DragKind.Moving || d == DragKind.Resizing)
            {
                LayoutToolbar();
                Invalidate();
                lastDirty = Rectangle.Empty;
            }
            UpdateCursor();
        }

        protected override void OnMouseDoubleClick(MouseEventArgs e)
        {
            base.OnMouseDoubleClick(e);
            if (e.Button == MouseButtons.Left && hasSel && tool == Tool.None && sel.Contains(e.Location) && !toolbarRect.Contains(e.Location))
                Finish(FinishAction.Copy);
        }

        protected override void OnMouseWheel(MouseEventArgs e)
        {
            base.OnMouseWheel(e);
            if (tool == Tool.None) return;
            widthLevel = Math.Max(0, Math.Min(2, widthLevel + (e.Delta > 0 ? 1 : -1)));
            lastWidthLevel = widthLevel;
            Invalidate(ToolbarDirty());
        }

        void OnRightClick()
        {
            if (textBox != null) { CommitText(); return; }
            if (drag != DragKind.None) return;
            if (hasSel && mode == CaptureMode.Normal)
            {
                hasSel = false; shapes.Clear(); tool = Tool.None; toolbarRect = subbarRect = Rectangle.Empty;
                UpdateHover(); Invalidate(); lastDirty = Rectangle.Empty; UpdateCursor();
                return;
            }
            Close();
        }

        void OnButton(ToolButton bt)
        {
            if (bt.Click != null) { bt.Click(); return; }
            if (bt.Swatch.HasValue) { color = lastColor = bt.Swatch.Value; }
            else if (bt.WidthLevel >= 0) { widthLevel = lastWidthLevel = bt.WidthLevel; }
            else if (bt.Tool != Tool.None) { tool = tool == bt.Tool ? Tool.None : bt.Tool; LayoutToolbar(); }
            Invalidate(); lastDirty = Rectangle.Empty;
        }

        void Undo()
        {
            if (textBox != null) { textBox.Dispose(); textBox = null; return; }
            if (shapes.Count > 0) { shapes.RemoveAt(shapes.Count - 1); Invalidate(); }
        }

        // ---------- 文字工具 ----------
        void BeginText(Point p)
        {
            textPos = p;
            var font = new Font(Theme.FontName, FontPx(), GraphicsUnit.Pixel);
            textBox = new TextBox
            {
                Multiline = true, BorderStyle = BorderStyle.FixedSingle, Font = font, ForeColor = color,
                BackColor = color.GetBrightness() > 0.8f ? Color.FromArgb(60, 60, 60) : Color.White,
                Location = p, Size = new Size((int)(font.Height * 4), font.Height + 8)
            };
            textBox.TextChanged += (s, e) =>
            {
                var sz = TextRenderer.MeasureText(textBox.Text + "W", textBox.Font);
                textBox.Size = new Size(Math.Max(sz.Width + 8, font.Height * 4), Math.Max(sz.Height + 8, font.Height + 8));
            };
            Controls.Add(textBox);
            textBox.Focus();
        }

        void CommitText()
        {
            if (textBox == null) return;
            var tb = textBox;
            textBox = null;
            if (tb.Text.Trim().Length > 0)
            {
                var s = new Shape { Kind = Tool.Text, Color = tb.ForeColor, Width = 1, Text = tb.Text, Font = tb.Font };
                s.Pts.Add(new Point(textPos.X + 2, textPos.Y + 3));
                var sz = TextRenderer.MeasureText(tb.Text, tb.Font);
                s.Pts.Add(new Point(textPos.X + sz.Width, textPos.Y + sz.Height));
                shapes.Add(s);
            }
            Controls.Remove(tb);
            tb.Dispose();
            Focus();
            Invalidate();
        }

        // ---------- 键盘 ----------
        protected override bool ProcessCmdKey(ref Message msg, Keys keyData)
        {
            if (textBox != null)
            {
                if (keyData == Keys.Escape || keyData == (Keys.Control | Keys.Enter)) { CommitText(); return true; }
                return base.ProcessCmdKey(ref msg, keyData);
            }
            switch (keyData)
            {
                case Keys.Escape: Close(); return true;
                case Keys.Enter:
                case Keys.Control | Keys.C: if (hasSel) Finish(FinishAction.Copy); return true;
                case Keys.Control | Keys.S: if (hasSel) Finish(FinishAction.Save); return true;
                case Keys.Control | Keys.T:
                case Keys.F3: if (hasSel) Finish(FinishAction.Pin); return true;
                case Keys.Control | Keys.Z: Undo(); return true;
                case Keys.Control | Keys.A:
                    var mon = monitors.FirstOrDefault(m => m.Contains(mouse));
                    sel = mon.IsEmpty ? ClientRectangle : mon;
                    hasSel = true; hover = null; drag = DragKind.None;
                    if (mode == CaptureMode.Ocr) { Finish(FinishAction.Ocr); return true; }
                    LayoutToolbar(); Invalidate(); return true;
                case Keys.C:
                    if (!hasSel || ShowMagnifier())
                    {
                        string hex = ColorHex(PixelAt(mouse));
                        Clipboard.SetText(hex);
                        Close();
                        Host.Notify("已复制颜色", hex);
                    }
                    return true;
                case Keys.Left: case Keys.Right: case Keys.Up: case Keys.Down:
                case Keys.Shift | Keys.Left: case Keys.Shift | Keys.Right: case Keys.Shift | Keys.Up: case Keys.Shift | Keys.Down:
                    Nudge(keyData); return true;
            }
            return base.ProcessCmdKey(ref msg, keyData);
        }

        void Nudge(Keys k)
        {
            bool resize = (k & Keys.Shift) != 0;
            k &= ~Keys.Shift;
            int dx = k == Keys.Left ? -1 : k == Keys.Right ? 1 : 0, dy = k == Keys.Up ? -1 : k == Keys.Down ? 1 : 0;
            if (!hasSel)
            {
                // 没有选区时，方向键移动鼠标（精确定位）
                Cursor.Position = new Point(Cursor.Position.X + dx, Cursor.Position.Y + dy);
                return;
            }
            if (resize) sel = new Rectangle(sel.X, sel.Y, Math.Max(1, sel.Width + dx), Math.Max(1, sel.Height + dy));
            else { sel.Offset(dx, dy); MoveShapes(dx, dy); }
            LayoutToolbar(); Invalidate();
        }

        void MoveShapes(int dx, int dy)
        {
            if (dx == 0 && dy == 0) return;
            foreach (var s in shapes) for (int i = 0; i < s.Pts.Count; i++) s.Pts[i] = new Point(s.Pts[i].X + dx, s.Pts[i].Y + dy);
        }

        // ---------- 局部重绘 ----------
        void InvalidateDirty()
        {
            var r = Rectangle.Empty;
            Rectangle bright = hasSel || drag == DragKind.Creating ? sel : (hover.HasValue ? hover.Value : Rectangle.Empty);
            if (bright.Width > 0 || bright.Height > 0)
            {
                var b = bright; b.Inflate((int)(12 * scale), (int)(12 * scale));
                r = b;
                r = Rectangle.Union(r, SizeLabelRect(bright));
            }
            if (current != null) r = r.IsEmpty ? ShapeBounds(current, scale) : Rectangle.Union(r, ShapeBounds(current, scale));
            if (ShowMagnifier()) { var m = MagnifierRect(); m.Inflate(4, 4); r = r.IsEmpty ? m : Rectangle.Union(r, m); }
            if (mode == CaptureMode.Ocr) r = r.IsEmpty ? HintRect() : Rectangle.Union(r, HintRect());
            var all = lastDirty.IsEmpty ? r : Rectangle.Union(lastDirty, r);
            if (!all.IsEmpty) Invalidate(all);
            lastDirty = r;
        }

        // ---------- 完成 ----------
        enum FinishAction { Copy, Save, Pin, Ocr, AskAi }

        Bitmap RenderResult()
        {
            var r = Rectangle.Intersect(sel, new Rectangle(0, 0, screen.Width, screen.Height));
            var bmp = new Bitmap(r.Width, r.Height, PixelFormat.Format32bppArgb);
            using (var g = Graphics.FromImage(bmp))
            {
                g.DrawImage(screen, new Rectangle(0, 0, r.Width, r.Height), r, GraphicsUnit.Pixel);
                g.SmoothingMode = SmoothingMode.AntiAlias;
                g.TranslateTransform(-r.X, -r.Y);
                foreach (var s in shapes) DrawShape(g, s);
            }
            return bmp;
        }

        void Finish(FinishAction action)
        {
            CommitText();
            if (!hasSel) return;
            var bmp = RenderResult();
            var screenRect = ToScreenRect(sel);
            Host.Log("截图完成 " + action + " " + screenRect);
            Hide();
            switch (action)
            {
                case FinishAction.Copy:
                    ClipboardImage.Set(bmp);
                    bmp.Dispose();
                    break;
                case FinishAction.Pin:
                    PinWindow.Open(bmp, screenRect.Location);
                    break;
                case FinishAction.Save:
                    SaveImage(bmp);
                    bmp.Dispose();
                    break;
                case FinishAction.AskAi:
                    Host.AskAiImage(bmp);
                    bmp.Dispose();
                    break;
                case FinishAction.Ocr:
                    var ocr = Host.Get<IOcrService>();
                    if (ocr == null) { Host.Notify("OCR 模块未启用", "在 config.json 的 modules 里打开 ocr"); bmp.Dispose(); }
                    else ocr.Recognize(bmp, true);
                    break;
            }
            Close();
        }

        public static void SaveImage(Bitmap bmp)
        {
            var cfg = Host.Config.Section("capture");
            string folder = cfg.Str("saveFolder");
            if (string.IsNullOrEmpty(folder) || !Directory.Exists(folder)) folder = Environment.GetFolderPath(Environment.SpecialFolder.MyPictures);
            using (var dlg = new SaveFileDialog
            {
                Filter = "PNG 图片|*.png|JPEG 图片|*.jpg|BMP 图片|*.bmp",
                FileName = "SOP_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".png",
                InitialDirectory = folder
            })
            {
                if (dlg.ShowDialog() != DialogResult.OK) return;
                var fmt = dlg.FilterIndex == 2 ? ImageFormat.Jpeg : dlg.FilterIndex == 3 ? ImageFormat.Bmp : ImageFormat.Png;
                bmp.Save(dlg.FileName, fmt);
                cfg.Set("saveFolder", Path.GetDirectoryName(dlg.FileName));
                Host.Config.Save();
            }
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                screen.Dispose(); dim.Dispose();
                if (mosaicSmall != null) mosaicSmall.Dispose();
                uiFont.Dispose(); smallFont.Dispose(); glyphFont.Dispose(); glyphFontText.Dispose();
            }
            base.Dispose(disposing);
        }

        /// <summary>开发调试用：把遮罩在“已选中 + 显示工具栏”状态下渲染成图片。</summary>
        public Bitmap RenderPreview(Rectangle selection, int toolIndex)
        {
            sel = selection; hasSel = true; hover = null;
            if (toolIndex > 0) tool = (Tool)toolIndex;
            var s1 = new Shape { Kind = Tool.Rect, Color = color, Width = PenWidth() };
            s1.Pts.Add(new Point(sel.X + 20, sel.Y + 20)); s1.Pts.Add(new Point(sel.X + 120, sel.Y + 80));
            var s2 = new Shape { Kind = Tool.Arrow, Color = color, Width = PenWidth() };
            s2.Pts.Add(new Point(sel.X + 200, sel.Y + 140)); s2.Pts.Add(new Point(sel.X + 130, sel.Y + 90));
            var s3 = new Shape { Kind = Tool.Mosaic, Color = color, Width = MosaicWidth() };
            for (int i = 0; i < 10; i++) s3.Pts.Add(new Point(sel.X + 40 + i * 15, sel.Y + 150));
            shapes.Add(s1); shapes.Add(s2); shapes.Add(s3);
            LayoutToolbar();
            var pad = Rectangle.Union(sel, Rectangle.Union(toolbarRect, subbarRect.IsEmpty ? toolbarRect : subbarRect));
            pad.Inflate(60, 60);
            pad.Intersect(new Rectangle(0, 0, screen.Width, screen.Height));
            var bmp = new Bitmap(screen.Width, screen.Height);
            using (var g = Graphics.FromImage(bmp)) OnPaint(new PaintEventArgs(g, new Rectangle(0, 0, screen.Width, screen.Height)));
            var crop = bmp.Clone(pad, bmp.PixelFormat);
            bmp.Dispose();
            return crop;
        }
    }

    public static class ClipboardImage
    {
        /// <summary>同时放入位图和 PNG，粘贴到各种软件都能保持清晰。</summary>
        public static void Set(Bitmap bmp)
        {
            var data = new DataObject();
            data.SetData(DataFormats.Bitmap, true, bmp);
            var ms = new MemoryStream();
            bmp.Save(ms, ImageFormat.Png);
            data.SetData("PNG", false, ms);
            for (int i = 0; i < 5; i++)
            {
                try { Clipboard.SetDataObject(data, true); return; }
                catch (System.Runtime.InteropServices.ExternalException) { System.Threading.Thread.Sleep(60); }
            }
        }

        public static Bitmap Get()
        {
            try
            {
                var data = Clipboard.GetDataObject();
                if (data == null) return null;
                if (data.GetDataPresent("PNG"))
                {
                    var s = data.GetData("PNG") as Stream;
                    if (s != null) return new Bitmap(s);
                }
                if (Clipboard.ContainsImage()) { var img = Clipboard.GetImage(); return img == null ? null : new Bitmap(img); }
                if (Clipboard.ContainsFileDropList())
                    foreach (string f in Clipboard.GetFileDropList())
                    {
                        string ext = Path.GetExtension(f).ToLowerInvariant();
                        if (ext == ".png" || ext == ".jpg" || ext == ".jpeg" || ext == ".bmp" || ext == ".gif" || ext == ".webp" || ext == ".tif" || ext == ".tiff")
                            using (var img = Image.FromFile(f)) return new Bitmap(img);
                    }
            }
            catch (Exception ex) { Host.Log("读取剪贴板图片失败: " + ex.Message); }
            return null;
        }
    }

    /// <summary>OCR 服务（由 OCR 模块提供，截图/贴图模块调用）。</summary>
    public interface IOcrService
    {
        /// <summary>识别图片；完成后按配置复制结果并提示。会负责释放 bmp。</summary>
        void Recognize(Bitmap bmp, bool disposeAfter);
        /// <summary>提前启动引擎（用户框选的同时预热）。</summary>
        void Prewarm();
    }
}
