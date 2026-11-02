using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Linq;
using System.Windows.Forms;
using MySOP.Core;

namespace MySOP.Capture
{
    /// <summary>
    /// 贴图窗口（仿 Snipaste）：置顶显示图片。
    /// 拖动 = 移动，滚轮 = 缩放，Ctrl+滚轮 = 透明度，双击 / Esc = 关闭，右键 = 菜单。
    /// </summary>
    public class PinWindow : Form
    {
        static readonly List<PinWindow> open = new List<PinWindow>();
        static bool allHidden;

        readonly Bitmap image;
        float zoom = 1f;
        Point dragOffset;
        bool dragging;

        public static void Open(Bitmap bmp, Point location)
        {
            var w = new PinWindow(bmp, location);
            open.Add(w);
            allHidden = false;
            foreach (var p in open) if (!p.Visible) p.Show();
            w.Show();
            w.Activate();
        }

        /// <summary>把剪贴板内容（图片 / 图片文件 / 文字）钉到屏幕上。</summary>
        public static void FromClipboard()
        {
            var bmp = ClipboardImage.Get();
            if (bmp == null && Clipboard.ContainsText()) bmp = RenderText(Clipboard.GetText());
            if (bmp == null) { Host.Notify("剪贴板里没有图片或文字", "先复制一张图片再按贴图热键"); return; }
            var c = Cursor.Position;
            var area = Screen.FromPoint(c).WorkingArea;
            int x = Math.Max(area.Left, Math.Min(c.X - bmp.Width / 2, area.Right - bmp.Width));
            int y = Math.Max(area.Top, Math.Min(c.Y - bmp.Height / 2, area.Bottom - bmp.Height));
            Open(bmp, new Point(x, y));
        }

        public static void ToggleAll()
        {
            open.RemoveAll(p => p.IsDisposed);
            if (open.Count == 0) return;
            allHidden = !allHidden;
            foreach (var p in open) { if (allHidden) p.Hide(); else p.Show(); }
        }

        public static void CloseAll()
        {
            foreach (var p in open.ToList()) p.Close();
            open.Clear();
        }

        static Bitmap RenderText(string text)
        {
            if (text.Length > 4000) text = text.Substring(0, 4000) + "…";
            float s = Native.ScaleAt(Cursor.Position);
            using (var font = new Font(Theme.FontName, 14 * s, GraphicsUnit.Pixel))
            {
                int pad = (int)(12 * s), maxW = (int)(620 * s);
                var sz = TextRenderer.MeasureText(text, font, new Size(maxW, 100000), TextFormatFlags.WordBreak);
                var bmp = new Bitmap(sz.Width + pad * 2, sz.Height + pad * 2);
                using (var g = Graphics.FromImage(bmp))
                {
                    g.Clear(Color.FromArgb(255, 253, 246));
                    TextRenderer.DrawText(g, text, font, new Rectangle(pad, pad, sz.Width, sz.Height), Theme.Text, TextFormatFlags.WordBreak);
                }
                return bmp;
            }
        }

        PinWindow(Bitmap bmp, Point location)
        {
            image = bmp;
            FormBorderStyle = FormBorderStyle.None;
            StartPosition = FormStartPosition.Manual;
            ShowInTaskbar = false;
            TopMost = true;
            DoubleBuffered = true;
            KeyPreview = true;
            Text = "贴图";
            Bounds = new Rectangle(location, bmp.Size);
            ContextMenuStrip = BuildMenu();
        }

        protected override void WndProc(ref Message m)
        {
            if (m.Msg == 0x02E0) return; // 拖到别的屏幕时保持原始像素大小
            base.WndProc(ref m);
        }

        ContextMenuStrip BuildMenu()
        {
            var m = new ContextMenuStrip { Font = new Font(Theme.FontName, 9f) };
            m.Items.Add(new ToolStripMenuItem("复制", null, (s, e) => ClipboardImage.Set(image)) { ShortcutKeyDisplayString = "Ctrl+C" });
            m.Items.Add(new ToolStripMenuItem("另存为…", null, (s, e) => CaptureOverlay.SaveImage(image)) { ShortcutKeyDisplayString = "Ctrl+S" });
            m.Items.Add(new ToolStripMenuItem("识别文字 OCR", null, (s, e) => Ocr()));
            m.Items.Add(new ToolStripMenuItem("问 AI", null, (s, e) => Host.AskAiImage(image)));
            m.Items.Add(new ToolStripSeparator());
            m.Items.Add(new ToolStripMenuItem("原始大小", null, (s, e) => SetZoom(1f, new Point(0, 0))) { ShortcutKeyDisplayString = "1" });
            var top = new ToolStripMenuItem("置顶") { Checked = true };
            top.Click += (s, e) => { TopMost = !TopMost; top.Checked = TopMost; };
            m.Items.Add(top);
            m.Items.Add(new ToolStripSeparator());
            m.Items.Add(new ToolStripMenuItem("关闭", null, (s, e) => Close()) { ShortcutKeyDisplayString = "Esc" });
            m.Items.Add(new ToolStripMenuItem("关闭所有贴图", null, (s, e) => CloseAll()));
            return m;
        }

        void Ocr()
        {
            var ocr = Host.Get<IOcrService>();
            if (ocr == null) { Host.Notify("OCR 模块未启用", ""); return; }
            ocr.Recognize(new Bitmap(image), true);
        }

        void SetZoom(float z, Point anchorClient)
        {
            z = Math.Max(0.1f, Math.Min(8f, z));
            float rx = Width > 0 ? anchorClient.X / (float)Width : 0, ry = Height > 0 ? anchorClient.Y / (float)Height : 0;
            int nw = Math.Max(10, (int)(image.Width * z)), nh = Math.Max(10, (int)(image.Height * z));
            var anchorScreen = PointToScreen(anchorClient);
            zoom = z;
            Bounds = new Rectangle((int)(anchorScreen.X - rx * nw), (int)(anchorScreen.Y - ry * nh), nw, nh);
            Invalidate();
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            g.InterpolationMode = zoom >= 2f ? InterpolationMode.NearestNeighbor : zoom == 1f ? InterpolationMode.NearestNeighbor : InterpolationMode.HighQualityBicubic;
            g.PixelOffsetMode = PixelOffsetMode.Half;
            g.DrawImage(image, new Rectangle(0, 0, Width, Height));
            using (var pen = new Pen(Color.FromArgb(Focused || ContainsFocus ? 200 : 110, Theme.Accent), 1))
                g.DrawRectangle(pen, 0, 0, Width - 1, Height - 1);
            if (zoom != 1f)
            {
                string t = (int)Math.Round(zoom * 100) + "%";
                using (var f = new Font(Theme.FontName, 11, GraphicsUnit.Pixel))
                {
                    var sz = g.MeasureString(t, f);
                    using (var b = new SolidBrush(Color.FromArgb(160, 0, 0, 0))) g.FillRectangle(b, 2, 2, sz.Width + 6, sz.Height + 2);
                    g.DrawString(t, f, Brushes.White, 5, 3);
                }
            }
        }

        protected override void OnActivated(EventArgs e) { base.OnActivated(e); Invalidate(); }
        protected override void OnDeactivate(EventArgs e) { base.OnDeactivate(e); Invalidate(); }

        protected override void OnMouseDown(MouseEventArgs e)
        {
            base.OnMouseDown(e);
            if (e.Button == MouseButtons.Left) { dragging = true; dragOffset = e.Location; Cursor = Cursors.SizeAll; }
        }

        protected override void OnMouseMove(MouseEventArgs e)
        {
            base.OnMouseMove(e);
            if (dragging) { var p = Cursor.Position; Location = new Point(p.X - dragOffset.X, p.Y - dragOffset.Y); }
        }

        protected override void OnMouseUp(MouseEventArgs e)
        {
            base.OnMouseUp(e);
            dragging = false;
            Cursor = Cursors.Default;
        }

        protected override void OnMouseDoubleClick(MouseEventArgs e)
        {
            base.OnMouseDoubleClick(e);
            if (e.Button == MouseButtons.Left) Close();
        }

        protected override void OnMouseWheel(MouseEventArgs e)
        {
            base.OnMouseWheel(e);
            if ((ModifierKeys & Keys.Control) != 0)
                Opacity = Math.Max(0.2, Math.Min(1.0, Opacity + (e.Delta > 0 ? 0.1 : -0.1)));
            else
                SetZoom(zoom * (e.Delta > 0 ? 1.1f : 1 / 1.1f), e.Location);
        }

        protected override bool ProcessCmdKey(ref Message msg, Keys keyData)
        {
            switch (keyData)
            {
                case Keys.Escape: Close(); return true;
                case Keys.Control | Keys.C: ClipboardImage.Set(image); Host.Notify("已复制贴图", null); return true;
                case Keys.Control | Keys.S: CaptureOverlay.SaveImage(image); return true;
                case Keys.D1: case Keys.NumPad1: SetZoom(1f, new Point(0, 0)); return true;
                case Keys.Left: Left -= 1; return true;
                case Keys.Right: Left += 1; return true;
                case Keys.Up: Top -= 1; return true;
                case Keys.Down: Top += 1; return true;
            }
            return base.ProcessCmdKey(ref msg, keyData);
        }

        protected override void OnFormClosed(FormClosedEventArgs e)
        {
            base.OnFormClosed(e);
            open.Remove(this);
            image.Dispose();
        }
    }
}
