using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Text;
using System.IO;
using System.Linq;
using System.Windows.Forms;

namespace MySOP.Core
{
    public static class Theme
    {
        public static readonly Color Accent = Color.FromArgb(0, 122, 255);
        public static readonly Color Text = Color.FromArgb(29, 29, 31);
        public static readonly Color SubText = Color.FromArgb(110, 110, 115);
        public static readonly Color Border = Color.FromArgb(210, 210, 215);
        public const string FontName = "Microsoft YaHei UI";

        public static GraphicsPath RoundRect(RectangleF r, float radius)
        {
            var p = new GraphicsPath();
            float d = radius * 2;
            if (d <= 0) { p.AddRectangle(r); return p; }
            p.AddArc(r.X, r.Y, d, d, 180, 90);
            p.AddArc(r.Right - d, r.Y, d, d, 270, 90);
            p.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
            p.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
            p.CloseFigure();
            return p;
        }
    }

    /// <summary>程序图标：蓝底圆角方块 + 白色 “S”，运行时绘制，不依赖资源文件。</summary>
    public static class IconFactory
    {
        public static Bitmap Draw(int size)
        {
            var bmp = new Bitmap(size, size);
            using (var g = Graphics.FromImage(bmp))
            {
                g.SmoothingMode = SmoothingMode.AntiAlias;
                g.TextRenderingHint = TextRenderingHint.AntiAliasGridFit;
                float pad = size * 0.04f;
                using (var path = Theme.RoundRect(new RectangleF(pad, pad, size - 2 * pad, size - 2 * pad), size * 0.22f))
                using (var b = new SolidBrush(Theme.Accent))
                    g.FillPath(b, path);
                using (var f = new Font("Segoe UI", size * 0.62f, FontStyle.Bold, GraphicsUnit.Pixel))
                using (var sf = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center })
                    g.DrawString("S", f, Brushes.White, new RectangleF(0, size * 0.02f, size, size), sf);
            }
            return bmp;
        }

        public static Icon Create(int size)
        {
            using (var bmp = Draw(size)) return Icon.FromHandle(bmp.GetHicon());
        }

        /// <summary>写出多尺寸 .ico（PNG 压缩），供编译 exe 图标使用。</summary>
        public static void SaveIco(string path)
        {
            int[] sizes = { 16, 24, 32, 48, 64, 128, 256 };
            var pngs = sizes.Select(s =>
            {
                using (var bmp = Draw(s))
                using (var ms = new MemoryStream()) { bmp.Save(ms, System.Drawing.Imaging.ImageFormat.Png); return ms.ToArray(); }
            }).ToArray();
            using (var w = new BinaryWriter(File.Create(path)))
            {
                w.Write((short)0); w.Write((short)1); w.Write((short)sizes.Length);
                int offset = 6 + 16 * sizes.Length;
                for (int i = 0; i < sizes.Length; i++)
                {
                    w.Write((byte)(sizes[i] >= 256 ? 0 : sizes[i]));
                    w.Write((byte)(sizes[i] >= 256 ? 0 : sizes[i]));
                    w.Write((byte)0); w.Write((byte)0);
                    w.Write((short)1); w.Write((short)32);
                    w.Write(pngs[i].Length); w.Write(offset);
                    offset += pngs[i].Length;
                }
                foreach (var p in pngs) w.Write(p);
            }
        }
    }

    /// <summary>右下角的轻提示，不抢焦点，几秒后自动消失；鼠标悬停时不消失。</summary>
    public class Toast : Form
    {
        static Toast current;
        readonly string title, body;
        readonly Action onClick;
        readonly Timer timer = new Timer();
        readonly float scale;
        Font titleFont, bodyFont;

        public static void Show(string title, string body, Action onClick)
        {
            if (current != null && !current.IsDisposed) current.Close();
            current = new Toast(title, body, onClick);
            current.Show();
        }

        Toast(string title, string body, Action onClick)
        {
            this.title = title; this.body = body ?? ""; this.onClick = onClick;
            scale = Native.ScaleAt(Cursor.Position);
            FormBorderStyle = FormBorderStyle.None;
            ShowInTaskbar = false;
            TopMost = true;
            StartPosition = FormStartPosition.Manual;
            BackColor = Color.White;
            DoubleBuffered = true;
            Cursor = onClick != null ? Cursors.Hand : Cursors.Default;
            titleFont = new Font(Theme.FontName, 14 * scale, FontStyle.Bold, GraphicsUnit.Pixel);
            bodyFont = new Font(Theme.FontName, 13 * scale, GraphicsUnit.Pixel);

            int w = (int)(340 * scale), pad = (int)(14 * scale);
            Size bodySize;
            using (var g = CreateGraphics())
                bodySize = TextRenderer.MeasureText(g, Truncate(this.body), bodyFont, new Size(w - 2 * pad, 10000), TextFormatFlags.WordBreak);
            int h = pad + titleFont.Height + (this.body.Length > 0 ? (int)(6 * scale) + Math.Min(bodySize.Height, (int)(120 * scale)) : 0) + pad;
            var area = Screen.FromPoint(Cursor.Position).WorkingArea;
            Bounds = new Rectangle(area.Right - w - (int)(16 * scale), area.Bottom - h - (int)(16 * scale), w, h);

            timer.Interval = onClick != null ? 4500 : 3000;
            timer.Tick += (s, e) => { if (!Bounds.Contains(Cursor.Position)) Close(); };
            timer.Start();
            MouseClick += (s, e) =>
            {
                Close();
                if (e.Button == MouseButtons.Left && this.onClick != null) this.onClick();
            };
        }

        static string Truncate(string s) { return s.Length > 300 ? s.Substring(0, 300) + "…" : s; }

        protected override bool ShowWithoutActivation { get { return true; } }

        protected override CreateParams CreateParams
        {
            get
            {
                var cp = base.CreateParams;
                cp.ExStyle |= Native.WS_EX_TOOLWINDOW | Native.WS_EX_NOACTIVATE | 0x8 /*TOPMOST*/;
                return cp;
            }
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            int pad = (int)(14 * scale);
            using (var pen = new Pen(Theme.Border)) g.DrawRectangle(pen, 0, 0, Width - 1, Height - 1);
            using (var b = new SolidBrush(Theme.Accent)) g.FillRectangle(b, 0, 0, (int)(4 * scale), Height);
            TextRenderer.DrawText(g, title, titleFont, new Point(pad, pad), Theme.Text);
            if (body.Length > 0)
            {
                var r = new Rectangle(pad, pad + titleFont.Height + (int)(6 * scale), Width - 2 * pad, Height - pad * 2 - titleFont.Height);
                TextRenderer.DrawText(g, Truncate(body), bodyFont, r, Theme.SubText, TextFormatFlags.WordBreak | TextFormatFlags.EndEllipsis);
            }
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing) { timer.Dispose(); titleFont.Dispose(); bodyFont.Dispose(); }
            base.Dispose(disposing);
        }
    }

    /// <summary>热键一览 + 命令一览。</summary>
    public class HotkeyListForm : Form
    {
        static HotkeyListForm instance;

        public static void ShowList()
        {
            if (instance == null || instance.IsDisposed) instance = new HotkeyListForm();
            instance.Show();
            instance.Activate();
        }

        HotkeyListForm()
        {
            Text = Host.AppName + " — 热键与命令";
            Icon = IconFactory.Create(32);
            Font = new Font(Theme.FontName, 9.5f);
            Size = new Size(760, 520);
            StartPosition = FormStartPosition.CenterScreen;

            var lv = new ListView { Dock = DockStyle.Fill, View = View.Details, FullRowSelect = true, GridLines = false };
            lv.Columns.Add("模块", 110);
            lv.Columns.Add("热键 / 命令", 170);
            lv.Columns.Add("功能", 240);
            lv.Columns.Add("状态", 200);
            var gHot = new ListViewGroup("全局热键");
            var gCmd = new ListViewGroup("命令（可在工作流里用 \"command\"，或命令行 SOP.exe 命令名）");
            lv.Groups.Add(gHot); lv.Groups.Add(gCmd);
            foreach (var h in Host.Hotkeys.All)
            {
                var it = new ListViewItem(new[] { h.Module, h.Combo, h.Description, h.Ok ? "✓ 正常" : h.Error }, gHot);
                if (!h.Ok && h.Error != "未设置") it.ForeColor = Color.Firebrick;
                lv.Items.Add(it);
            }
            foreach (var c in Host.Commands.Values.OrderBy(c => c.Module))
                lv.Items.Add(new ListViewItem(new[] { c.Module, c.Name, c.Description, "" }, gCmd));

            var tip = new Label
            {
                Dock = DockStyle.Bottom, Height = 44, Padding = new Padding(8, 6, 8, 6),
                ForeColor = Theme.SubText,
                Text = "修改热键：托盘右键 → 设置 → 编辑配置 config.json，保存后点“重新加载配置”。\n短语（原 AHK 的 r1→[1]）在 hotstrings.txt 里，保存后自动生效。"
            };
            Controls.Add(lv);
            Controls.Add(tip);
        }
    }
}
