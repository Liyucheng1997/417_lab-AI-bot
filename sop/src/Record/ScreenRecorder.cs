using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
using MySOP.Core;

namespace MySOP.Record
{
    /// <summary>
    /// 录屏引擎：画面线程按帧率截屏（GDI，带鼠标指针）→ H.264；声音线程录电脑声音 → AAC；一起写进 MP4。
    /// 时间戳都按同一个“录制时钟”（去掉暂停的时间）算，音画同步。
    /// 设了 WDA_EXCLUDEFROMCAPTURE 的窗口（录屏框、控制条）不会被录进去。
    /// </summary>
    public sealed class ScreenRecorder
    {
        public readonly Rectangle Region;   // 屏幕坐标（物理像素）
        public readonly string OutputPath;
        public readonly int Fps;
        public readonly bool DrawCursor;
        readonly bool wantAudio;
        int outW, outH;

        Mp4Writer writer;
        LoopbackAudio loopback;
        Thread videoThread, audioThread;
        volatile bool stopping;
        Exception startError;
        readonly ManualResetEvent started = new ManualResetEvent(false);

        // 录制时钟
        readonly Stopwatch clock = new Stopwatch();
        readonly object clockLock = new object();
        long pausedTicks, pauseStart = -1;

        /// <summary>录制过程中关掉/打开电脑声音（关掉时写静音）。</summary>
        public volatile bool Muted;
        public bool HasAudio { get { return writer != null && writer.HasAudio; } }
        /// <summary>声音没录上的原因（比如没有播放设备）；画面照常录。</summary>
        public string AudioError { get; private set; }
        public long Frames { get; private set; }

        /// <summary>结束（成功或失败）时在工作线程上触发：(文件路径, 时长, 错误)。</summary>
        public event Action<string, TimeSpan, Exception> Finished;

        public ScreenRecorder(Rectangle region, string outputPath, int fps, bool audio, bool cursor)
        {
            // H.264 要求宽高是偶数
            Region = new Rectangle(region.X, region.Y, region.Width & ~1, region.Height & ~1);
            OutputPath = outputPath;
            Fps = Math.Max(5, Math.Min(60, fps));
            wantAudio = audio;
            DrawCursor = cursor;
            // 太大的区域（多屏拼起来）缩到 H.264 能编的尺寸：最多 36864 个宏块，单边不超过 4096
            double s = Math.Min(1.0, Math.Min(Math.Sqrt(36864.0 * 256 / ((double)Region.Width * Region.Height)),
                                              Math.Min(4096.0 / Region.Width, 4096.0 / Region.Height)));
            outW = Math.Max(2, (int)(Region.Width * s) & ~1);
            outH = Math.Max(2, (int)(Region.Height * s) & ~1);
        }

        public Size OutputSize { get { return new Size(outW, outH); } }

        public TimeSpan Elapsed { get { return TimeSpan.FromTicks(RecordedTicks()); } }

        /// <summary>录制时钟（100 纳秒），不含暂停的时间。</summary>
        long RecordedTicks()
        {
            lock (clockLock)
            {
                long now = clock.Elapsed.Ticks;
                return now - pausedTicks - (pauseStart >= 0 ? now - pauseStart : 0);
            }
        }

        public bool Paused
        {
            get { lock (clockLock) return pauseStart >= 0; }
            set
            {
                lock (clockLock)
                {
                    if (value && pauseStart < 0) pauseStart = clock.Elapsed.Ticks;
                    else if (!value && pauseStart >= 0) { pausedTicks += clock.Elapsed.Ticks - pauseStart; pauseStart = -1; }
                }
            }
        }

        /// <summary>开始录制；准备失败（比如编码器打不开）直接抛异常。</summary>
        public void Start()
        {
            Directory.CreateDirectory(Path.GetDirectoryName(OutputPath));
            videoThread = new Thread(VideoLoop) { IsBackground = true, Name = "record-video" };
            videoThread.SetApartmentState(ApartmentState.MTA);
            videoThread.Start();
            if (!started.WaitOne(15000)) throw new TimeoutException("录屏启动超时");
            if (startError != null) throw startError;
        }

        public void Stop() { stopping = true; }

        /// <summary>停止并等收尾写完（退出程序时用）。</summary>
        public bool StopAndWait(int timeoutMs)
        {
            stopping = true;
            var t = videoThread;
            return t == null || t.Join(timeoutMs);
        }

        // ---------- 画面线程（也负责创建和收尾写入器） ----------
        void VideoLoop()
        {
            IntPtr screenDc = IntPtr.Zero, memDc = IntPtr.Zero, dib = IntPtr.Zero, oldObj = IntPtr.Zero;
            DesktopDuplication gpu = null;
            Exception error = null;
            try
            {
                if (wantAudio)
                {
                    try { loopback = new LoopbackAudio(); }
                    catch (Exception ex) { AudioError = ex.Message; Host.Log("录电脑声音失败，只录画面: " + ex); loopback = null; }
                }
                int bitrate = (int)Math.Max(2000000, Math.Min(24000000, (long)outW * outH * Fps / 8));
                writer = new Mp4Writer(OutputPath, outW, outH, Fps, bitrate,
                                       loopback != null ? loopback.SampleRate : 0, LoopbackAudio.Channels);

                screenDc = GetDC(IntPtr.Zero);
                memDc = CreateCompatibleDC(screenDc);
                var bi = new BITMAPINFOHEADER { biSize = 40, biWidth = outW, biHeight = -outH, biPlanes = 1, biBitCount = 32 };
                IntPtr bits;
                dib = CreateDIBSection(memDc, ref bi, 0, out bits, IntPtr.Zero, 0);
                if (dib == IntPtr.Zero) throw new OutOfMemoryException("创建画面缓冲失败");
                oldObj = SelectObject(memDc, dib);
                bool scaled = outW != Region.Width || outH != Region.Height;
                if (scaled) SetStretchBltMode(memDc, 4 /*HALFTONE*/);
                else gpu = DesktopDuplication.TryCreate(Region);

                timeBeginPeriod(1);
                clock.Start();
                if (loopback != null)
                {
                    loopback.Start();
                    audioThread = new Thread(AudioLoop) { IsBackground = true, Name = "record-audio" };
                    audioThread.SetApartmentState(ApartmentState.MTA);
                    audioThread.Start();
                }
                started.Set();
                Host.Log("开始录屏 " + Region + " → " + outW + "x" + outH + " @" + Fps + "fps " + (bitrate / 1000) + "kbps，声音：" +
                         (loopback != null ? loopback.SampleRate + "Hz" : (wantAudio ? "失败" : "关")) + "，截屏：" + (gpu != null ? "显卡" : "GDI"));

                long frameTicks = 10000000L / Fps;
                long last = -1;
                var perf = new Stopwatch(); long blitTicks = 0, writeTicks = 0;
                long due = 0; // 下一帧该拍的时间
                while (!stopping)
                {
                    if (Paused) { Thread.Sleep(15); continue; }
                    long t = RecordedTicks();
                    if (t < due) { Thread.Sleep((int)Math.Max(1, Math.Min(50, (due - t) / 10000))); continue; }
                    due = (t / frameTicks + 1) * frameTicks;
                    perf.Restart();
                    bool grabbed = false;
                    if (gpu != null)
                    {
                        try { grabbed = gpu.Grab(bits, outW * 4); }
                        catch (Exception ex) { Host.Log("显卡截屏失效，改用 GDI: " + ex.Message); gpu.Dispose(); gpu = null; }
                    }
                    if (grabbed) { }
                    else if (scaled) StretchBlt(memDc, 0, 0, outW, outH, screenDc, Region.X, Region.Y, Region.Width, Region.Height, SRCCOPY);
                    else BitBlt(memDc, 0, 0, outW, outH, screenDc, Region.X, Region.Y, SRCCOPY);
                    if (DrawCursor) DrawCursorOn(memDc, scaled);
                    GdiFlush();
                    blitTicks += perf.ElapsedTicks; perf.Restart();
                    if (t <= last) t = last + 1;
                    writer.WriteVideo(bits, outW * 4, t, frameTicks);
                    writeTicks += perf.ElapsedTicks;
                    last = t;
                    Frames++;
                }
                if (Frames > 0) Host.Log("录屏每帧平均：截屏 " + (blitTicks * 1000.0 / Stopwatch.Frequency / Frames).ToString("0.0") + " ms，编码 " + (writeTicks * 1000.0 / Stopwatch.Frequency / Frames).ToString("0.0") + " ms");
            }
            catch (Exception ex)
            {
                error = ex;
                Host.Log("录屏出错: " + ex);
                if (!started.WaitOne(0)) { startError = ex; started.Set(); }
            }
            finally
            {
                stopping = true;
                if (audioThread != null) audioThread.Join(3000);
                if (loopback != null) loopback.Dispose();
                if (gpu != null) gpu.Dispose();
                timeEndPeriod(1);
                if (oldObj != IntPtr.Zero) SelectObject(memDc, oldObj);
                if (dib != IntPtr.Zero) DeleteObject(dib);
                if (memDc != IntPtr.Zero) DeleteDC(memDc);
                if (screenDc != IntPtr.Zero) ReleaseDC(IntPtr.Zero, screenDc);
            }

            var duration = Elapsed;
            if (writer != null)
            {
                try
                {
                    if (error == null && Frames > 0) writer.Finish();
                    else writer.Dispose();
                }
                catch (Exception ex) { if (error == null) error = ex; Host.Log("录屏收尾出错: " + ex); }
            }
            if (error != null || Frames == 0) { try { File.Delete(OutputPath); } catch { } }
            if (startError != null) return; // Start() 已经抛给调用方了
            var h = Finished;
            if (h != null) h(OutputPath, duration, error ?? (Frames == 0 ? new Exception("没有录到画面") : null));
        }

        // ---------- 声音线程 ----------
        void AudioLoop()
        {
            int rate = loopback.SampleRate, block = LoopbackAudio.Channels * 2;
            long written = 0; // 已写入的采样帧数
            byte[] silence = new byte[rate / 10 * block];
            try
            {
                while (!stopping)
                {
                    Thread.Sleep(10);
                    bool paused = Paused;
                    bool got = false;
                    long target = RecordedTicks() * rate / 10000000L; // 录制时钟对应的采样位置
                    loopback.Drain((buf, count) =>
                    {
                        if (paused) return; // 暂停期间的声音丢掉
                        got = true;
                        int frames = count / block;
                        // 声卡时钟比录制时钟快太多（超前 0.2 秒以上）：丢掉这一包，保持同步
                        if (written > target + rate / 5) return;
                        if (Muted) Array.Clear(buf, 0, count);
                        writer.WriteAudio(buf, count, written * 10000000L / rate);
                        written += frames;
                    });
                    if (paused) continue;
                    // 没声音在放（系统不给数据）或者落后太多：补静音追上录制时钟
                    long lag = target - written;
                    if (lag > (got ? rate / 5 : rate / 20))
                    {
                        while (lag > 0)
                        {
                            int n = (int)Math.Min(lag, silence.Length / block);
                            writer.WriteAudio(silence, n * block, written * 10000000L / rate);
                            written += n;
                            lag -= n;
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                Host.Log("录声音出错（画面继续录）: " + ex);
                AudioError = ex.Message;
            }
        }

        // ---------- 鼠标指针 ----------
        IntPtr lastCursor;
        int hotX, hotY;

        void DrawCursorOn(IntPtr dc, bool scaled)
        {
            var ci = new CURSORINFO { cbSize = Marshal.SizeOf(typeof(CURSORINFO)) };
            if (!GetCursorInfo(ref ci) || (ci.flags & 1) == 0 || ci.hCursor == IntPtr.Zero) return;
            if (ci.hCursor != lastCursor)
            {
                ICONINFO ii;
                if (GetIconInfo(ci.hCursor, out ii))
                {
                    hotX = ii.xHotspot; hotY = ii.yHotspot;
                    if (ii.hbmMask != IntPtr.Zero) DeleteObject(ii.hbmMask);
                    if (ii.hbmColor != IntPtr.Zero) DeleteObject(ii.hbmColor);
                }
                lastCursor = ci.hCursor;
            }
            int x = ci.x - hotX - Region.X, y = ci.y - hotY - Region.Y;
            if (scaled) { x = x * outW / Region.Width; y = y * outH / Region.Height; }
            if (x < -64 || y < -64 || x > outW || y > outH) return;
            DrawIconEx(dc, x, y, ci.hCursor, 0, 0, 0, IntPtr.Zero, 3 /*DI_NORMAL*/);
        }

        // ---------- Win32 ----------
        const int SRCCOPY = 0x00CC0020;

        [StructLayout(LayoutKind.Sequential)]
        struct BITMAPINFOHEADER
        {
            public int biSize, biWidth, biHeight;
            public short biPlanes, biBitCount;
            public int biCompression, biSizeImage, biXPelsPerMeter, biYPelsPerMeter, biClrUsed, biClrImportant;
        }

        [StructLayout(LayoutKind.Sequential)]
        struct CURSORINFO { public int cbSize, flags; public IntPtr hCursor; public int x, y; }

        [StructLayout(LayoutKind.Sequential)]
        struct ICONINFO { public bool fIcon; public int xHotspot, yHotspot; public IntPtr hbmMask, hbmColor; }

        [DllImport("user32.dll")] static extern IntPtr GetDC(IntPtr hwnd);
        [DllImport("user32.dll")] static extern int ReleaseDC(IntPtr hwnd, IntPtr dc);
        [DllImport("user32.dll")] static extern bool GetCursorInfo(ref CURSORINFO ci);
        [DllImport("user32.dll")] static extern bool GetIconInfo(IntPtr icon, out ICONINFO info);
        [DllImport("user32.dll")] static extern bool DrawIconEx(IntPtr dc, int x, int y, IntPtr icon, int w, int h, int step, IntPtr brush, int flags);
        [DllImport("gdi32.dll")] static extern IntPtr CreateCompatibleDC(IntPtr dc);
        [DllImport("gdi32.dll")] static extern bool DeleteDC(IntPtr dc);
        [DllImport("gdi32.dll")] static extern IntPtr CreateDIBSection(IntPtr dc, ref BITMAPINFOHEADER bi, int usage, out IntPtr bits, IntPtr section, int offset);
        [DllImport("gdi32.dll")] static extern IntPtr SelectObject(IntPtr dc, IntPtr obj);
        [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr obj);
        [DllImport("gdi32.dll")] static extern bool BitBlt(IntPtr dst, int x, int y, int w, int h, IntPtr src, int sx, int sy, int rop);
        [DllImport("gdi32.dll")] static extern bool StretchBlt(IntPtr dst, int x, int y, int w, int h, IntPtr src, int sx, int sy, int sw, int sh, int rop);
        [DllImport("gdi32.dll")] static extern int SetStretchBltMode(IntPtr dc, int mode);
        [DllImport("gdi32.dll")] static extern bool GdiFlush();
        [DllImport("winmm.dll")] static extern int timeBeginPeriod(int ms);
        [DllImport("winmm.dll")] static extern int timeEndPeriod(int ms);
    }
}
