using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Text;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using MySOP.Capture;
using MySOP.Core;
using MySOP.Modules;
using MySOP.Ocr;

namespace MySOP
{
    static class Program
    {
        /// <summary>内置模块清单。新增模块：写一个 IModule 类，然后加到这里。</summary>
        static IEnumerable<IModule> BuiltinModules()
        {
            yield return new CaptureModule();
            yield return new RecordModule();
            yield return new OcrModule();
            yield return new HotstringModule();
            yield return new WorkflowModule();
        }

        [STAThread]
        static int Main(string[] args)
        {
            try { if (!Native.SetProcessDpiAwarenessContext(Native.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)) Native.SetProcessDpiAwareness(2); }
            catch { try { Native.SetProcessDPIAware(); } catch { } }

            Host.AppDir = Path.GetDirectoryName(Application.ExecutablePath);
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            if (args.Length >= 2 && args[0] == "--make-icon") { IconFactory.SaveIco(args[1]); return 0; }
            if (args.Length >= 1 && args[0] == "--selftest") return SelfTest(args.Skip(1).ToArray());

            // --embedded：由 AI Bot 启动；--parent <pid>：Bot 退出时跟着退出
            var rest = new List<string>(args);
            Host.Embedded = rest.Remove("--embedded");
            int parentPid = 0;
            int pi = rest.IndexOf("--parent");
            if (pi >= 0 && pi + 1 < rest.Count) { int.TryParse(rest[pi + 1], out parentPid); rest.RemoveRange(pi, 2); }
            string command = rest.Count > 0 ? string.Join(" ", rest) : null;
            bool created;
            using (var mutex = new Mutex(true, "Local\\MySOP_SingleInstance", out created))
            {
                if (!created)
                {
                    // 已经在运行：把命令转发过去（没有命令就打开热键一览；Bot 重复拉起时什么都不做）
                    if (command != null || !Host.Embedded) HotkeyManager.SendToRunning(command ?? "hotkeys");
                    return 0;
                }
                Host.Config = new Config(Path.Combine(Host.AppDir, "config.json"));
                Application.ThreadException += (s, e) => { Host.Log("未处理异常: " + e.Exception); Host.Notify("出错了", e.Exception.Message); };
                AppDomain.CurrentDomain.UnhandledException += (s, e) => Host.Log("致命异常: " + e.ExceptionObject);
                var app = new AppHost(BuiltinModules);
                if (command != null) Host.Ui.BeginInvoke(new Action(() => Host.RunCommand(command)));
                if (parentPid > 0) WatchParent(parentPid);
                Application.Run(app);
            }
            return 0;
        }

        static void WatchParent(int pid)
        {
            Process parent;
            try { parent = Process.GetProcessById(pid); }
            catch (ArgumentException) { Host.RunCommand("quit"); return; }
            new Thread(() =>
            {
                parent.WaitForExit();
                Host.Log("AI Bot 已退出，SOP 跟着退出");
                Host.OnUi(() => Host.RunCommand("quit"));
            }) { IsBackground = true, Name = "parent-watch" }.Start();
        }

        // ---------------- 自检 ----------------
        static int SelfTest(string[] args)
        {
            var log = new StringBuilder();
            Action<string> p = s => { log.AppendLine(s); Console.WriteLine(s); };
            int fails = 0;
            Action<bool, string> check = (ok, what) => { p((ok ? "[通过] " : "[失败] ") + what); if (!ok) fails++; };
            Host.Config = new Config(Path.Combine(Path.GetTempPath(), "mysop_selftest_config.json"));

            // 1. 快捷短语
            string[] warn;
            var hsFile = Path.Combine(Host.AppDir, "hotstrings.txt");
            var hs = HotstringModule.Parse(File.ReadAllLines(hsFile, Encoding.UTF8), out warn);
            check(hs.Count >= 100, "解析 hotstrings.txt：" + hs.Count + " 条，警告 " + warn.Length);
            Func<string, string> sim = t => HotstringModule.Simulate(hs, t);
            check(sim("见文献r1 ") == "见文献r1 ", "“见文献r1 ” 前面是汉字（非结束符）时不触发，与 AHK 一致 → " + sim("见文献r1 "));
            check(sim("r1 ") == "[1] ", "r1+空格 → " + sim("r1 "));
            check(sim("see r12.") == "see [12].", "see r12. → " + sim("see r12."));
            check(sim("r99\n") == "[99]\n", "r99+回车 → [99]");
            check(sim("rr ") == "1. 思考快与慢。 ", "rr+空格 → " + sim("rr "));
            check(sim("rr1 ") == "rr1 ", "rr1 不误触发 → " + sim("rr1 "));
            check(sim("r100 ") == "r100 ", "r100 没有定义 → 不替换");
            check(sim("r2\bx ") == "r2\bx ".Replace("2\b", ""), "退格后不触发");
            var star = HotstringModule.Parse(new[] { ":*:;dd::完成`n下一步" }, out warn);
            check(HotstringModule.Simulate(star, "a ;dd") == "a 完成\n下一步", ":*: 立即替换 + `n 换行");

            // 2. 排版
            Func<int, int, int, int, string, OcrBlock> B = (x, y, w, h, t) => new OcrBlock { Text = t, Box = new Rectangle(x, y, w, h) };
            var twoCol = new List<OcrBlock>
            {
                B(10, 10, 300, 20, "左栏第一行"), B(400, 10, 300, 20, "右栏第一行"),
                B(10, 35, 300, 20, "左栏第二行"), B(400, 35, 300, 20, "右栏第二行"),
            };
            string multi = TextLayout.Format(twoCol, "multi_line");
            check(multi == "左栏第一行\n左栏第二行\n右栏第一行\n右栏第二行", "双栏 multi_line → " + multi.Replace("\n", " | "));
            string none = TextLayout.Format(new List<OcrBlock> { B(0, 0, 200, 20, "Hello"), B(0, 25, 200, 20, "world 你好"), B(0, 50, 200, 20, "世界") }, "multi_none");
            check(none == "Hello world 你好世界", "multi_none 中英混排 → " + none);

            // 3. OCR 引擎
            if (!args.Contains("--no-ocr"))
            {
                var engine = new PaddleEngine(Path.Combine(Host.AppDir, @"engine\PaddleOCR-json"),
                    "--config_path \"models/config_chinese.txt\" --enable_mkldnn=true --cls=false --use_angle_cls=false --limit_side_len 960 --cpu_threads 16", 0);
                try
                {
                    var sw = Stopwatch.StartNew();
                    engine.EnsureStarted();
                    p("引擎启动耗时 " + sw.ElapsedMilliseconds + " ms");
                    using (var bmp = new Bitmap(720, 160))
                    {
                        using (var g = Graphics.FromImage(bmp))
                        {
                            g.Clear(Color.White);
                            g.TextRenderingHint = TextRenderingHint.AntiAliasGridFit;
                            using (var f = new Font("Microsoft YaHei UI", 26, GraphicsUnit.Pixel))
                            {
                                g.DrawString("我的工作流软件 OCR 测试", f, Brushes.Black, 20, 20);
                                g.DrawString("Thinking, Fast and Slow [1]", f, Brushes.Black, 20, 90);
                            }
                        }
                        var ms = new MemoryStream();
                        bmp.Save(ms, System.Drawing.Imaging.ImageFormat.Png);
                        sw.Restart();
                        var blocks = engine.Recognize(ms.ToArray());
                        string text = TextLayout.Format(blocks, "multi_line");
                        p("识别耗时 " + sw.ElapsedMilliseconds + " ms，结果：" + text.Replace("\n", " | "));
                        check(text.Contains("工作流") && text.Contains("Fast"), "OCR 识别中英文");
                    }
                }
                catch (Exception ex) { check(false, "OCR 引擎：" + ex.Message); }
                finally { engine.Dispose(); }
            }

            // 4. 录屏：--record <输出.mp4> [秒数]（录主屏幕中间一块 + 电脑声音）
            int ri = Array.IndexOf(args, "--record");
            if (ri >= 0 && ri + 1 < args.Length)
            {
                int secs = 3;
                if (ri + 2 < args.Length) int.TryParse(args[ri + 2], out secs);
                var pb = Screen.PrimaryScreen.Bounds;
                var region = args.Contains("--full") ? pb : new Rectangle(pb.X + pb.Width / 4, pb.Y + pb.Height / 4, pb.Width / 2 + 1, pb.Height / 2 + 1);
                try
                {
                    var rec = new MySOP.Record.ScreenRecorder(region, Path.GetFullPath(args[ri + 1]), 30, !args.Contains("--no-audio"), true);
                    var done = new ManualResetEvent(false);
                    Exception recErr = null;
                    rec.Finished += (path, dur, err) => { recErr = err; p("录屏结束：" + dur.TotalSeconds.ToString("0.00") + " 秒，" + rec.Frames + " 帧"); done.Set(); };
                    var sw = Stopwatch.StartNew();
                    rec.Start();
                    p("录屏启动 " + sw.ElapsedMilliseconds + " ms，输出 " + rec.OutputSize.Width + "x" + rec.OutputSize.Height +
                      "，声音：" + (rec.HasAudio ? "有" : "无 " + rec.AudioError));
                    Thread.Sleep(secs * 1000);
                    if (args.Contains("--pause-test")) { rec.Paused = true; Thread.Sleep(1000); rec.Paused = false; Thread.Sleep(1000); }
                    sw.Restart();
                    rec.Stop();
                    done.WaitOne(30000);
                    p("收尾 " + sw.ElapsedMilliseconds + " ms");
                    check(recErr == null && File.Exists(rec.OutputPath) && new FileInfo(rec.OutputPath).Length > 1000,
                          "录屏 → " + rec.OutputPath + (recErr != null ? "：" + recErr.Message : ""));
                }
                catch (Exception ex) { check(false, "录屏：" + ex); }
            }

            // 5. 截图遮罩渲染（不显示窗口，渲染成图片供检查）
            int pi = Array.IndexOf(args, "--preview");
            if (pi >= 0 && pi + 1 < args.Length)
            {
                using (var ov = new CaptureOverlay(CaptureMode.Normal))
                using (var img = ov.RenderPreview(new Rectangle(300, 200, 520, 300), 1))
                    img.Save(args[pi + 1]);
                p("遮罩预览已保存：" + args[pi + 1]);
            }

            p(fails == 0 ? "全部通过" : fails + " 项失败");
            File.WriteAllText(Path.Combine(Host.AppDir, "logs_selftest.txt"), log.ToString(), Encoding.UTF8);
            return fails;
        }
    }
}
