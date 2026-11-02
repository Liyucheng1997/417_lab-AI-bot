using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using System.Windows.Forms;
using MySOP.Capture;
using MySOP.Core;
using MySOP.Ocr;

namespace MySOP.Modules
{
    /// <summary>截图 OCR / 剪贴板 OCR（替代 Umi-OCR），热键与原来的 Umi-OCR 设置一致。</summary>
    public class OcrModule : IModule, IOcrService
    {
        public string Id { get { return "ocr"; } }
        public string Name { get { return "文字识别"; } }

        PaddleEngine engine;
        ConfigSection cfg;
        List<OcrBlock> lastBlocks;
        string lastText;

        public void Init(ModuleContext ctx)
        {
            cfg = ctx.Config;
            cfg.Default("engineDir", @"engine\PaddleOCR-json")
               .Default("language", "models/config_chinese.txt")
               .Default("layout", "multi_none")
               .Default("cpuThreads", 16)
               .Default("enableMkldnn", true)
               .Default("limitSideLen", 960)
               .Default("cls", false)
               .Default("idleExitSeconds", 600)
               .Default("copyResult", true)
               .Default("showResultWindow", false);

            string dir = cfg.Str("engineDir");
            if (!Path.IsPathRooted(dir)) dir = Path.Combine(Host.AppDir, dir);
            bool cls = cfg.Bool("cls");
            string args = "--config_path \"" + cfg.Str("language") + "\"" +
                          " --enable_mkldnn=" + (cfg.Bool("enableMkldnn") ? "true" : "false") +
                          " --cls=" + (cls ? "true" : "false") + " --use_angle_cls=" + (cls ? "true" : "false") +
                          " --limit_side_len " + cfg.Int("limitSideLen", 960) +
                          " --cpu_threads " + cfg.Int("cpuThreads", 16);
            engine = new PaddleEngine(dir, args, cfg.Int("idleExitSeconds", 600));
            if (!engine.Exists) Host.Notify("找不到 OCR 引擎", dir);
            Host.Provide<IOcrService>(this);

            ctx.Command("ocr", "截图识别文字", ScreenshotOcr);
            ctx.Command("ocr-clipboard", "识别剪贴板里的图片", ClipboardOcr);
            ctx.Command("ocr-last", "显示上次识别结果", ShowLast);

            var h1 = ctx.Hotkey("hotkeyScreenshot", "win+alt+c", "截图识别文字", ScreenshotOcr);
            var h2 = ctx.Hotkey("hotkeyClipboard", "win+alt+v", "识别剪贴板图片", ClipboardOcr);
            ctx.Menu("截图识字", ScreenshotOcr).ShortcutKeyDisplayString = h1.Combo;
            ctx.Menu("识别剪贴板图片", ClipboardOcr).ShortcutKeyDisplayString = h2.Combo;
            ctx.Menu("上次识别结果…", ShowLast);
        }

        void ScreenshotOcr()
        {
            Prewarm(); // 用户框选的同时启动引擎
            CaptureModule.Start(CaptureMode.Ocr);
        }

        void ClipboardOcr()
        {
            var bmp = ClipboardImage.Get();
            if (bmp == null) { Host.Notify("剪贴板里没有图片", "先复制一张图片（或图片文件）"); return; }
            Recognize(bmp, true);
        }

        public void Prewarm()
        {
            Task.Run(() => { try { engine.EnsureStarted(); } catch (Exception ex) { Host.Log("预热失败: " + ex.Message); } });
        }

        public void Recognize(Bitmap bmp, bool disposeAfter)
        {
            byte[] png;
            using (var ms = new MemoryStream())
            {
                bmp.Save(ms, ImageFormat.Png);
                png = ms.ToArray();
            }
            if (disposeAfter) bmp.Dispose();
            string layout = cfg.Str("layout", "multi_none");
            Task.Run(() =>
            {
                try
                {
                    var t0 = DateTime.Now;
                    var blocks = engine.Recognize(png);
                    string text = TextLayout.Format(blocks, layout);
                    double secs = (DateTime.Now - t0).TotalSeconds;
                    Host.OnUi(() => Deliver(blocks, text, secs));
                }
                catch (Exception ex)
                {
                    Host.Log("OCR 失败: " + ex);
                    Host.Notify("OCR 失败", ex.Message);
                }
            });
        }

        void Deliver(List<OcrBlock> blocks, string text, double secs)
        {
            lastBlocks = blocks;
            lastText = text;
            if (text.Length == 0) { Host.Notify("没有识别到文字", null); return; }
            if (cfg.Bool("copyResult", true)) SetClipboardText(text);
            if (cfg.Bool("showResultWindow", false)) { ShowLast(); return; }
            Host.Notify((cfg.Bool("copyResult", true) ? "已复制 " : "识别完成 ") + text.Length + " 字  ·  " + secs.ToString("0.0") + " 秒",
                        text + "\n\n（点这里查看 / 编辑）", ShowLast);
        }

        static void SetClipboardText(string text)
        {
            for (int i = 0; i < 5; i++)
            {
                try { Clipboard.SetText(text); return; }
                catch (System.Runtime.InteropServices.ExternalException) { System.Threading.Thread.Sleep(60); }
            }
        }

        void ShowLast()
        {
            if (lastBlocks == null) { Host.Notify("还没有识别记录", null); return; }
            OcrResultForm.ShowResult(lastBlocks, cfg);
        }

        public void Shutdown()
        {
            if (engine != null) engine.Dispose();
        }
    }

    /// <summary>识别结果窗口：可编辑、可切换排版方式。</summary>
    public class OcrResultForm : Form
    {
        static OcrResultForm instance;
        readonly TextBox box;
        readonly ComboBox layout;
        List<OcrBlock> blocks;

        public static void ShowResult(List<OcrBlock> blocks, ConfigSection cfg)
        {
            if (instance == null || instance.IsDisposed) instance = new OcrResultForm(cfg);
            instance.blocks = blocks;
            instance.Reformat();
            instance.Show();
            instance.WindowState = FormWindowState.Normal;
            instance.Activate();
        }

        OcrResultForm(ConfigSection cfg)
        {
            Text = "识别结果 — " + Host.AppName;
            Icon = IconFactory.Create(32);
            Font = new Font(Theme.FontName, 10f);
            Size = new Size(620, 460);
            StartPosition = FormStartPosition.CenterScreen;
            TopMost = true;

            box = new TextBox { Multiline = true, ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill, Font = new Font(Theme.FontName, 11f), BorderStyle = BorderStyle.None };
            var bar = new FlowLayoutPanel { Dock = DockStyle.Bottom, Height = 44, Padding = new Padding(6), FlowDirection = FlowDirection.RightToLeft };
            var close = new Button { Text = "关闭", AutoSize = true };
            close.Click += (s, e) => Close();
            var copy = new Button { Text = "复制全部", AutoSize = true };
            copy.Click += (s, e) => { if (box.Text.Length > 0) Clipboard.SetText(box.Text); Host.Notify("已复制", null); };
            var ai = new Button { Text = "发给 AI", AutoSize = true };
            ai.Click += (s, e) =>
            {
                string t = box.SelectionLength > 0 ? box.SelectedText : box.Text;
                if (t.Trim().Length == 0) return;
                Host.AskAiText(t.Replace("\r\n", "\n"));
                Close();
            };
            layout = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 200 };
            foreach (var m in TextLayout.Modes) layout.Items.Add(m[1]);
            int idx = Array.FindIndex(TextLayout.Modes, m => m[0] == cfg.Str("layout", "multi_none"));
            layout.SelectedIndex = Math.Max(0, idx);
            layout.SelectedIndexChanged += (s, e) =>
            {
                cfg.Set("layout", TextLayout.Modes[layout.SelectedIndex][0]);
                Host.Config.Save();
                Reformat();
            };
            var label = new Label { Text = "排版：", AutoSize = true, Padding = new Padding(0, 7, 0, 0) };
            bar.Controls.Add(close);
            bar.Controls.Add(copy);
            bar.Controls.Add(ai);
            bar.Controls.Add(layout);
            bar.Controls.Add(label);
            var host = new Panel { Dock = DockStyle.Fill, Padding = new Padding(10) , BackColor = SystemColors.Window };
            host.Controls.Add(box);
            Controls.Add(host);
            Controls.Add(bar);
            KeyPreview = true;
            KeyDown += (s, e) => { if (e.KeyCode == Keys.Escape) Close(); };
        }

        void Reformat()
        {
            box.Text = TextLayout.Format(blocks, TextLayout.Modes[Math.Max(0, layout.SelectedIndex)][0]).Replace("\n", "\r\n");
            box.SelectionStart = 0;
        }
    }
}
