using System;
using System.Collections.Specialized;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Threading;
using System.Windows.Forms;
using MySOP.Capture;
using MySOP.Core;
using MySOP.Record;

namespace MySOP.Modules
{
    /// <summary>
    /// 录屏：Ctrl+F1 框选区域（和截图一样能吸附窗口、拖手柄调整）→ 开始录制 → 再按 Ctrl+F1 或点控制条的「停止」。
    /// 存成 MP4（H.264 + AAC），默认放在「视频\录屏」，并复制到剪贴板，可以直接粘贴发送。
    /// </summary>
    public class RecordModule : IModule
    {
        public string Id { get { return "record"; } }
        public string Name { get { return "录屏"; } }

        static CaptureOverlay picking;
        static ScreenRecorder rec;
        static RecordChrome chrome;
        static ConfigSection cfg;

        public void Init(ModuleContext ctx)
        {
            cfg = ctx.Config;
            cfg.Default("audio", true).Default("fps", 30).Default("cursor", true)
               .Default("saveFolder", "").Default("copyToClipboard", true);

            ctx.Command("record", "录屏（录制中再执行 = 停止）", Toggle);
            ctx.Command("record-stop", "停止录屏", Stop);
            var h = ctx.Hotkey("hotkeyRecord", "ctrl+F1", "录屏（再按一次停止）", Toggle);
            ctx.Menu("录屏", Toggle).ShortcutKeyDisplayString = h.Combo;
            ctx.Command("record-folder", "打开录屏文件夹", OpenFolder);
            ctx.Menu("打开录屏文件夹", OpenFolder);
        }

        public static bool Recording { get { return rec != null; } }

        static void Toggle()
        {
            if (rec != null) { Stop(); return; }
            if (picking != null && !picking.IsDisposed) { picking.Activate(); return; }
            picking = new CaptureOverlay(CaptureMode.Record);
            picking.RecordRequested += Begin;
            picking.FormClosed += (s, e) => { picking = null; GC.Collect(); };
            picking.Show();
        }

        static void OpenFolder() { Process.Start("explorer.exe", "\"" + SaveFolder() + "\""); }

        static string SaveFolder()
        {
            string folder = cfg.Str("saveFolder");
            if (string.IsNullOrEmpty(folder))
                folder = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyVideos), "录屏");
            Directory.CreateDirectory(folder);
            return folder;
        }

        static void Begin(Rectangle region)
        {
            if (rec != null) return;
            bool audio = cfg.Bool("audio", true);
            int fps = cfg.Int("fps", 30);
            string path = Path.Combine(SaveFolder(), "录屏_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".mp4");

            var r = new ScreenRecorder(region, path, fps, audio, cfg.Bool("cursor", true));
            rec = r;
            r.Finished += (p, dur, err) => Host.OnUi(() => OnFinished(r, p, dur, err));
            chrome = new RecordChrome(region, r, audio, Stop);

            // 准备编码器要大半秒，放到后台，界面先出来（显示“准备中”）；遮罩此时已经隐藏，不会被录进去
            ThreadPool.QueueUserWorkItem(_ =>
            {
                try
                {
                    Thread.Sleep(120);
                    r.Start();
                    Host.OnUi(() =>
                    {
                        if (chrome != null && rec == r) chrome.SetRunning();
                        if (audio && !r.HasAudio) Host.Notify("没录到电脑声音", (r.AudioError ?? "") + "\n这次只录画面。");
                    });
                }
                catch (Exception ex)
                {
                    Host.Log("录屏启动失败: " + ex);
                    Host.OnUi(() =>
                    {
                        CloseChrome();
                        if (rec == r) rec = null;
                        Host.Notify("录屏启动失败", ex.Message);
                    });
                }
            });
        }

        static void Stop()
        {
            if (rec == null) return;
            if (chrome != null) chrome.SetSaving();
            rec.Stop();
        }

        static void CloseChrome()
        {
            if (chrome != null) { chrome.Dispose(); chrome = null; }
        }

        static void OnFinished(ScreenRecorder r, string path, TimeSpan duration, Exception err)
        {
            if (rec == r) { rec = null; CloseChrome(); }
            if (err != null)
            {
                if (r.Frames == 0) Host.Notify("录屏已取消", null);
                else Host.Notify("录屏失败", err.Message);
                return;
            }
            string size = (new FileInfo(path).Length / 1048576.0).ToString("0.0") + " MB";
            string len = duration.TotalHours >= 1 ? duration.ToString(@"h\:mm\:ss") : duration.ToString(@"mm\:ss");
            bool copied = false;
            if (cfg.Bool("copyToClipboard", true))
            {
                for (int i = 0; i < 5 && !copied; i++)
                {
                    try { Clipboard.SetFileDropList(new StringCollection { path }); copied = true; }
                    catch (System.Runtime.InteropServices.ExternalException) { Thread.Sleep(60); }
                }
            }
            Host.Log("录屏已保存 " + path + " " + len + " " + size);
            Host.Notify("录屏已保存", len + " · " + size + (copied ? " · 已复制，可直接粘贴发送" : "") + "\n点这里打开所在文件夹",
                        () => Process.Start("explorer.exe", "/select,\"" + path + "\""));
        }

        public void Shutdown()
        {
            // 退出 / 重新加载时正在录：先把文件收好尾，不然 MP4 打不开
            var r = rec;
            if (r == null) return;
            rec = null;
            CloseChrome();
            r.StopAndWait(15000);
        }
    }
}
