using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using MySOP.Core;

namespace MySOP.Ocr
{
    public class OcrBlock
    {
        public string Text;
        public float Score;
        public System.Drawing.Rectangle Box;
    }

    /// <summary>
    /// PaddleOCR-json 引擎（与 Umi-OCR 同一个引擎）的管道封装。
    /// 第一次识别时才启动；空闲 idleSeconds 秒后自动退出以释放内存。
    /// </summary>
    public class PaddleEngine : IDisposable
    {
        readonly string exePath, arguments;
        readonly int idleSeconds;
        readonly object gate = new object();
        Process proc;
        Timer idleTimer;

        public PaddleEngine(string engineDir, string arguments, int idleSeconds)
        {
            exePath = Path.Combine(engineDir, "PaddleOCR-json.exe");
            this.arguments = arguments;
            this.idleSeconds = idleSeconds;
        }

        public bool Exists { get { return File.Exists(exePath); } }

        public void EnsureStarted()
        {
            lock (gate)
            {
                StopIdleTimer();
                if (proc != null && !proc.HasExited) return;
                if (!Exists) throw new FileNotFoundException("找不到 OCR 引擎：" + exePath);
                var psi = new ProcessStartInfo(exePath, arguments)
                {
                    WorkingDirectory = Path.GetDirectoryName(exePath),
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardInput = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    StandardOutputEncoding = Encoding.UTF8,
                };
                var p = Process.Start(psi);
                p.ErrorDataReceived += (s, e) => { };
                p.BeginErrorReadLine();
                var init = Task.Run(() =>
                {
                    string line;
                    while ((line = p.StandardOutput.ReadLine()) != null)
                        if (line.Contains("OCR init completed.")) return true;
                    return false;
                });
                if (!init.Wait(TimeSpan.FromSeconds(60)) || !init.Result)
                {
                    try { p.Kill(); } catch { }
                    throw new Exception("OCR 引擎启动失败");
                }
                proc = p;
                Host.Log("OCR 引擎已启动 pid=" + p.Id);
                ArmIdleTimer();
            }
        }

        public List<OcrBlock> Recognize(byte[] imageBytes)
        {
            lock (gate)
            {
                EnsureStarted();
                StopIdleTimer();
                try
                {
                    proc.StandardInput.WriteLine("{\"image_base64\":\"" + Convert.ToBase64String(imageBytes) + "\"}");
                    proc.StandardInput.Flush();
                    string line;
                    do { line = proc.StandardOutput.ReadLine(); }
                    while (line != null && !line.TrimStart().StartsWith("{"));
                    if (line == null) { Kill(); throw new Exception("OCR 引擎意外退出"); }
                    return Parse(line);
                }
                catch (IOException) { Kill(); throw new Exception("OCR 引擎通信失败，下次会自动重启"); }
                finally { ArmIdleTimer(); }
            }
        }

        static List<OcrBlock> Parse(string json)
        {
            var result = new List<OcrBlock>();
            var js = new JavaScriptSerializer { MaxJsonLength = int.MaxValue };
            var d = js.DeserializeObject(json) as Dictionary<string, object>;
            int code = Convert.ToInt32(d["code"]);
            if (code == 101) return result; // 没有文字
            if (code != 100) throw new Exception("OCR 错误 " + code + "：" + d["data"]);
            foreach (var item in (object[])d["data"])
            {
                var b = (Dictionary<string, object>)item;
                int x1 = int.MaxValue, y1 = int.MaxValue, x2 = int.MinValue, y2 = int.MinValue;
                foreach (var pt in (object[])b["box"])
                {
                    var xy = (object[])pt;
                    int x = Convert.ToInt32(xy[0]), y = Convert.ToInt32(xy[1]);
                    x1 = Math.Min(x1, x); y1 = Math.Min(y1, y); x2 = Math.Max(x2, x); y2 = Math.Max(y2, y);
                }
                result.Add(new OcrBlock
                {
                    Text = Convert.ToString(b["text"]),
                    Score = Convert.ToSingle(b["score"]),
                    Box = System.Drawing.Rectangle.FromLTRB(x1, y1, Math.Max(x2, x1 + 1), Math.Max(y2, y1 + 1))
                });
            }
            return result;
        }

        void ArmIdleTimer()
        {
            if (idleSeconds <= 0) return;
            StopIdleTimer();
            idleTimer = new Timer(_ => { lock (gate) { Host.Log("OCR 引擎空闲退出"); Kill(); } }, null, idleSeconds * 1000, Timeout.Infinite);
        }

        void StopIdleTimer()
        {
            if (idleTimer != null) { idleTimer.Dispose(); idleTimer = null; }
        }

        void Kill()
        {
            if (proc == null) return;
            try { if (!proc.HasExited) proc.Kill(); } catch { }
            proc.Dispose();
            proc = null;
        }

        public void Dispose()
        {
            lock (gate) { StopIdleTimer(); Kill(); }
        }
    }
}
