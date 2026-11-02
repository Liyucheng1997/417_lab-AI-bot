using System;
using System.Windows.Forms;
using MySOP.Capture;
using MySOP.Core;

namespace MySOP.Modules
{
    /// <summary>截图 + 贴图（替代 Snipaste），热键与 Snipaste 默认一致。</summary>
    public class CaptureModule : IModule
    {
        public string Id { get { return "capture"; } }
        public string Name { get { return "截图贴图"; } }

        static CaptureOverlay active;

        public void Init(ModuleContext ctx)
        {
            ctx.Config.Default("windowDetect", true).Default("magnifier", true).Default("saveFolder", "");

            ctx.Command("capture", "截图", () => Start(CaptureMode.Normal));
            ctx.Command("pin", "把剪贴板贴到屏幕", PinWindow.FromClipboard);
            ctx.Command("pins-toggle", "隐藏/显示所有贴图", PinWindow.ToggleAll);
            ctx.Command("pins-close", "关闭所有贴图", PinWindow.CloseAll);

            var h1 = ctx.Hotkey("hotkeySnip", "F1", "截图", () => Start(CaptureMode.Normal));
            var h2 = ctx.Hotkey("hotkeyPin", "F3", "贴图（剪贴板）", PinWindow.FromClipboard);
            var h3 = ctx.Hotkey("hotkeyTogglePins", "shift+F3", "隐藏/显示所有贴图", PinWindow.ToggleAll);

            ctx.Menu("截图", () => Start(CaptureMode.Normal)).ShortcutKeyDisplayString = h1.Combo;
            ctx.Menu("贴图（剪贴板）", PinWindow.FromClipboard).ShortcutKeyDisplayString = h2.Combo;
            ctx.Menu("隐藏/显示所有贴图", PinWindow.ToggleAll).ShortcutKeyDisplayString = h3.Combo;
            ctx.Menu("关闭所有贴图", PinWindow.CloseAll);
        }

        /// <summary>打开截图遮罩。OCR 模块也用它（mode = Ocr）。</summary>
        public static void Start(CaptureMode mode)
        {
            if (active != null && !active.IsDisposed) { active.Activate(); return; }
            var sw = System.Diagnostics.Stopwatch.StartNew();
            active = new CaptureOverlay(mode);
            active.FormClosed += (s, e) => { active = null; GC.Collect(); };
            active.Show();
            Host.Log("截图遮罩就绪 " + sw.ElapsedMilliseconds + " ms");
        }

        public void Shutdown() { }
    }
}
