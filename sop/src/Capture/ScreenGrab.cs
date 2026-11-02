using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.Text;
using System.Windows.Forms;
using MySOP.Core;

namespace MySOP.Capture
{
    /// <summary>截全屏（所有显示器，物理像素）+ 收集可吸附的窗口矩形。</summary>
    public static class ScreenGrab
    {
        public static Rectangle VirtualBounds { get { return SystemInformation.VirtualScreen; } }

        public static Bitmap CaptureVirtualScreen(out Rectangle bounds)
        {
            bounds = VirtualBounds;
            var bmp = new Bitmap(bounds.Width, bounds.Height, PixelFormat.Format32bppPArgb);
            using (var g = Graphics.FromImage(bmp))
                g.CopyFromScreen(bounds.X, bounds.Y, 0, 0, bounds.Size, CopyPixelOperation.SourceCopy);
            return bmp;
        }

        /// <summary>按 Z 序（最上面的在前）返回可见顶层窗口的矩形，最后追加每个显示器的矩形。</summary>
        public static List<Rectangle> WindowRects()
        {
            var list = new List<Rectangle>();
            var vs = VirtualBounds;
            var cls = new StringBuilder(64);
            Native.EnumWindows((h, l) =>
            {
                if (!Native.IsWindowVisible(h) || Native.IsIconic(h)) return true;
                int cloaked;
                if (Native.DwmGetWindowAttribute(h, Native.DWMWA_CLOAKED, out cloaked, 4) == 0 && cloaked != 0) return true;
                cls.Length = 0;
                Native.GetClassName(h, cls, 64);
                string c = cls.ToString();
                if (c == "Progman" || c == "WorkerW") return true; // 桌面，用显示器矩形代替
                Native.RECT r;
                if (Native.DwmGetWindowAttribute(h, Native.DWMWA_EXTENDED_FRAME_BOUNDS, out r, 16) != 0 && !Native.GetWindowRect(h, out r)) return true;
                var rect = Rectangle.Intersect(r.ToRectangle(), vs);
                if (rect.Width < 8 || rect.Height < 8) return true;
                list.Add(rect);
                return true;
            }, IntPtr.Zero);
            foreach (var s in Screen.AllScreens) list.Add(s.Bounds);
            return list;
        }
    }
}
