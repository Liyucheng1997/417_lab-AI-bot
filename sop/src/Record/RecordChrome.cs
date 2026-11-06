using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Drawing.Text;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using MySOP.Core;

namespace MySOP.Record
{
    /// <summary>录制时屏幕上的东西：录屏框（红色细边，点击穿透）+ 控制条（计时、声音开关、暂停、停止）。都不会被录进视频。</summary>
    public sealed class RecordChrome : IDisposable
    {
        readonly FrameWindow frame;
        readonly ControlBar bar;

        public RecordChrome(Rectangle region, ScreenRecorder rec, bool audioWanted, Action stop)
        {
            var mon = Screen.FromRectangle(region).Bounds;
            float scale = Native.ScaleAt(new Point(region.X + region.Width / 2, region.Y + region.Height / 2));
            frame = new FrameWindow(region, mon, Math.Max(2, (int)Math.Round(2 * scale)));
            bar = new ControlBar(region, mon, scale, rec, audioWanted, stop);
            frame.Show();
            bar.Show();
        }

        /// <summary>引擎真正开始录了（之前显示“准备中”）。</summary>
        public void SetRunning() { bar.State = ControlBar.Phase.Running; bar.Render(); }
        public void SetSaving() { bar.State = ControlBar.Phase.Saving; bar.Render(); }

        public void Dispose()
        {
            frame.Close(); frame.Dispose();
            bar.Close(); bar.Dispose();
        }

        // 让窗口不出现在任何截图 / 录屏里（Windows 10 2004 起）
        internal static void ExcludeFromCapture(IntPtr hwnd)
        {
            if (Environment.GetEnvironmentVariable("MYSOP_SHOW_CHROME") == "1") return; // 调试：截图时能看到录屏框和控制条
            try { SetWindowDisplayAffinity(hwnd, 0x11); } catch { }
        }
        [DllImport("user32.dll")] static extern bool SetWindowDisplayAffinity(IntPtr hwnd, int affinity);

        // ---------------- 录屏框 ----------------
        sealed class FrameWindow : Form
        {
            public FrameWindow(Rectangle region, Rectangle monitor, int t)
            {
                FormBorderStyle = FormBorderStyle.None;
                StartPosition = FormStartPosition.Manual;
                ShowInTaskbar = false;
                TopMost = true;
                BackColor = Color.FromArgb(255, 59, 48);
                Opacity = 0.92; // 顺便让窗口变成分层窗口，配合 WS_EX_TRANSPARENT 才能点击穿透
                var outer = Rectangle.Intersect(Rectangle.Inflate(region, t, t), monitor);
                if (outer.IsEmpty) outer = region;
                Bounds = outer;
                var ring = new System.Drawing.Region(new Rectangle(0, 0, outer.Width, outer.Height));
                var hole = region; hole.Offset(-outer.X, -outer.Y);
                ring.Exclude(hole);
                Region = ring;
            }

            protected override bool ShowWithoutActivation { get { return true; } }
            protected override CreateParams CreateParams
            {
                get
                {
                    var cp = base.CreateParams;
                    cp.ExStyle |= 0x20 /*TRANSPARENT*/ | 0x80000 /*LAYERED*/ | Native.WS_EX_TOOLWINDOW | Native.WS_EX_NOACTIVATE | 0x8 /*TOPMOST*/;
                    return cp;
                }
            }
            protected override void OnHandleCreated(EventArgs e) { base.OnHandleCreated(e); ExcludeFromCapture(Handle); }
            protected override void WndProc(ref Message m)
            {
                if (m.Msg == 0x02E0) return; // WM_DPICHANGED：大小按物理像素算好了，别让系统缩放
                base.WndProc(ref m);
            }
        }

        // ---------------- 控制条 ----------------
        sealed class ControlBar : Form
        {
            public enum Phase { Starting, Running, Saving }
            public Phase State = Phase.Starting;

            readonly ScreenRecorder rec;
            readonly Action stop;
            readonly bool audioWanted;
            readonly float s;
            readonly Font timeFont, labelFont, glyphFont;
            readonly Timer timer = new Timer { Interval = 250 };
            Rectangle audioBtn, pauseBtn, stopBtn;
            string hot;
            int tick;

            public ControlBar(Rectangle region, Rectangle monitor, float scale, ScreenRecorder rec, bool audioWanted, Action stop)
            {
                this.rec = rec; this.stop = stop; this.audioWanted = audioWanted; s = scale;
                timeFont = new Font("Segoe UI", 14 * s, FontStyle.Bold, GraphicsUnit.Pixel);
                labelFont = new Font(Theme.FontName, 13 * s, FontStyle.Bold, GraphicsUnit.Pixel);
                glyphFont = new Font("Segoe MDL2 Assets", 14 * s, GraphicsUnit.Pixel);
                FormBorderStyle = FormBorderStyle.None;
                StartPosition = FormStartPosition.Manual;
                ShowInTaskbar = false;
                TopMost = true;

                // 布局：● 00:00 | 声音 暂停 [■ 停止]
                int h = (int)(44 * s), pad = (int)(8 * s), bs = (int)(32 * s);
                int timeW = TextRenderer.MeasureText("00:00:00", timeFont).Width;
                int stopW = bs + TextRenderer.MeasureText("停止", labelFont).Width + (int)(10 * s);
                int x = pad + (int)(18 * s) + timeW + (int)(6 * s);
                audioBtn = new Rectangle(x, (h - bs) / 2, bs, bs); x += bs + (int)(2 * s);
                pauseBtn = new Rectangle(x, (h - bs) / 2, bs, bs); x += bs + (int)(6 * s);
                stopBtn = new Rectangle(x, (h - bs) / 2, stopW, bs); x += stopW + pad;
                int w = x;

                // 位置：优先放在录屏框下方靠右，放不下就上方，再不行放进框里（反正不会被录进去）
                var area = Screen.FromRectangle(region).WorkingArea;
                int gap = (int)(10 * s);
                int bx = Math.Max(area.Left + 4, Math.Min(region.Right - w, area.Right - w - 4));
                int by = region.Bottom + gap;
                if (by + h > area.Bottom)
                {
                    by = region.Top - h - gap;
                    if (by < area.Top) { by = Math.Min(region.Bottom, area.Bottom) - h - (int)(20 * s); bx = region.X + (region.Width - w) / 2; }
                }
                Bounds = new Rectangle(bx, by, w, h);

                timer.Tick += (o, e) => { tick++; Render(); };
                timer.Start();
            }

            protected override bool ShowWithoutActivation { get { return true; } }
            protected override CreateParams CreateParams
            {
                get
                {
                    var cp = base.CreateParams;
                    cp.ExStyle |= 0x80000 /*LAYERED*/ | Native.WS_EX_TOOLWINDOW | Native.WS_EX_NOACTIVATE | 0x8 /*TOPMOST*/;
                    return cp;
                }
            }

            protected override void OnHandleCreated(EventArgs e)
            {
                base.OnHandleCreated(e);
                ExcludeFromCapture(Handle);
                Render();
            }

            protected override void WndProc(ref Message m)
            {
                if (m.Msg == 0x02E0) return;
                base.WndProc(ref m);
            }

            string HitTest(Point p)
            {
                if (State != Phase.Running) return null;
                if (audioWanted && rec.HasAudio && audioBtn.Contains(p)) return "audio";
                if (pauseBtn.Contains(p)) return "pause";
                if (stopBtn.Contains(p)) return "stop";
                return null;
            }

            protected override void OnMouseMove(MouseEventArgs e)
            {
                base.OnMouseMove(e);
                var h = HitTest(e.Location);
                Cursor = h != null ? Cursors.Hand : Cursors.SizeAll;
                if (h != hot) { hot = h; Render(); }
            }

            protected override void OnMouseLeave(EventArgs e) { base.OnMouseLeave(e); if (hot != null) { hot = null; Render(); } }

            protected override void OnMouseDown(MouseEventArgs e)
            {
                base.OnMouseDown(e);
                if (e.Button != MouseButtons.Left) return;
                switch (HitTest(e.Location))
                {
                    case "audio": rec.Muted = !rec.Muted; Render(); return;
                    case "pause": rec.Paused = !rec.Paused; Render(); return;
                    case "stop": stop(); return;
                }
                // 其余地方：拖动控制条
                ReleaseCapture();
                SendMessage(Handle, 0xA1 /*WM_NCLBUTTONDOWN*/, (IntPtr)2 /*HTCAPTION*/, IntPtr.Zero);
            }

            static string Format(TimeSpan t)
            {
                return t.TotalHours >= 1 ? ((int)t.TotalHours).ToString("00") + ":" + t.Minutes.ToString("00") + ":" + t.Seconds.ToString("00")
                                         : t.Minutes.ToString("00") + ":" + t.Seconds.ToString("00");
            }

            public void Render()
            {
                if (!IsHandleCreated || IsDisposed) return;
                int w = Width, h = Height;
                using (var bmp = new Bitmap(w, h, PixelFormat.Format32bppPArgb))
                {
                    using (var g = Graphics.FromImage(bmp))
                    {
                        g.SmoothingMode = SmoothingMode.AntiAlias;
                        g.TextRenderingHint = TextRenderingHint.AntiAliasGridFit;
                        using (var path = Theme.RoundRect(new RectangleF(0.5f, 0.5f, w - 1, h - 1), (h - 1) / 2f))
                        {
                            using (var b = new SolidBrush(Color.FromArgb(238, 28, 28, 30))) g.FillPath(b, path);
                            using (var pen = new Pen(Color.FromArgb(50, 255, 255, 255))) g.DrawPath(pen, path);
                        }
                        bool paused = rec.Paused;
                        // 状态点：录制中红点闪烁，暂停橙色，准备 / 保存灰色
                        float dot = 10 * s, dx = 14 * s, dy = (h - dot) / 2;
                        Color dc = State != Phase.Running ? Color.FromArgb(150, 150, 155)
                                 : paused ? Color.FromArgb(255, 159, 10)
                                 : (tick % 4 < 3 ? Color.FromArgb(255, 69, 58) : Color.FromArgb(120, 255, 69, 58));
                        using (var b = new SolidBrush(dc)) g.FillEllipse(b, dx, dy, dot, dot);

                        string text = State == Phase.Starting ? "准备中" : State == Phase.Saving ? "保存中" : Format(rec.Elapsed);
                        var font = State == Phase.Running ? timeFont : labelFont;
                        var sf = new StringFormat { LineAlignment = StringAlignment.Center };
                        g.DrawString(text, font, Brushes.White, new RectangleF(dx + dot + 6 * s, 0, audioBtn.X - dx - dot, h), sf);

                        bool enabled = State == Phase.Running;
                        if (audioWanted)
                        {
                            bool on = rec.HasAudio && !rec.Muted;
                            Glyph(g, audioBtn, on ? "" : "", enabled && rec.HasAudio, hot == "audio",
                                  on ? Color.White : Color.FromArgb(160, 160, 165));
                        }
                        Glyph(g, pauseBtn, paused ? "" : "", enabled, hot == "pause", Color.White);

                        using (var p = Theme.RoundRect(stopBtn, stopBtn.Height / 2f))
                        using (var b = new SolidBrush(!enabled ? Color.FromArgb(90, 90, 95) : hot == "stop" ? Color.FromArgb(225, 45, 35) : Color.FromArgb(255, 59, 48)))
                            g.FillPath(b, p);
                        float sq = 10 * s;
                        using (var p = Theme.RoundRect(new RectangleF(stopBtn.X + 12 * s, stopBtn.Y + (stopBtn.Height - sq) / 2, sq, sq), 2 * s))
                            g.FillPath(Brushes.White, p);
                        g.DrawString("停止", labelFont, Brushes.White, new RectangleF(stopBtn.X + 12 * s + sq + 5 * s, stopBtn.Y, stopBtn.Width, stopBtn.Height), sf);
                    }
                    Apply(bmp);
                }
            }

            void Glyph(Graphics g, Rectangle r, string glyph, bool enabled, bool hover, Color color)
            {
                if (hover && enabled)
                    using (var b = new SolidBrush(Color.FromArgb(40, 255, 255, 255))) g.FillEllipse(b, r);
                var sf = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
                using (var b = new SolidBrush(enabled ? color : Color.FromArgb(90, 90, 95))) g.DrawString(glyph, glyphFont, b, r, sf);
            }

            void Apply(Bitmap bmp)
            {
                IntPtr screen = GetDC(IntPtr.Zero), mem = CreateCompatibleDC(screen);
                IntPtr hbmp = bmp.GetHbitmap(Color.FromArgb(0)), old = SelectObject(mem, hbmp);
                try
                {
                    var size = new SIZE { cx = bmp.Width, cy = bmp.Height };
                    var src = new Native.POINT();
                    var pos = new Native.POINT { X = Left, Y = Top };
                    var blend = new BLENDFUNCTION { BlendOp = 0, SourceConstantAlpha = 255, AlphaFormat = 1 };
                    UpdateLayeredWindow(Handle, screen, ref pos, ref size, mem, ref src, 0, ref blend, 2 /*ULW_ALPHA*/);
                }
                finally
                {
                    SelectObject(mem, old);
                    DeleteObject(hbmp);
                    DeleteDC(mem);
                    ReleaseDC(IntPtr.Zero, screen);
                }
            }

            protected override void Dispose(bool disposing)
            {
                if (disposing) { timer.Dispose(); timeFont.Dispose(); labelFont.Dispose(); glyphFont.Dispose(); }
                base.Dispose(disposing);
            }

            [StructLayout(LayoutKind.Sequential)] struct SIZE { public int cx, cy; }
            [StructLayout(LayoutKind.Sequential, Pack = 1)] struct BLENDFUNCTION { public byte BlendOp, BlendFlags, SourceConstantAlpha, AlphaFormat; }
            [DllImport("user32.dll")] static extern bool UpdateLayeredWindow(IntPtr hwnd, IntPtr dst, ref Native.POINT pos, ref SIZE size, IntPtr src, ref Native.POINT srcPos, int key, ref BLENDFUNCTION blend, int flags);
            [DllImport("user32.dll")] static extern IntPtr GetDC(IntPtr hwnd);
            [DllImport("user32.dll")] static extern int ReleaseDC(IntPtr hwnd, IntPtr dc);
            [DllImport("user32.dll")] static extern bool ReleaseCapture();
            [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr hwnd, int msg, IntPtr w, IntPtr l);
            [DllImport("gdi32.dll")] static extern IntPtr CreateCompatibleDC(IntPtr dc);
            [DllImport("gdi32.dll")] static extern bool DeleteDC(IntPtr dc);
            [DllImport("gdi32.dll")] static extern IntPtr SelectObject(IntPtr dc, IntPtr obj);
            [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr obj);
        }
    }
}
